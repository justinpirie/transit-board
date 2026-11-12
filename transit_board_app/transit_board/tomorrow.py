"""
Tomorrow's plan, built the evening before from GTFS timetables + calendars.

Morning: be at the office by
    min(first in-person meeting − 5 min, default_arrive_by)
Evening: be home by
    min(first evening event at a home-area location − 10 min, default_home_by)

For each leg, every option's departures are turned into
    leave_at = departure − travel − buffer
    eta      = departure + ride + egress
and for each option we keep its LATEST departure that's still on time (leave
as late as possible). Then the same preference rules as the live plan apply:
regular options before fallbacks, and the most-preferred option wins unless
you'd have to leave more than `slack_minutes` earlier than with the
latest-leaving on-time option. With no on-time option at all, it picks the
earliest arrival and says how late.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta

from .calendar_ctx import CalendarRules, calendar_context
from .config import Config, Option


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _iso(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="minutes")


def plan_leg(options: list[Option], deps: dict[str, list[dict]], deadline: datetime,
             earliest: datetime, slack_minutes: int, tz) -> dict | None:
    """deps: {stop_key: [departure dicts]} for the day."""
    dl, early = deadline.timestamp(), earliest.timestamp()
    cands = []
    for o in options:
        best = None
        for d in deps.get(o.stop_key, []):
            if not o.accepts(d):
                continue
            leave = d["arrival_ts"] - (o.travel_minutes + o.buffer_minutes) * 60
            eta = d["arrival_ts"] + (o.ride_minutes + o.egress_minutes) * 60
            if leave < early:
                continue
            c = {"opt": o, "d": d, "leave": leave, "eta": eta, "on_time": eta <= dl}
            # Keep the latest on-time departure, or failing that the earliest arrival
            if best is None or (c["on_time"] and (not best["on_time"] or c["leave"] > best["leave"])) \
                    or (not c["on_time"] and not best["on_time"] and c["eta"] < best["eta"]):
                best = c
        if best:
            cands.append(best)
    if not cands:
        return None

    def pick(pool):
        latest = max(c["leave"] for c in pool)
        ok = [c for c in pool if c["leave"] >= latest - slack_minutes * 60]
        return min(ok, key=lambda c: (c["opt"].priority, -c["leave"]))

    on_time = [c for c in cands if c["on_time"]]
    primary = [c for c in on_time if not c["opt"].fallback]
    fallback = [c for c in on_time if c["opt"].fallback]
    if primary:
        rec, reason = pick(primary), "preferred"
    elif fallback:
        rec, reason = pick(fallback), "only the fallback arrives on time"
    else:
        rec = min(cands, key=lambda c: c["eta"])
        reason = f"running late: earliest arrival, ~{int((rec['eta'] - dl) // 60)} min after target"
    if reason == "preferred" and rec["opt"].priority != min(o.priority for o in options):
        reason = f"preferred options leave >{slack_minutes} min earlier or arrive late"

    rest = [c for c in on_time if c is not rec]
    rest_primary = [c for c in rest if not c["opt"].fallback]
    backup = pick(rest_primary) if rest_primary else (pick(rest) if rest else None)

    def out(c):
        return c and {"option": c["opt"].name, "mode": c["opt"].mode, "line": c["d"]["line"],
                      "dest": c["d"]["dest"], "leave_at": _iso(c["leave"], tz),
                      "depart": _iso(c["d"]["arrival_ts"], tz), "eta": _iso(c["eta"], tz),
                      "on_time": c["on_time"], "fallback": c["opt"].fallback}
    return {**out(rec), "reason": reason, "target": _iso(dl, tz), "backup": out(backup)}


def home_event(events: list[dict], pattern: str, day: date, tz, after: dtime = dtime(15, 0)):
    """First timed event on `day` starting after `after` at a home-area location."""
    rx = re.compile(pattern, re.I)
    best = None
    for e in events:
        s = e.get("start", "")
        if "T" not in s:
            continue
        st = datetime.fromisoformat(s)
        st = st if st.tzinfo else st.replace(tzinfo=tz)
        if st.date() == day and st.time() >= after and rx.search(e.get("location") or ""):
            if best is None or st < best[0]:
                best = (st, e)
    return best


async def build_tomorrow(cfg: Config, deps_for, now: datetime, rules: CalendarRules | None = None,
                         get_events=None) -> dict:
    """deps_for(agency, stops, day) → {stop: [deps]};  get_events(cals, start, end) → events."""
    tz = cfg.tz
    day = now.date() + timedelta(days=1)
    midnight = datetime.combine(day, dtime(0, 0), tz)

    events: dict[str, list[dict]] = {}
    if rules and rules.work_calendars and get_events:
        cals = rules.work_calendars + ([rules.holiday_calendar] if rules.holiday_calendar else [])
        events = await get_events(cals, midnight, midnight + timedelta(days=1))
    ctx = (calendar_context(events, rules, midnight) if rules and rules.work_calendars else
           {"commute_today": day.weekday() < 5, "reason": "workday" if day.weekday() < 5 else "weekend",
            "arrive_by": None, "first_in_person": None, "holiday": None})
    result = {"date": day.isoformat(), "commute": ctx["commute_today"], "reason": ctx["reason"],
              "holiday": ctx.get("holiday"), "first_in_person": ctx.get("first_in_person")}
    if not ctx["commute_today"]:
        return {**result, "morning": None, "evening": None}

    # Morning target: first in-person meeting, but no later than the default arrival time
    target = datetime.combine(day, _hhmm(cfg.default_arrive_by), tz)
    if ctx.get("arrive_by"):
        target = min(target, datetime.fromisoformat(ctx["arrive_by"]))
    # Evening target: first home-area event, but no later than the default home time
    home_by = datetime.combine(day, _hhmm(cfg.default_home_by), tz)
    work_events = [e for c in (rules.work_calendars if rules else []) for e in events.get(c, [])]
    he = home_event(work_events, cfg.home_location_regex, day, tz)
    if he:
        home_by = min(home_by, he[0] - timedelta(minutes=10))
        result["home_event"] = f"{he[0].strftime('%H:%M')} {he[1].get('summary')}"

    morning_dir = cfg.location_directions.get("home") or next(iter(cfg.directions))
    evening_dir = cfg.location_directions.get("office") or next(
        (d for d in cfg.directions if d != morning_dir), None)

    stops_by_agency: dict[str, set[str]] = {}
    for d in (morning_dir, evening_dir):
        for o in cfg.directions.get(d, []) if d else []:
            stops_by_agency.setdefault(o.agency, set()).add(o.stop)
    deps: dict[str, list[dict]] = {}
    for agency, stops in stops_by_agency.items():
        for stop, lst in (await deps_for(agency, stops, day)).items():
            deps[f"{agency}:{stop}"] = lst

    result["morning"] = plan_leg(cfg.directions[morning_dir], deps, target,
                                 target - timedelta(hours=4), cfg.slack_minutes, tz)
    result["evening"] = (plan_leg(cfg.directions[evening_dir], deps, home_by,
                                  home_by - timedelta(hours=3), cfg.slack_minutes, tz)
                         if evening_dir else None)
    return result
