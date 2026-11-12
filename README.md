# transit-board

A Home Assistant app that plans a Bay Area commute from 511.org real-time data.
It ranks your options (Seaplane ferry > bus > Harbor Bay ferry > BART > Uber+BART), counts down to when you
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
4. **Connect it to HA.** In the app's Configuration tab, set:
   - `person_entity`: your `person.*`, for GPS. Home means the trip to the office; zone `Office` means the trip home. Once you've left, polling pauses.
   - `work_calendars` and `holiday_calendar`: from HA's Google Calendar integration.
5. **HA side:** follow [`homeassistant/README.md`](homeassistant/README.md): a toggle helper, an `Office` zone, five automations, and the dashboard. No `configuration.yaml` changes.

## What it publishes

The app writes `sensor.commute_leave_in` every 60 s:
- **State:** minutes until you must leave for the recommended option, or `unavailable`.
- **Attributes:** `recommended`, `backup`, `options`, `direction`, `commute_today`, `arrive_by`, `stale`,
  `polling_paused`, and `calendar` (`reason`, `holiday`, `first_in_person`).

### Night before: `sensor.commute_tomorrow`
511's live feed only covers about the next hour, and its StopTimetable API only works for
"timepoint" stops (other stops return HTTP 412). So tomorrow's plan comes from each agency's **GTFS**
feed, downloaded at most weekly (1 API call each) and cached in `/data/gtfs`.
- **Morning target:** your first in-person meeting minus 5 min, capped at `default_arrive_by` (09:30).
- **Evening target:** your first evening event matching `home_location_regex` minus 10 min, capped at `default_home_by` (18:30).
- **Per option:** the planner takes the latest departure that still meets the target (leave as late as
  possible), then applies the live plan's rules: priority order, fallbacks last, 15-min slack.

The GTFS reader drops arrivals at terminals, which can't be boarded (a ferry dock lists both directions),
and when a feed has no headsigns (SF Bay Ferry's doesn't) it uses the trip's last stop as the destination.
Debug endpoints: `/tomorrow`, `/schedule?agency=SB&stop=7207&after=06:00`, `/line_stops?agency=AC&line=W&q=central`.

### Calendar rules
| Situation | Result |
|---|---|
| Workday | Commute (default). `arrive_by` = 5 min before the first upcoming in-person event |
| In-person event | Location matches `in_person_location_regex`. The default matches SF rooms (`SF-<n>…`), "San Francisco", and Market St addresses. A Zoom link next to an SF room counts |
| Public holiday | Still a commute day, but flagged as `holiday` so HA can **ask** (actionable push). Observances (e.g. Black Friday) are ignored |
| OOO / PTO / vacation / sick / WFH | Off, when it's an all-day event or a 4h+ block |
| Weekend | Off |
| `input_boolean.commute_today` off | Off for the day (it resets at 04:30) |

## API

| Endpoint | |
|---|---|
| `GET /next` | Plan for the auto-picked direction |
| `&direction=to_home` | Force a direction |
| `&at=home` | HA person state. Picks the direction from `location_directions`, and pauses background polling once you've left the origin zone |
| `&commute=off` | WFH or holiday: no background 511 calls |
| `&arrive_by=2026-11-12T10:55` | Calendar deadline: picks the most-preferred option that arrives **on time** |
| `GET /setup` | Stop-code finder (plain text) |
| `GET /tomorrow` | Tomorrow's plan, computed now |
| `GET /schedule`, `/line_stops` | GTFS timetable lookups (no API calls once cached) |
| `GET /peek?agency=AC&stop=55555` | Live, unfiltered departures at one stop |
| `GET /health` | Budget used, expected calls/hr, missing stop codes, last HA hint |

Direction is picked in this order: `direction`, then `at`, then the commute window, then `switch_hour`.

### How the recommendation works
Each option's *next catchable* departure gets
`leave_in = mins_to_departure − travel − buffer` and `eta = departure + ride + egress`.
- **`always_prefer` (the Seaplane ferry):** wins whenever it's catchable, and on time when there's a deadline. No slack applies.
- **No deadline:** pick the highest-priority option whose ETA is within `slack_minutes` (15) of the fastest.
- **With `arrive_by`:** pick the highest-priority option that's on time. If none is, pick the earliest arrival and flag it late.
- `backup` is what the same logic picks with the recommendation removed.

### Changing options remotely
The app's Configuration tab has an `overrides` list, applied on top of `config.yaml`, one per line:
```
to_office/Seaplane Ferry.travel_minutes=12        # set a field
to_office/Harbor Bay ferry.clone=Seaplane Ferry   # add an option by copying one…
to_office/Harbor Bay ferry.stop=7206              # …then set its fields
location_directions={home: to_office, office: to_home}   # top-level setting
```
Lines apply in order, so a rename (`to_office/Seaplane Ferry.name=…`) only affects the lines after it.
Bad lines are logged and skipped; they never stop the app.

## Rate budget
511 allows 60 calls/hr per token by default. Stops refresh on their own intervals (buses 4 min, ferries and BART 15 min),
and shared stops are fetched once. Six morning stops come to about 46 calls/hr (38 evenings) against a budget of 50.
Background polling only runs inside commute windows, and stops when HA says you've left home or it's a WFH day.
On HTTP 429 the app backs off for 15 min and serves cached data marked `stale`.

## Development
```bash
pip install -r transit_board_app/requirements.txt -r requirements-dev.txt
pytest -q          # mocked 511 + fake clock: no network, no quota
```
