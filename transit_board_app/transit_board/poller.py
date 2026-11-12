"""
Cache, rate-limited fetcher, and the commute planner.

  * Cache holds ABSOLUTE departure timestamps per stop (unfiltered); each
    option filters at read time, so shared stops are fetched once.
  * A sliding-window limiter caps 511 calls per rolling hour; over budget we
    serve stale data (flagged) instead of getting the token throttled.
  * The background loop warms the active window's stops, unless Home
    Assistant's latest hint says there's no point (not commuting today, or
    you've already left the origin zone).
  * plan() turns departures into a ranked recommendation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import Config, Option
from .siri import decode_511, parse_stop_monitoring

log = logging.getLogger("transit_board")
STOP_MONITORING_URL = "https://api.511.org/transit/StopMonitoring"


class RateLimited(RuntimeError):
    pass


async def fetch_stop_monitoring(client: httpx.AsyncClient, api_key: str,
                                agency: str, stop: str) -> list[dict]:
    """One StopMonitoring call → parsed arrivals for exactly this stop."""
    r = await client.get(STOP_MONITORING_URL, params={
        # 511's spec spells it `stopcode`. Without a stop filter the API returns
        # the WHOLE agency, hence the stop_ref guard below.
        "api_key": api_key, "agency": agency, "stopcode": stop, "format": "json"})
    if r.status_code == 429:
        raise RateLimited("511 rate limit hit (HTTP 429); backing off 15 min")
    r.raise_for_status()
    arrivals = parse_stop_monitoring(decode_511(r.content))
    return [a for a in arrivals if a["stop_ref"] in (None, stop)]


class RateLimiter:
    """Sliding-window counter: at most `limit` events in any `window` seconds."""

    def __init__(self, limit: int, window: float = 3600.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events: deque[float] = deque()
        self.blocked_until = 0.0  # set on HTTP 429

    def _trim(self) -> None:
        cutoff = self.clock() - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def try_acquire(self) -> bool:
        now = self.clock()
        if now < self.blocked_until:
            return False
        self._trim()
        if len(self._events) >= self.limit:
            return False
        self._events.append(now)
        return True

    @property
    def used(self) -> int:
        self._trim()
        return len(self._events)


@dataclass
class Entry:
    fetched_at: float = 0.0
    arrivals: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class Hint:
    """Latest context pushed by Home Assistant on its /next poll."""
    at: str | None = None          # slugified person state: home / office / not_home
    commute: bool | None = None    # False = WFH / holiday / OOO per calendar or toggle
    ts: float = 0.0


class Poller:
    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.client = client or httpx.AsyncClient(timeout=10)
        self.limiter = RateLimiter(cfg.request_budget_per_hour)
        self.cache = {k: Entry() for k in cfg.all_stop_keys()}
        self._locks = {k: asyncio.Lock() for k in self.cache}
        self.hint = Hint()

    # ── time helpers ───────────────────────────────────────────────────
    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.cfg.tz)

    def _iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.cfg.tz).isoformat(timespec="minutes")

    # ── fetching ───────────────────────────────────────────────────────
    async def _fetch(self, key: str, agency: str, stop: str) -> None:
        entry = self.cache[key]
        if not self.limiter.try_acquire():
            log.warning("rate budget exhausted; serving stale data for %s", key)
            return
        try:
            arrivals = await fetch_stop_monitoring(self.client, self.cfg.api_key, agency, stop)
        except RateLimited as e:
            self.limiter.blocked_until = self.limiter.clock() + 900
            entry.error = str(e)
            log.error("fetch %s failed: %s", key, e)
            return
        except Exception as e:
            entry.error = str(e)
            log.error("fetch %s failed: %s", key, e)
            return
        entry.arrivals, entry.fetched_at, entry.error = arrivals, self.clock(), None

    async def refresh(self, direction: str, lazy: bool = False) -> None:
        """Fetch stops in `direction` whose data is older than their refresh
        interval (or idle_ttl when lazy, i.e. outside a commute window)."""
        async def one(key, agency, stop, refresh_s):
            max_age = max(refresh_s, self.cfg.idle_ttl_seconds) if lazy else refresh_s
            async with self._locks[key]:
                if self.clock() - self.cache[key].fetched_at >= max_age:
                    await self._fetch(key, agency, stop)
        await asyncio.gather(*(one(k, *v) for k, v in self.cfg.stops(direction).items()))

    def set_hint(self, at: str | None, commute: bool | None) -> None:
        if at is not None or commute is not None:
            self.hint = Hint(at=at, commute=commute, ts=self.clock())

    def should_poll(self, direction: str) -> bool:
        """Background polling gate. Without a fresh hint, poll (fail open)."""
        h = self.hint
        if self.clock() - h.ts > self.cfg.hint_ttl_seconds:
            return True
        if h.commute is False:
            return False                      # WFH / holiday: zero API calls
        origin = self.cfg.origin_zone(direction)
        if origin and h.at and h.at != origin:
            return False                      # already left (or never there): stop spending budget
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                w = self.cfg.active_window(self.local_now())
                if w and self.should_poll(w.direction):
                    await self.refresh(w.direction)
            except Exception:
                log.exception("poll loop error")
            await asyncio.sleep(15)

    # ── planning ───────────────────────────────────────────────────────
    def _departures(self, opt: Option, now: float) -> list[dict]:
        out = []
        for a in self.cache[opt.stop_key].arrivals:
            if not opt.accepts(a):
                continue
            mins = int((a["arrival_ts"] - now) // 60)
            leave_in = mins - opt.travel_minutes - opt.buffer_minutes
            if leave_in < 0:
                continue                      # can't make it
            eta = a["arrival_ts"] + (opt.ride_minutes + opt.egress_minutes) * 60
            out.append({"line": a["line"], "dest": a["dest"], "mins": mins,
                        "leave_in": leave_in, "realtime": a["realtime"],
                        "depart": self._iso(a["arrival_ts"]), "eta": self._iso(eta),
                        "_eta_ts": eta})
        return sorted(out, key=lambda d: d["leave_in"])

    def plan(self, direction: str, arrive_by: datetime | None = None) -> dict:
        now = self.clock()
        deadline = arrive_by.timestamp() if arrive_by else None

        options, errors, oldest = [], [], None
        for opt in self.cfg.directions[direction]:
            e = self.cache[opt.stop_key]
            if e.error:
                errors.append(f"{opt.name}: {e.error}")
            if e.fetched_at:
                oldest = e.fetched_at if oldest is None else min(oldest, e.fetched_at)
            deps = self._departures(opt, now)
            for d in deps:
                d["on_time"] = None if deadline is None else d["_eta_ts"] <= deadline
            options.append({"option": opt.name, "mode": opt.mode, "priority": opt.priority,
                            "fallback": opt.fallback,
                            "next": deps[0] if deps else None,
                            "later": [d["leave_in"] for d in deps[1:3]]})

        rec, reason = self._recommend(options, deadline)
        # Backup = what the same logic would pick if the recommendation vanished
        # (missed the bus, ferry cancelled). Ranking stays here, not in HA Jinja.
        bak, _ = self._recommend([o for o in options if o is not rec], deadline) if rec else (None, "")
        clean = lambda d: d and {k: v for k, v in d.items() if not k.startswith("_")}
        for o in options:
            o["next"] = clean(o["next"])
        age = None if oldest is None else int(now - oldest)
        return {
            "direction": direction,
            "arrive_by": arrive_by.isoformat(timespec="minutes") if arrive_by else None,
            "recommended": rec and {**clean(rec["next"]), "option": rec["option"],
                                    "mode": rec["mode"], "reason": reason},
            "backup": bak and {**clean(bak["next"]), "option": bak["option"], "mode": bak["mode"]},
            "next_leave_in": rec["next"]["leave_in"] if rec else None,  # HA sensor state
            "options": options,
            "data_age_seconds": age,
            "stale": age is None or age > 2 * max(self.cfg.idle_ttl_seconds, 600),
            "errors": errors,
            "budget_used_last_hour": self.limiter.used,
            "polling_paused": not self.should_poll(direction),
        }

    def _recommend(self, options: list[dict], deadline: float | None):
        """Pick one option.

        With a deadline (calendar): the most-preferred option that arrives on
        time; if none can, the one that arrives earliest (and say so).
        Without: the most-preferred option arriving within `slack_minutes` of
        the fastest. So the ferry wins unless it would cost you >15 min.
        """
        live = [o for o in options if o["next"]]
        if not live:
            return None, "no catchable departures"
        rank = lambda o: (o["priority"], o["next"]["_eta_ts"])
        # Fallback options (Uber) are a last resort: they're used only when no
        # regular option works, and never set the "fastest" baseline. Otherwise
        # a pricey Uber arriving first would knock the ferry out of the slack window.
        primary = [o for o in live if not o.get("fallback")]
        fallbacks = [o for o in live if o.get("fallback")]

        if deadline is not None:
            for pool, why_pool in ((primary, None), (fallbacks, "only the fallback arrives on time")):
                on_time = [o for o in pool if o["next"]["_eta_ts"] <= deadline]
                if on_time:
                    pick = min(on_time, key=rank)
                    why = why_pool or ("preferred" if pick["priority"] == min(o["priority"] for o in options)
                                       else "preferred options would be late")
                    return pick, why
            pick = min(live, key=lambda o: o["next"]["_eta_ts"])
            late = int((pick["next"]["_eta_ts"] - deadline) // 60)
            return pick, f"running late: earliest arrival, ~{late} min after deadline"

        if not primary:
            return min(fallbacks, key=rank), "nothing else catchable"
        best_eta = min(o["next"]["_eta_ts"] for o in primary)
        pool = [o for o in primary if o["next"]["_eta_ts"] <= best_eta + self.cfg.slack_minutes * 60]
        pick = min(pool, key=rank)
        why = "preferred" if pick["priority"] == min(o["priority"] for o in options) \
            else f"preferred options arrive >{self.cfg.slack_minutes} min later or aren't catchable"
        return pick, why
