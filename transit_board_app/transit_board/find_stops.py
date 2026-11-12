"""
Find 511 stop codes by name. Costs one API call per run.

    API_KEY_511=xxx python -m transit_board.find_stops AC telegraph 40th
    API_KEY_511=xxx python -m transit_board.find_stops BA "macarthur"
    API_KEY_511=xxx python -m transit_board.find_stops --operators   # list agency codes

All search words must appear in the stop name (case-insensitive).
Bus stops are direction-specific: you'll usually see two codes for the same
intersection, one for each side of the street. Pick the side you board on
and sanity-check it with a live query (see README).
"""
import os
import sys

import httpx

from .siri import decode_511


def main(argv: list[str]) -> None:
    key = os.environ.get("API_KEY_511") or sys.exit("Set API_KEY_511")
    if not argv:
        sys.exit(__doc__)

    if argv[0] == "--operators":
        r = httpx.get("https://api.511.org/transit/operators",
                      params={"api_key": key, "format": "json"}, timeout=30)
        r.raise_for_status()
        for op in decode_511(r.content):
            print(f"{op.get('Id'):>4}  {op.get('Name')}")
        return

    agency, words = argv[0], [w.lower() for w in argv[1:]]
    r = httpx.get("https://api.511.org/transit/stops",
                  params={"api_key": key, "operator_id": agency, "format": "json"}, timeout=30)
    r.raise_for_status()
    stops = (decode_511(r.content).get("Contents", {}).get("dataObjects", {})
             .get("ScheduledStopPoint", []))
    hits = [s for s in stops if all(w in s.get("Name", "").lower() for w in words)]
    for s in sorted(hits, key=lambda s: s.get("Name", "")):
        loc = s.get("Location", {})
        print(f"{s.get('id'):>10}  {s.get('Name')}  ({loc.get('Latitude')}, {loc.get('Longitude')})")
    print(f"\n{len(hits)} match(es) of {len(stops)} {agency} stops", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
