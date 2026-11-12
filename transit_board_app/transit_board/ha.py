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

import httpx

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

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        r = await self.client.post(f"{self.base}/states/{entity_id}",
                                   json={"state": state, "attributes": attributes})
        r.raise_for_status()


async def read_hints(ha: HomeAssistant, ent: Entities) -> dict:
    """Collect the same hints the old REST sensor sent as query params."""
    person, commute, arrive_by = await asyncio.gather(
        ha.state(ent.person), ha.state(ent.commute), ha.state(ent.arrive_by))
    return {
        "at": slugify(person) if person else None,
        "commute": None if commute is None else commute == "on",
        # Like the old REST sensor: a deadline only applies to the outbound trip,
        # i.e. while you're still home.
        "arrive_by": arrive_by if person == "home" else None,
    }


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


async def publish_once(ha: HomeAssistant, ent: Entities, build_plan) -> dict:
    hints = await read_hints(ha, ent)
    plan = await build_plan(**hints)
    state, attrs = sensor_payload(plan, ent)
    await ha.set_state(ent.sensor, state, attrs)
    return plan


async def publish_forever(ha: HomeAssistant, ent: Entities, build_plan, every: float = 60) -> None:
    while True:
        try:
            await publish_once(ha, ent, build_plan)
        except Exception as e:  # HA restarting, network blip: try again next tick
            log.warning("publish to Home Assistant failed: %s", e)
        await asyncio.sleep(every)
