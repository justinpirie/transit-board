"""
HTTP API.

    GET /next
        ?direction=to_home         force a direction
        &at=home                   HA person state (slugified): picks direction,
                                   and pauses background polling once you've left
        &commute=off               calendar/toggle says no commute today → stop polling
        &arrive_by=2026-11-12T09:30  calendar: recommend options that arrive on time
    GET /health

All hint params are optional; without them the service falls back to
commute windows and time of day.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import load_config
from .calendar_ctx import CalendarRules
from .ha import (CalendarCache, Entities, HomeAssistant, TomorrowPublisher, publish_forever,
                 tomorrow_forever)
from .poller import Poller, fetch_stop_monitoring
from .siri import decode_511
from .schedule import ScheduleStore
from .setup import find_line, setup_report
from .tomorrow import build_tomorrow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs every request URL at INFO, and 511 takes the API key as a query
# parameter, so INFO would write the token into the app log. Keep it at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = Poller(load_config())
    app.state.poller = poller
    tasks = [asyncio.create_task(poller.run_forever())]

    # Inside an HA app: publish sensor.commute_leave_in and read hints from HA.
    token = os.environ.get("SUPERVISOR_TOKEN")
    ha_client = None
    if token:
        ha_client = httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"})
        ent = Entities(**{k: v for k, v in json.loads(os.environ.get("TRANSIT_ENTITIES", "{}")).items()
                          if v is not None})
        build = lambda **h: build_plan(poller, **h)
        c = json.loads(os.environ.get("TRANSIT_CALENDAR", "{}"))
        rules = CalendarRules(**{k: v for k, v in c.items() if v}) if c.get("work_calendars") else None
        if rules:
            logging.info("reading calendars %s (holidays: %s)", rules.work_calendars,
                         rules.holiday_calendar or "none")
        ha = HomeAssistant(ha_client)
        store = ScheduleStore(poller.cfg.api_key, os.environ.get("TRANSIT_GTFS_DIR", "/data/gtfs"),
                              poller.client, poller.limiter, poller.cfg.tz)
        app.state.tomorrow = lambda now: build_tomorrow(poller.cfg, store.departures, now, rules,
                                                        ha.get_events)
        app.state.schedule = store
        poller.schedule_fn = store.departures
        tasks.append(asyncio.create_task(publish_forever(
            ha, ent, build, CalendarCache(rules), poller.local_now)))
        tasks.append(asyncio.create_task(tomorrow_forever(
            ha, TomorrowPublisher(app.state.tomorrow), poller.local_now)))
        logging.info("publishing %s to Home Assistant every 60s", ent.sensor)
    yield
    for t in tasks:
        t.cancel()
    await poller.client.aclose()
    if ha_client:
        await ha_client.aclose()


app = FastAPI(title="transit-board", lifespan=lifespan)


def _bool(s: str | None) -> bool | None:
    if s is None or s == "":
        return None
    return s.lower() in ("1", "on", "true", "yes")


def _when(s: str | None, tz) -> datetime | None:
    """Parse arrive_by. Accepts HA's calendar format ('2026-11-12 09:30:00',
    naive local) or ISO with offset. Junk/empty → None (no deadline)."""
    if not s or s.lower() in ("none", "unknown", "unavailable"):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


@app.get("/next")
async def next_departures(direction: str | None = None, at: str | None = None,
                          commute: str | None = None, arrive_by: str | None = None):
    p: Poller = app.state.poller
    at = (at or "").strip().lower().replace(" ", "_") or None
    try:
        return await build_plan(p, direction=direction, at=at, commute=_bool(commute),
                                arrive_by=arrive_by)
    except KeyError as e:
        raise HTTPException(404, str(e))


def default_deadline(cfg, direction: str, now: datetime) -> datetime | None:
    """Same defaults as the night-before plan: be at the office by
    default_arrive_by, home by default_home_by, while those are still ahead."""
    origin = cfg.origin_zone(direction)
    hhmm = {"home": cfg.default_arrive_by, "office": cfg.default_home_by}.get(origin or "")
    if not hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else None


async def build_plan(p: Poller, direction: str | None = None, at: str | None = None,
                     commute: bool | None = None, arrive_by: str | None = None) -> dict:
    """One planning path for both /next and the HA sensor publisher."""
    p.set_hint(at, commute)

    now = p.local_now()
    direction, source = p.cfg.resolve_direction(now, direction, at)
    if direction not in p.cfg.directions:
        raise KeyError(f"unknown direction {direction!r}; have {sorted(p.cfg.directions)}")

    # A deadline only makes sense if it's in the future and today-ish
    deadline = _when(arrive_by, p.cfg.tz)
    if deadline and not (0 < (deadline - now).total_seconds() < 6 * 3600):
        deadline = None
    if deadline is None:
        deadline = default_deadline(p.cfg, direction, now)

    # Lazy refresh outside windows; inside, the background loop already did the work
    in_window = p.cfg.active_window(now) is not None
    if p.should_poll(direction) or not in_window:
        await p.refresh(direction, lazy=not in_window)
    await p.load_schedule(direction)
    return {**p.plan(direction, deadline), "direction_source": source}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Landing page for the HA sidebar panel (ingress). Links are relative
    because ingress serves the app under /api/hassio_ingress/<token>/."""
    return """<!doctype html><meta name=viewport content="width=device-width">
<body style="font-family:system-ui;padding:16px;max-width:640px">
<h2>Transit Board</h2><ul>
<li><a href="next">next</a>: current plan (JSON)</li>
<li><a href="setup">setup</a>: find stop codes for options still marked TODO</li>
<li><a href="health">health</a>: rate budget and status</li></ul></body>"""


