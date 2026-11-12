"""
Load and validate config.yaml.

Model:
  direction (to_office / to_home)
    └─ options, ranked by `priority` (1 = preferred)
         each option = one stop + how you get there + how long the ride is

Several options may share one stop (e.g. Montgomery BART is both "BART to
Fruitvale" and "BART to West Oakland + Uber"). The stop is fetched once and
each option applies its own line/destination filter.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger("transit_board")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Per-mode defaults. Buffers are minutes you want to be AT the stop before
# departure. Refresh is how often to re-poll 511: buses drift the most, so
# they get the budget. Ferries/BART run close to schedule.
MODE_DEFAULTS = {
    "ferry": {"buffer_minutes": 3, "refresh_seconds": 600},
    "bart":  {"buffer_minutes": 3, "refresh_seconds": 600},
    "bus":   {"buffer_minutes": 2, "refresh_seconds": 240},
}


@dataclass(frozen=True)
class Option:
    name: str                   # "Ferry", "O bus", "Uber → West Oakland BART"
    mode: str                   # ferry | bus | bart (selects defaults above)
    priority: int               # 1 = preferred; equal priorities compete on ETA
    agency: str                 # 511 operator: SB=SF Bay Ferry, AC, BA=BART, SF=Muni
    stop: str                   # 511 stopCode
    travel_minutes: int = 0     # origin door → stop (bike, walk, or Uber incl. pickup wait)
    buffer_minutes: int = 2     # be at the stop this long before departure
    ride_minutes: int = 0       # in-vehicle time
    egress_minutes: int = 0     # stop → destination door
    refresh_seconds: int = 300
    lines: tuple[str, ...] = ()       # allow-list on line name / LineRef
    dest_match: tuple[str, ...] = ()  # allow-list: substring of destination (case-insensitive)
    stop_query: str = ""              # words to find the stop by name (used by /setup)

    @property
    def stop_key(self) -> str:
        return f"{self.agency}:{self.stop}"

    def accepts(self, a: dict) -> bool:
        if self.lines and a["line"] not in self.lines and a["line_ref"] not in self.lines:
            return False
        if self.dest_match:
            dest = (a["dest"] or "").lower()
            if not any(m.lower() in dest for m in self.dest_match):
                return False
        return True


@dataclass(frozen=True)
class Window:
    """Commute window: while active (and HA hints don't say otherwise), the
    poller keeps this direction's stops warm in the background."""
    direction: str
    start: time
    end: time
    days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")

    def contains(self, now: datetime) -> bool:
        return DAYS[now.weekday()] in self.days and self.start <= now.time() < self.end


@dataclass
class Config:
    api_key: str
    tz: ZoneInfo
    directions: dict[str, list[Option]]
    windows: list[Window] = field(default_factory=list)
    # HA zone (slugified person state) → direction to show when you're there.
    # e.g. {"home": "to_office", "office": "to_home"}
    location_directions: dict[str, str] = field(default_factory=dict)
    switch_hour: int = 12
    slack_minutes: int = 15            # Accept a preferred option arriving up to this much later than the fastest
    request_budget_per_hour: int = 50  # 511 default token limit is 60/hr
    idle_ttl_seconds: int = 600
    hint_ttl_seconds: int = 600        # How long an HA location/commute hint stays valid
    pending: list[tuple[str, Option]] = field(default_factory=list)  # (direction, option) with stop TODO

    # ── direction ──────────────────────────────────────────────────────
    def active_window(self, now: datetime) -> Window | None:
        return next((w for w in self.windows if w.contains(now)), None)

    def resolve_direction(self, now: datetime, override: str | None = None,
                          at: str | None = None) -> tuple[str, str]:
        """Return (direction, why). Priority: explicit > location > window > clock."""
        if override:
            return override, "override"
        if at and at in self.location_directions:
            return self.location_directions[at], f"location:{at}"
        w = self.active_window(now)
        if w:
            return w.direction, "window"
        names = list(self.directions)
        return (names[0] if now.hour < self.switch_hour or len(names) == 1 else names[1]), "clock"

    def origin_zone(self, direction: str) -> str | None:
        """The zone you start `direction` from (inverse of location_directions)."""
        return next((z for z, d in self.location_directions.items() if d == direction), None)

    # ── stops / budget ─────────────────────────────────────────────────
    def stops(self, direction: str) -> dict[str, tuple[str, str, int]]:
        """Unique stops for a direction → (agency, stop, refresh_seconds).
        A shared stop takes the shortest refresh among the options using it."""
        out: dict[str, tuple[str, str, int]] = {}
        for o in self.directions[direction]:
            prev = out.get(o.stop_key)
            r = o.refresh_seconds if prev is None else min(prev[2], o.refresh_seconds)
            out[o.stop_key] = (o.agency, o.stop, r)
        return out

    def all_stop_keys(self) -> set[str]:
        return {o.stop_key for opts in self.directions.values() for o in opts}

    def calls_per_hour(self, direction: str) -> float:
        return sum(3600 / r for _, _, r in self.stops(direction).values())


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _option(raw: dict) -> Option:
    mode = raw["mode"]
    d = {**MODE_DEFAULTS.get(mode, {}), **raw}
    return Option(
        name=d["name"], mode=mode, priority=int(d["priority"]),
        agency=d["agency"], stop=str(d["stop"]),
        travel_minutes=int(d.get("travel_minutes", 0)),
        buffer_minutes=int(d.get("buffer_minutes", 2)),
        ride_minutes=int(d.get("ride_minutes", 0)),
        egress_minutes=int(d.get("egress_minutes", 0)),
        refresh_seconds=int(d.get("refresh_seconds", 300)),
        lines=tuple(str(x) for x in d.get("lines", [])),
        dest_match=tuple(str(x) for x in d.get("dest_match", [])),
        stop_query=str(d.get("stop_query", "")),
    )


def is_unresolved(o: Option) -> bool:
    return o.stop.strip().upper() in ("", "TODO")


def apply_overrides(raw: dict, overrides: list[str]) -> list[str]:
    """Apply 'key=value' overrides (from the app's Configuration tab) onto the
    raw YAML dict, so values can be changed remotely without editing files.

        slack_minutes=10                    top-level setting
        to_office/Ferry.stop=12345          option field (option matched by name)
        to_office/Ferry.lines=O,OX          comma list for lines / dest_match

    Values are parsed as YAML scalars ("10" → 10). Returns error strings for
    bad entries instead of raising: one typo shouldn't stop the app."""
    errors = []
    for item in overrides:
        if "=" not in item:
            errors.append(f"{item!r}: expected key=value")
            continue
        key, value = (x.strip() for x in item.split("=", 1))
        if "/" not in key:
            raw[key] = yaml.safe_load(value)
            continue
        direction, _, rest = key.partition("/")
        name, dot, field_name = rest.rpartition(".")
        if not dot:
            errors.append(f"{item!r}: expected direction/Option name.field=value")
            continue
        opts = (raw.get("directions") or {}).get(direction) or []
        target = next((o for o in opts if o.get("name") == name), None)
        if target is None:
            errors.append(f"{item!r}: no option named {name!r} in {direction!r}")
            continue
        target[field_name] = ([v.strip() for v in value.split(",")]
                              if field_name in ("lines", "dest_match")
                              else str(value) if field_name == "stop"
                              else yaml.safe_load(value))
    return errors


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("TRANSIT_CONFIG", "config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    import json
    for err in apply_overrides(raw, json.loads(os.environ.get("TRANSIT_OVERRIDES", "[]"))):
        log.error("override ignored: %s", err)

    # The key comes from the environment so config.yaml can be committed
    api_key = os.environ.get("API_KEY_511") or os.environ.get("API_KEY", "")
    if not api_key:
        raise SystemExit("Set API_KEY_511 (free token: https://511.org/open-data/token)")

    # Options whose stop is still TODO are kept aside for /setup and never
    # polled, so a half-finished config doesn't burn API budget on errors.
    directions, pending = {}, []
    for name, opts in (raw.get("directions") or {}).items():
        parsed = sorted((_option(o) for o in opts), key=lambda o: o.priority)
        pending += [(name, o) for o in parsed if is_unresolved(o)]
        directions[name] = [o for o in parsed if not is_unresolved(o)]
    if not directions:
        raise SystemExit(f"{path}: no directions configured")
    if pending:
        log.warning("%d option(s) have no stop code yet; open /setup to find them", len(pending))

    windows = []
    for w in raw.get("windows") or []:
        if w["direction"] not in directions:
            raise SystemExit(f"{path}: window direction {w['direction']!r} is not defined")
        windows.append(Window(w["direction"], _parse_time(w["start"]), _parse_time(w["end"]),
                              tuple(w.get("days", Window.days))))

    cfg = Config(api_key=api_key, tz=ZoneInfo(raw.get("timezone", "America/Los_Angeles")),
                 directions=directions, windows=windows,
                 location_directions=raw.get("location_directions") or {}, pending=pending)
    for k in ("switch_hour", "slack_minutes", "request_budget_per_hour",
              "idle_ttl_seconds", "hint_ttl_seconds"):
        if k in raw:
            setattr(cfg, k, int(raw[k]))

    for d in directions:  # Fail loudly at startup, not at 8am when you're out of quota
        cph = cfg.calls_per_hour(d)
        if cph > cfg.request_budget_per_hour:
            log.warning("%s needs ~%.0f calls/hr but budget is %d; data will go stale. "
                        "Raise refresh_seconds or request a higher 511 limit.",
                        d, cph, cfg.request_budget_per_hour)
    return cfg
