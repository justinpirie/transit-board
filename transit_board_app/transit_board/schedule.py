"""
Scheduled departures from GTFS, for planning tomorrow.

511's real-time feed only looks about an hour ahead, and its StopTimetable
API only answers for "timepoint" stops (most bus stops get HTTP 412). GTFS
covers every stop. 511 serves one zip per agency:
    https://api.511.org/transit/datafeeds?operator_id=AC
The app downloads each at most weekly (1 API call each), caches it on disk,
and reads only the rows for the stops in config.yaml.

GTFS stop_ids in 511's feeds are the same codes its SIRI APIs use, so the
stop codes in config.yaml work for both.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
import zipfile
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger("transit_board")
FEED_URL = "https://api.511.org/transit/datafeeds"
MAX_AGE = 7 * 86400


def _rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return iter(())
    # utf-8-sig: 511's GTFS files can start with a BOM, like its JSON
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


def active_services(z: zipfile.ZipFile, day: date) -> set[str]:
    """service_ids running on `day`, from calendar.txt + calendar_dates.txt exceptions."""
    ymd, dow = day.strftime("%Y%m%d"), day.strftime("%A").lower()
    active = {r["service_id"] for r in _rows(z, "calendar.txt")
              if r["start_date"] <= ymd <= r["end_date"] and r.get(dow) == "1"}
    for r in _rows(z, "calendar_dates.txt"):
        if r["date"] == ymd:
            (active.add if r["exception_type"] == "1" else active.discard)(r["service_id"])
    return active


def departures_from_zip(z: zipfile.ZipFile, stops: set[str], day: date, tz) -> dict[str, list[dict]]:
    """{stop_id: [{line, dest, arrival_ts, realtime: False}, ...]} for one service day.

    Same dict shape as the real-time parser, so Option.accepts() and the
    planner work on both."""
    services = active_services(z, day)
    routes = {r["route_id"]: (r.get("route_short_name") or r.get("route_long_name") or r["route_id"])
              for r in _rows(z, "routes.txt")}
    trips = {r["trip_id"]: (routes.get(r["route_id"], r["route_id"]), r.get("trip_headsign") or "")
             for r in _rows(z, "trips.txt") if r["service_id"] in services}
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)

    # One pass over the big file. For every running trip, remember its LAST
    # stop: at a terminal (e.g. a ferry dock) the timetable lists both
    # departures and arrivals, and a trip whose final stop is ours is an
    # arrival you can't board. Some feeds (SF Bay Ferry) also omit
    # trip_headsign, so the last stop's name becomes the destination.
    last: dict[str, tuple[int, str]] = {}
    ours: list[tuple[str, int, str, str]] = []
    for r in _rows(z, "stop_times.txt"):
        tid = r["trip_id"]
        if tid not in trips:
            continue
        seq = int(r["stop_sequence"])
        if tid not in last or seq > last[tid][0]:
            last[tid] = (seq, r["stop_id"])
        if r["stop_id"] in stops:
            t = r.get("departure_time") or r.get("arrival_time")
            if t:                                     # non-timepoint rows may be blank in some feeds
                ours.append((tid, seq, r["stop_id"], t))

    need_names = {last[tid][1] for tid, *_ in ours if not trips[tid][1]}
    names = ({r["stop_id"]: r.get("stop_name", "") for r in _rows(z, "stops.txt")
              if r["stop_id"] in need_names} if need_names else {})

    out: dict[str, list[dict]] = {s: [] for s in stops}
    for tid, seq, stop, t in ours:
        if last[tid][0] == seq:
            continue                                  # trip ends here: an arrival, not a departure
        h, m, s = (int(x) for x in t.split(":"))    # GTFS allows 24:xx+ for after-midnight trips
        line, headsign = trips[tid]
        out[stop].append({
            "line": line, "line_ref": line, "dest": headsign or names.get(last[tid][1], ""),
            "arrival_ts": (midnight + timedelta(hours=h, minutes=m, seconds=s)).timestamp(),
            "realtime": False, "stop_name": None, "stop_ref": stop,
        })
    for v in out.values():
        v.sort(key=lambda a: a["arrival_ts"])
    return out


class ScheduleStore:
    """Weekly-cached GTFS zips per agency + per-day departure index."""

    def __init__(self, api_key: str, directory: str, client: httpx.AsyncClient, limiter, tz):
        self.api_key, self.dir, self.client, self.limiter, self.tz = api_key, directory, client, limiter, tz
        self._index: dict[tuple[str, date], dict[str, list[dict]]] = {}
        os.makedirs(directory, exist_ok=True)

    def _path(self, agency: str) -> str:
        return os.path.join(self.dir, f"{agency}.zip")

    async def _ensure_feed(self, agency: str) -> str | None:
        path = self._path(agency)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < MAX_AGE:
            return path
        if not self.limiter.try_acquire():
            log.warning("rate budget exhausted; using %s GTFS as-is", agency)
            return path if os.path.exists(path) else None
        try:
            r = await self.client.get(FEED_URL, params={"api_key": self.api_key, "operator_id": agency},
                                      timeout=180, follow_redirects=True)
            r.raise_for_status()
            zipfile.ZipFile(io.BytesIO(r.content)).testzip()   # don't replace a good cache with junk
            with open(path + ".tmp", "wb") as f:
                f.write(r.content)
            os.replace(path + ".tmp", path)
            log.info("downloaded %s GTFS (%.1f MB)", agency, len(r.content) / 1e6)
            self._index = {k: v for k, v in self._index.items() if k[0] != agency}
        except Exception as e:
            log.error("GTFS download for %s failed: %s", agency, e)
            if not os.path.exists(path):
                return None
        return path

    async def departures(self, agency: str, stops: set[str], day: date) -> dict[str, list[dict]]:
        key = (agency, day)
        if key not in self._index:
            path = await self._ensure_feed(agency)
            if not path:
                return {s: [] for s in stops}
            import asyncio
            # Parsing stop_times is CPU-bound (seconds on a Pi): keep it off the event loop
            self._index[key] = await asyncio.to_thread(
                lambda: departures_from_zip(zipfile.ZipFile(path), stops, day, self.tz))
            for old in [k for k in self._index if k[1] < day - timedelta(days=1)]:
                del self._index[old]
        cached = self._index[key]
        missing = stops - cached.keys()
        if missing:      # config gained stops since this day was indexed
            del self._index[key]
            return await self.departures(agency, stops, day)
        return cached
