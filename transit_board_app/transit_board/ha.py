"""
Home Assistant integration: read hints from HA, write the plan back as a sensor.

Inside an HA app with `homeassistant_api: true`, the Supervisor provides
SUPERVISOR_TOKEN and proxies Core's REST API at http://supervisor/core/api.
That gets us both directions with no YAML in configuration.yaml:

  read   person.<you>                  → `at` (home / office / not_home)
         input_boolean.commute_today   → `commute` (calendar/WFH toggle)
         sensor.commute_arrive_by      → `arrive_by` (first in-person meeting)
  write  sensor.commute_leave_in       → state = minutes until you leave,
                                         attributes = the full plan

Entities written through POST /api/states aren't in the entity registry: HA
drops them on restart until the next publish (≤60s later), and they can't be
renamed in the UI. That's the trade-off for needing no configuration.yaml edit.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .calendar_ctx import CalendarRules, calendar_context

log = logging.getLogger("transit_board")


@dataclass
class Entities:
    sensor: str = "sensor.commute_leave_in"
    person: str = ""                                 # e.g. person.jane; "" = don't use GPS
    commute: str = "input_boolean.commute_today"     # missing entity = no hint
    arrive_by: str = "sensor.commute_arrive_by"      # missing entity = no deadline


def slugify(s: str) -> str:
    """Approximates HA's slugify for zone names: 'Office' → 'office', 'Work HQ' → 'work_hq'."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


class HomeAssistant:
    def __init__(self, client: httpx.AsyncClient, base: str = "http://supervisor/core/api"):
        self.client, self.base = client, base.rstrip("/")

    async def state(self, entity_id: str) -> str | None:
        """Current state string, or None if the entity doesn't exist / isn't usable."""
        if not entity_id:
            return None
        r = await self.client.get(f"{self.base}/states/{entity_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        s = r.json().get("state")
        return None if s in (None, "unknown", "unavailable", "") else s

    async def get_events(self, entity_ids: list[str], start, end) -> dict[str, list[dict]]:
        """calendar.get_events through the REST API (needs ?return_response)."""
        r = await self.client.post(
            f"{self.base}/services/calendar/get_events?return_response",
            json={"entity_id": entity_ids, "start_date_time": start.isoformat(),
                  "end_date_time": end.isoformat()})
        r.raise_for_status()
        resp = r.json().get("service_response", {})
        return {k: v.get("events", []) for k, v in resp.items()}

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        r = await self.client.post(f"{self.base}/states/{entity_id}",
                                   json={"state": state, "attributes": attributes})
        r.raise_for_status()


class CalendarCache:
    """Calendar context, recomputed at most every 10 min (Google sync is slower anyway)."""
    def __init__(self, rules: CalendarRules | None, ttl: float = 600):
        self.rules, self.ttl = rules, ttl
        self.ctx: dict | None = None
        self.at: float = 0.0

    async def get(self, ha: HomeAssistant, now: datetime) -> dict | None:
        if not self.rules or not self.rules.work_calendars:
            return None
        if self.ctx is None or now.timestamp() - self.at > self.ttl:
            cals = self.rules.work_calendars + ([self.rules.holiday_calendar]
                                                if self.rules.holiday_calendar else [])
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            events = await ha.get_events(cals, start, start + timedelta(days=1))
            self.ctx, self.at = calendar_context(events, self.rules, now), now.timestamp()
        return self.ctx


async def read_hints(ha: HomeAssistant, ent: Entities, cal: CalendarCache | None = None,
                     now: datetime | None = None) -> tuple[dict, dict | None]:
    """Collect hints: GPS, the manual toggle, and (if configured) calendar context.

    The calendar decides whether today is a commute day; the "Commuting today"
    toggle, if it exists and is OFF, overrides it (e.g. a sick day the calendar
    doesn't know about)."""
    person, toggle, arrive_by = await asyncio.gather(
        ha.state(ent.person), ha.state(ent.commute), ha.state(ent.arrive_by))
    ctx = None
    if cal is not None and now is not None:
        try:
            ctx = await cal.get(ha, now)
        except Exception as e:
            log.warning("calendar read failed: %s", e)

    commute = None if toggle is None else toggle == "on"
    if ctx is not None:
        commute = ctx["commute_today"] and commute is not False
        arrive_by = ctx["arrive_by"]
    return {
        "at": slugify(person) if person else None,
        "commute": commute,
        # A deadline only applies to the outbound trip, i.e. while you're home.
        "arrive_by": arrive_by if person == "home" else None,
    }, ctx


def sensor_payload(plan: dict, ent: Entities) -> tuple[object, dict]:
    """Plan → (state, attributes). State is minutes until you must leave for
    the recommended option, or 'unavailable' when nothing is catchable."""
    state = plan["next_leave_in"] if plan["next_leave_in"] is not None else "unavailable"
    attrs = {k: v for k, v in plan.items() if k != "next_leave_in"}
    attrs.update({
        "friendly_name": "Commute leave in",
        "unit_of_measurement": "min",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": {"ferry": "mdi:ferry", "bus": "mdi:bus", "bart": "mdi:train"}.get(
            (plan.get("recommended") or {}).get("mode"), "mdi:transit-connection-variant"),
    })
    return state, attrs


