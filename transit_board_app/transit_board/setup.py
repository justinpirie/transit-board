"""
Stop-code discovery, run from inside the app (on your network, where 511 is reachable).

GET /setup builds a plain-text report. For every option whose stop is still
TODO, it:
  1. searches the agency's stop list for the option's `stop_query` words
  2. pulls LIVE departures at each candidate and lists line → destination,
     with ✓ where it matches the option's lines / dest_match filters

✓ marks the right stop and the right side of the street (or platform). Copy the
id into `stop:`. Cost: one stops-list call per agency, plus one call per
candidate (capped). Run it outside a commute window so it doesn't compete
with polling for budget.
"""
from __future__ import annotations

import httpx

from .config import Config, Option
from .poller import RateLimiter, fetch_stop_monitoring
from .siri import decode_511

STOPS_URL = "https://api.511.org/transit/stops"
MAX_CANDIDATES = 6


async def stops_for_agency(client: httpx.AsyncClient, api_key: str, agency: str) -> list[dict]:
    r = await client.get(STOPS_URL, params={"api_key": api_key, "operator_id": agency,
                                            "format": "json"}, timeout=30)
    r.raise_for_status()
    pts = (decode_511(r.content).get("Contents", {}).get("dataObjects", {})
           .get("ScheduledStopPoint", []))
    return pts if isinstance(pts, list) else [pts]


def search(stops: list[dict], query: str) -> list[dict]:
    """All query words must appear in the stop name (case-insensitive).
    511 names use '&', ':' or 'and' inconsistently, so match words, not phrases."""
    words = [w for w in query.lower().replace("&", " ").split() if w not in ("and", "st", "av")]
    return [s for s in stops if all(w in (s.get("Name") or "").lower() for w in words)]


async def setup_report(cfg: Config, client: httpx.AsyncClient, limiter: RateLimiter) -> str:
    if not cfg.pending:
        return "All options have stop codes. Nothing to set up.\n"

    lines_out: list[str] = []
    stop_lists: dict[str, list[dict]] = {}
    peeked: dict[str, list[dict] | str] = {}

    def spend() -> bool:
        return limiter.try_acquire()

    for direction, opt in cfg.pending:
        lines_out.append(f"== {direction} / {opt.name}  (agency {opt.agency}, "
                         f"query {opt.stop_query!r}) ==")
        if not opt.stop_query:
            lines_out.append("   no stop_query in config; add one (e.g. 'santa clara bay')\n")
            continue

        if opt.agency not in stop_lists:
            if not spend():
                lines_out.append("   rate budget exhausted; try again later\n")
                continue
            try:
                stop_lists[opt.agency] = await stops_for_agency(client, cfg.api_key, opt.agency)
            except Exception as e:
                lines_out.append(f"   stops list failed: {e}\n")
                stop_lists[opt.agency] = []
                continue

        hits = search(stop_lists[opt.agency], opt.stop_query)
        if not hits:
            lines_out.append(f"   no stop names contain all of: {opt.stop_query}\n")
            continue
        if len(hits) > MAX_CANDIDATES:
            lines_out.append(f"   {len(hits)} matches, showing first {MAX_CANDIDATES}; "
                             "narrow stop_query for more")

        for s in hits[:MAX_CANDIDATES]:
            sid = str(s.get("id"))
            key = f"{opt.agency}:{sid}"
            if key not in peeked:
                if not spend():
                    peeked[key] = "rate budget exhausted"
                else:
                    try:
                        peeked[key] = await fetch_stop_monitoring(client, cfg.api_key,
                                                                  opt.agency, sid)
                    except Exception as e:
                        peeked[key] = f"error: {e}"
            lines_out.append(f"   stop {sid:>8}  {s.get('Name')}")
            lines_out.extend(_describe(opt, peeked[key]))
        lines_out.append("")

    lines_out.append(f"(511 calls used in the last hour: {limiter.used}/{limiter.limit})")
    return "\n".join(lines_out) + "\n"


def _describe(opt: Option, peek: list[dict] | str) -> list[str]:
    if isinstance(peek, str):
        return [f"              {peek}"]
    if not peek:
        return ["              (no live departures right now; try during service hours)"]
    seen: dict[tuple, int] = {}
    for a in peek:
        k = (a["line"], a["dest"])
        seen[k] = seen.get(k, 0) + 1
    return [f"            {'✓' if opt.accepts({'line': l, 'line_ref': l, 'dest': d}) else ' '} "
            f"{l} → {d}  ({n} upcoming)" for (l, d), n in sorted(seen.items(), key=str)]
