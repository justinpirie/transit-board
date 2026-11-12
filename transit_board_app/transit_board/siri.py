"""
Parse 511.org SIRI StopMonitoring responses into plain dicts.

Kept separate from the HTTP/caching code so it can be unit-tested against
saved fixtures without touching the network or burning API quota.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def decode_511(raw: bytes) -> dict[str, Any]:
    """Decode a 511 response body.

    511 prefixes every JSON body with a UTF-8 BOM (EF BB BF). Plain
    `json.loads(raw)` or `response.json()` chokes on it, so decode with
    'utf-8-sig', which strips a leading BOM if present and is a no-op otherwise.
    """
    return json.loads(raw.decode("utf-8-sig"))


def _as_list(x: Any) -> list:
    """SIRI-as-JSON is inconsistent: a field can be an object, a list of
    objects, or missing, depending on how many items there are. Normalise."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _text(x: Any) -> str | None:
    """Some SIRI text fields arrive as a string, some as a one-item list,
    some as {"value": "..."}; collapse them all to a plain string."""
    for item in _as_list(x):
        if isinstance(item, str):
            return item
        if isinstance(item, dict) and "value" in item:
            return item["value"]
    return None


def _parse_ts(s: str | None) -> float | None:
    """ISO-8601 → epoch seconds. Python 3.11+ fromisoformat handles 'Z'."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_stop_monitoring(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract upcoming vehicles from a StopMonitoring payload.

    Returns a list of:
        {"line": "NL", "dest": "San Francisco", "arrival_ts": 1762870000.0,
         "realtime": True, "stop_name": "Telegraph Av & 40th St"}

    `arrival_ts` is an absolute epoch timestamp, NOT minutes-from-now. The
    caller computes minutes at read time so cached data never goes stale.
    """
    delivery = payload.get("ServiceDelivery", {}).get("StopMonitoringDelivery", {})
    visits = []
    # StopMonitoringDelivery is an object from 511 today, but SIRI allows a list
    for d in _as_list(delivery):
        visits.extend(_as_list(d.get("MonitoredStopVisit")))

    out = []
    for v in visits:
        j = v.get("MonitoredVehicleJourney") or {}
        call = j.get("MonitoredCall") or {}

        # Preference order:
        #   1. Expected* = real-time prediction from the vehicle's AVL feed
        #   2. Aimed*    = timetable (no live data for this trip)
        # Departure before arrival: you need to be there before it LEAVES.
        # That matters at ferry terminals and trip origins, where a vessel or
        # bus can sit for minutes between arriving and departing.
        expected = call.get("ExpectedDepartureTime") or call.get("ExpectedArrivalTime")
        aimed = call.get("AimedDepartureTime") or call.get("AimedArrivalTime")
        ts = _parse_ts(expected) or _parse_ts(aimed)
        if ts is None:
            continue

        out.append({
            # PublishedLineName is the rider-facing name ("NL", "Yellow");
            # LineRef is the internal ID. Prefer the former for display.
            "line": _text(j.get("PublishedLineName")) or _text(j.get("LineRef")),
            "line_ref": _text(j.get("LineRef")),
            "dest": _text(j.get("DestinationName")),
            "arrival_ts": ts,
            "realtime": bool(_parse_ts(expected)),
            "stop_name": _text(call.get("StopPointName")),
            # Which stop this visit is for. Lets the poller drop visits for
            # other stops if 511 ever ignores the stop filter and answers
            # agency-wide.
            "stop_ref": _text(call.get("StopPointRef")) or _text(v.get("MonitoringRef")),
        })
    return out
