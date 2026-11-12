"""
Commute context from calendars (read through Home Assistant).

From today's events this works out:
  commute_today  False on OOO/PTO days and weekends; True on workdays
  holiday        name of a public holiday today (commute stays True; HA asks)
  arrive_by      5 min before the first upcoming IN-PERSON event today
  first_in_person / reason   for display and debugging

What counts as in person is a regex on the event LOCATION, configurable in
the app options. The default fits a calendar where meetings book rooms in
several offices: only SF rooms ("SF-3-Room"), SF addresses, or
Market St addresses count. A Zoom link next to an SF room still counts,
because hybrid meetings list both. Rooms in other offices (e.g. NYC, LON)
don't: you'd join those by video.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_IN_PERSON = r"\bSF-\d|San Francisco|\b\d+\s+Market\b|Market St"
DEFAULT_OFF_DAY = r"\b(ooo|out of office|pto|vacation|sick|wfh|working from home)\b"


@dataclass
class CalendarRules:
    work_calendars: list[str]
    holiday_calendar: str = ""
    in_person: str = DEFAULT_IN_PERSON
    off_day: str = DEFAULT_OFF_DAY
    arrive_early_minutes: int = 5


def _parse(s: str, tz) -> datetime | None:
    if not s or "T" not in s:
        return None                      # all-day events have a bare date
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def calendar_context(events: dict[str, list[dict]], rules: CalendarRules, now: datetime) -> dict:
    """events: {calendar_entity: [HA calendar.get_events event dicts]} for today."""
    in_person = re.compile(rules.in_person, re.I)
    off_day = re.compile(rules.off_day, re.I)
    today = now.date().isoformat()

    # Public holidays don't turn the commute off: startups don't take every
    # holiday. They're flagged instead, so HA can ASK in the morning (an
    # actionable push), and the answer sets the "Commuting today" toggle.
    # Observances (Black Friday, Halloween) aren't flagged at all.
    holiday = next((e.get("summary") for e in events.get(rules.holiday_calendar, [])
                    if e.get("start", "")[:10] == today
                    and "public holiday" in (e.get("description") or "").lower()), None)

    def out(commute: bool, reason: str, arrive_by=None, first=None) -> dict:
        if holiday and commute:
            reason = f"{reason} (holiday: {holiday}; ask)"
        return {"commute_today": commute, "reason": reason, "arrive_by": arrive_by,
                "first_in_person": first, "holiday": holiday}

    work = [e for cal in rules.work_calendars for e in events.get(cal, [])]
    for e in work:
        start = e.get("start", "")
        all_day_today = "T" not in start and start[:10] <= today < (e.get("end") or "9999")[:10]
        timed_today = start[:10] == today
        if (all_day_today or timed_today) and off_day.search(e.get("summary") or ""):
            # Only all-day or long blocks mean a day off. A 30-min "PTO planning"
            # meeting must not cancel the commute.
            s, en = _parse(start, now.tzinfo), _parse(e.get("end", ""), now.tzinfo)
            if all_day_today or (s and en and en - s >= timedelta(hours=4)):
                return out(False, f"off: {e.get('summary')}")

    if now.weekday() >= 5:
        return out(False, "weekend")

    upcoming = sorted(
        (s, e) for e in work
        if (s := _parse(e.get("start", ""), now.tzinfo)) and s.date() == now.date() and s > now
        and in_person.search(e.get("location") or ""))
    if not upcoming:
        return out(True, "workday, no in-person events left today")
    start, e = upcoming[0]
    return out(True, "workday",
               (start - timedelta(minutes=rules.arrive_early_minutes)).isoformat(),
               f"{start.strftime('%H:%M')} {e.get('summary')} @ {e.get('location')}")
