# transit-board

A Home Assistant app that plans a Bay Area commute from 511.org real-time data.
It ranks your options (ferry > bus > BART > Uber+BART), counts down to when you
need to **leave**, using your travel time and buffer, and pushes "leave now" to your phone.
GPS and calendar context from HA steer it.

```
511.org ──(≤50 calls/hr, budgeted)──► Transit Board app :8099 ◄──(every 60s)── HA REST sensor
                                       runs on the HA host        ?at=home&commute=on&arrive_by=…
                                                                    ├─ dashboard card
                                                                    └─ "leave now" push
```

## Install (Home Assistant OS)

**Option A, from GitHub (needs the repo to be public):**
Settings → Apps → App store → ⋮ → Repositories → add this repo's URL → install **Transit Board**.

**Option B, as a local app (works for a private repo):** copy `transit_board_app/` to
`/addons/transit_board/` on the HA host (Advanced SSH & Web Terminal, or the Samba app), then
App store → ⋮ → Check for updates → **Local apps** → Transit Board → Install.

Then:
1. **Configuration tab:** paste your 511 token (free at https://511.org/open-data/token). Start the app.
2. On first start it creates `/homeassistant/transit_board/config.yaml`, which you can edit with the File editor app.
3. **Find stop codes:** open `http://<HA IP>:8099/setup`. For each option still marked `TODO`
   it searches by `stop_query` and shows live lines and destinations at each candidate.
   **✓ marks the right stop and side of the street.** Put that id in `stop:` and restart the app.
   This costs about 25–35 calls, so run it outside commute windows.
4. **HA config:** `homeassistant/` has the package (REST and calendar sensors), two automations, and the dashboard card.

## API

| Endpoint | |
|---|---|
| `GET /next` | Plan for the auto-picked direction |
| `&direction=to_home` | Force a direction |
| `&at=home` | HA person state. Picks the direction from `location_directions`, and pauses background polling once you've left the origin zone |
| `&commute=off` | WFH or holiday: no background 511 calls |
| `&arrive_by=2026-11-12T10:55` | Calendar deadline: picks the most-preferred option that arrives **on time** |
| `GET /setup` | Stop-code finder (plain text) |
| `GET /peek?agency=AC&stop=55555` | Live, unfiltered departures at one stop |
| `GET /health` | Budget used, expected calls/hr, missing stop codes, last HA hint |

Direction is picked in this order: `direction`, then `at`, then the commute window, then `switch_hour`.

### How the recommendation works
Each option's *next catchable* departure gets
`leave_in = mins_to_departure − travel − buffer` and `eta = departure + ride + egress`.
- **No deadline:** pick the highest-priority option whose ETA is within `slack_minutes` (15) of the fastest.
- **With `arrive_by`:** pick the highest-priority option that's on time. If none is, pick the earliest arrival and flag it late.
- `backup` is what the same logic picks with the recommendation removed.

## Rate budget
511 allows 60 calls/hr per token by default. Stops refresh per mode (bus 4 min, ferry/BART 10 min),
and shared stops are fetched once. A full morning (5 stops) is about 48 calls/hr against a budget of 50.
Background polling only runs inside commute windows, and stops when HA says you've left home or it's a WFH day.
On HTTP 429 the app backs off for 15 min and serves cached data marked `stale`.

## Development
```bash
pip install -r transit_board_app/requirements.txt -r requirements-dev.txt
pytest -q          # mocked 511 + fake clock: no network, no quota
```