@app.get("/setup", response_class=PlainTextResponse)
async def setup():
    """Find stop codes for options still marked TODO. See setup.py."""
    p: Poller = app.state.poller
    return await setup_report(p.cfg, p.client, p.limiter)


@app.get("/find_line", response_class=PlainTextResponse)
async def find_line_endpoint(agency: str, line: str, q: str = ""):
    """Stops where a line is serving right now, e.g. ?agency=AC&line=O&q=salesforce"""
    p: Poller = app.state.poller
    return await find_line(p.cfg, p.client, p.limiter, agency, line, q)


@app.get("/raw511")
async def raw511(endpoint: str, request: Request, sample: int = 2):
    """Debug: call a 511 transit endpoint and return its JSON, with every list
    cut to `sample` items. For inspecting response shapes we don't parse yet.
    e.g. /raw511?endpoint=stoptimetable&operatorref=AC&monitoringref=55537"""
    if not re.fullmatch(r"[a-z]+", endpoint):
        raise HTTPException(400, "endpoint must be a bare 511 transit path like 'stoptimetable'")
    p: Poller = app.state.poller
    if not p.limiter.try_acquire():
        raise HTTPException(429, "rate budget exhausted")
    params = {k: v for k, v in request.query_params.items() if k not in ("endpoint", "sample")}
    r = await p.client.get(f"https://api.511.org/transit/{endpoint}",
                           params={**params, "api_key": p.cfg.api_key, "format": "json"}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:500])

    def trim(x):
        if isinstance(x, list):
            return {"_len": len(x), "items": [trim(i) for i in x[:sample]]}
        if isinstance(x, dict):
            return {k: trim(v) for k, v in x.items()}
        return x
    return trim(decode_511(r.content))


@app.get("/tomorrow")
async def tomorrow_endpoint():
    """Tomorrow's plan, computed now (same as sensor.commute_tomorrow)."""
    build = getattr(app.state, "tomorrow", None)
    if build is None:
        raise HTTPException(503, "only available when running inside Home Assistant")
    return await build(app.state.poller.local_now())


@app.get("/schedule")
async def schedule_endpoint(agency: str, stop: str, day: str | None = None, after: str = "00:00",
                            limit: int = 30):
    """Debug: scheduled departures from GTFS, e.g. ?agency=SB&stop=7207&after=06:00"""
    store = getattr(app.state, "schedule", None)
    if store is None:
        raise HTTPException(503, "only available when running inside Home Assistant")
    p: Poller = app.state.poller
    d = datetime.fromisoformat(day).date() if day else p.local_now().date()
    deps = (await store.departures(agency, {stop}, d))[stop]
    out = []
    for a in deps:
        t = datetime.fromtimestamp(a["arrival_ts"], p.cfg.tz).strftime("%H:%M")
        if t >= after:
            out.append(f"{t}  {a['line']} → {a['dest']}")
    return {"date": d.isoformat(), "count": len(deps), "departures": out[:limit]}


@app.get("/line_stops")
async def line_stops_endpoint(agency: str, line: str, q: str = "", day: str | None = None):
    """Debug: stops a line serves per the GTFS timetable (no API calls once cached).
    e.g. ?agency=AC&line=W&q=central"""
    store = getattr(app.state, "schedule", None)
    if store is None:
        raise HTTPException(503, "only available when running inside Home Assistant")
    import asyncio, zipfile
    from .schedule import line_stops
    path = await store._ensure_feed(agency)
    if not path:
        raise HTTPException(502, f"no GTFS for {agency}")
    d = datetime.fromisoformat(day).date() if day else app.state.poller.local_now().date()
    return await asyncio.to_thread(lambda: line_stops(zipfile.ZipFile(path), line, d, q))


@app.get("/peek")
async def peek(agency: str, stop: str):
    """Live departures at any stop, unfiltered, for checking a stop code by hand."""
    p: Poller = app.state.poller
    if not p.limiter.try_acquire():
        raise HTTPException(429, "rate budget exhausted")
    arrivals = await fetch_stop_monitoring(p.client, p.cfg.api_key, agency, stop)
    now = p.clock()
    return sorted(({"line": a["line"], "line_ref": a["line_ref"], "line_name": a["line_name"], "dest": a["dest"], "realtime": a["realtime"],
                    "mins": int((a["arrival_ts"] - now) // 60)} for a in arrivals),
                  key=lambda a: a["mins"])


@app.get("/health")
async def health():
    p: Poller = app.state.poller
    w = p.cfg.active_window(p.local_now())
    return {"ok": True, "budget_used_last_hour": p.limiter.used,
            "budget_per_hour": p.cfg.request_budget_per_hour,
            "active_window": w.direction if w else None,
            "calls_per_hour": {d: round(p.cfg.calls_per_hour(d), 1) for d in p.cfg.directions},
            "options_missing_stop_codes": [f"{d}/{o.name}" for d, o in p.cfg.pending],
            "hint": vars(p.hint)}