async def publish_once(ha: HomeAssistant, ent: Entities, build_plan,
                       cal: CalendarCache | None = None, now: datetime | None = None) -> dict:
    hints, ctx = await read_hints(ha, ent, cal, now)
    plan = await build_plan(**hints)
    state, attrs = sensor_payload(plan, ent)
    attrs["commute_today"] = hints["commute"]
    attrs["calendar"] = ctx      # reason / first_in_person, for the card and debugging
    await ha.set_state(ent.sensor, state, attrs)
    return plan


TOMORROW_SENSOR = "sensor.commute_tomorrow"


def tomorrow_payload(t: dict) -> tuple[str, dict]:
    """State = when to leave tomorrow morning (timestamp), 'unknown' on days off."""
    m = t.get("morning")
    state = (datetime.fromisoformat(m["leave_at"]).isoformat(timespec="seconds")
             if t.get("commute") and m else "unknown")
    return state, {**t, "friendly_name": "Commute tomorrow", "device_class": "timestamp",
                   "icon": "mdi:calendar-clock"}


class TomorrowPublisher:
    """Recompute tomorrow's plan from 15:00 onwards, at most every 30 min
    (calendars change during the day; timetables are cached on disk)."""
    def __init__(self, build, every: float = 1800):
        self.build, self.every = build, every
        self.at: datetime | None = None

    async def maybe_publish(self, ha: HomeAssistant, now: datetime) -> dict | None:
        if now.hour < 15:
            return None
        if self.at and self.at.date() == now.date() and (now - self.at).total_seconds() < self.every:
            return None
        t = await self.build(now)
        state, attrs = tomorrow_payload(t)
        await ha.set_state(TOMORROW_SENSOR, state, attrs)
        self.at = now
        return t


async def tomorrow_forever(ha: HomeAssistant, tomorrow: TomorrowPublisher, now_fn,
                           every: float = 60) -> None:
    """Separate loop from the live sensor: the first GTFS download + parse
    takes a while on a Pi, and must not stall live updates."""
    while True:
        try:
            await tomorrow.maybe_publish(ha, now_fn())
        except Exception as e:
            log.warning("tomorrow plan failed: %s", e)
        await asyncio.sleep(every)


async def publish_forever(ha: HomeAssistant, ent: Entities, build_plan,
                          cal: CalendarCache | None = None, now_fn=None, every: float = 60) -> None:
    while True:
        try:
            await publish_once(ha, ent, build_plan, cal, now_fn() if now_fn else None)
        except Exception as e:  # HA restarting, network blip: try again next tick
            log.warning("publish to Home Assistant failed: %s", e)
        await asyncio.sleep(every)
