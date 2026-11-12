"""
Tests run against a mocked 511 (httpx.MockTransport) and a fake clock:
deterministic, no network, no API quota.
"""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from transit_board.config import Config, Option, Window, _option, _parse_time, load_config
from transit_board.poller import Poller, RateLimiter
from transit_board.siri import decode_511, parse_stop_monitoring

TZ = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=TZ).timestamp()  # Tue 08:00, in morning window


def iso(offset_min: float) -> str:
    return datetime.fromtimestamp(T0 + offset_min * 60, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def siri(visits: list[dict]) -> bytes:
    """511-shaped StopMonitoring body, BOM included."""
    body = {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": [
        {"MonitoredVehicleJourney": {
            "LineRef": v["line"], "PublishedLineName": v["line"],
            "DestinationName": v.get("dest", "San Francisco"),
            "MonitoredCall": {"StopPointName": "Stop", **v["call"]}}}
        for v in visits]}}}
    return b"\xef\xbb\xbf" + json.dumps(body).encode()


def dep(line, at_min, dest="San Francisco"):
    return {"line": line, "dest": dest, "call": {"ExpectedDepartureTime": iso(at_min)}}


class FakeClock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def make_cfg(**kw) -> Config:
    """Mirrors the real commute: ferry > O/W bus > BART > Uber+BART."""
    opt = lambda **o: _option(o)
    cfg = Config(
        api_key="test", tz=TZ,
        directions={
            "to_office": [
                opt(name="Ferry", mode="ferry", priority=1, agency="SB", stop="FERRY",
                    travel_minutes=8, ride_minutes=20, egress_minutes=10),
                opt(name="O bus", mode="bus", priority=2, agency="AC", stop="O",
                    lines=["O"], travel_minutes=5, ride_minutes=40, egress_minutes=8),
                opt(name="W bus", mode="bus", priority=2, agency="AC", stop="W",
                    lines=["W"], travel_minutes=5, ride_minutes=40, egress_minutes=8),
                opt(name="BART", mode="bart", priority=3, agency="BA", stop="FTVL",
                    dest_match=["Daly City"], travel_minutes=12, ride_minutes=16, egress_minutes=5),
                opt(name="Uber+BART", mode="bart", priority=4, agency="BA", stop="WOAK",
                    travel_minutes=20, ride_minutes=8, egress_minutes=5),
            ],
            "to_home": [
                opt(name="BART Fruitvale", mode="bart", priority=1, agency="BA", stop="MONT",
                    dest_match=["Berryessa", "Dublin"], ride_minutes=16),
                opt(name="BART WOAK+Uber", mode="bart", priority=2, agency="BA", stop="MONT",
                    ride_minutes=8, egress_minutes=20),
            ],
        },
        windows=[Window("to_office", _parse_time("07:00"), _parse_time("10:00")),
                 Window("to_home", _parse_time("16:30"), _parse_time("19:30"))],
        location_directions={"home": "to_office"},
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def mock_511(responses: dict[str, bytes], calls: list | None = None):
    calls = [] if calls is None else calls
    def handler(req):
        stop = req.url.params["stopcode"]
        calls.append(stop)
        return httpx.Response(200, content=responses.get(stop, siri([])))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def planned(responses, arrive_by=None, direction="to_office", **cfg_kw):
    p = Poller(make_cfg(**cfg_kw), client=mock_511(responses), clock=FakeClock(T0))
    await p.refresh(direction)
    return p.plan(direction, arrive_by)


# ─── Parsing ──────────────────────────────────────────────────────────────

def test_bom_breaks_plain_json_but_decode_511_handles_it():
    with pytest.raises(json.JSONDecodeError):
        json.loads(siri([]).decode("utf-8"))
    assert "ServiceDelivery" in decode_511(siri([]))


def test_parse_prefers_realtime_departure():
    [a, b, c] = parse_stop_monitoring(decode_511(siri([
        {"line": "F", "call": {"ExpectedArrivalTime": iso(3), "ExpectedDepartureTime": iso(5)}},
        {"line": "B", "call": {"AimedArrivalTime": iso(10)}},
        {"line": "R", "call": {"ExpectedArrivalTime": iso(7), "AimedDepartureTime": iso(6)}},
    ])))
    assert a["arrival_ts"] == T0 + 300 and a["realtime"]      # departure beats arrival
    assert not b["realtime"]
    assert c["arrival_ts"] == T0 + 420 and c["realtime"]      # realtime beats schedule


def test_parse_single_object_instead_of_list():
    payload = {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": {
        "MonitoredVehicleJourney": {"LineRef": "O", "DestinationName": ["SF"],
                                    "MonitoredCall": {"ExpectedArrivalTime": iso(4)}}}}}}
    [a] = parse_stop_monitoring(payload)
    assert a["line"] == "O" and a["dest"] == "SF"


# ─── Config ───────────────────────────────────────────────────────────────

def test_mode_defaults_for_buffers_and_refresh():
    f = _option({"name": "F", "mode": "ferry", "priority": 1, "agency": "SB", "stop": "1"})
    b = _option({"name": "B", "mode": "bus", "priority": 2, "agency": "AC", "stop": "2"})
    o = _option({"name": "B", "mode": "bus", "priority": 2, "agency": "AC", "stop": "2",
                 "buffer_minutes": 1})
    assert (f.buffer_minutes, f.refresh_seconds) == (3, 600)
    assert (b.buffer_minutes, b.refresh_seconds) == (2, 240)
    assert o.buffer_minutes == 1                                  # explicit overrides default


def test_shared_stop_fetched_once_and_budget_fits():
    cfg = make_cfg()
    assert list(cfg.stops("to_home")) == ["BA:MONT"]
    # ferry 6 + O 15 + W 15 + FTVL 6 + WOAK 6 = 48/hr, under the 50 budget
    assert round(cfg.calls_per_hour("to_office")) == 48


def test_shipped_config_yaml_loads(monkeypatch):
    monkeypatch.setenv("API_KEY_511", "x")
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "transit_board_app", "default_config.yaml"))
    # Shipped config has every stop resolved: nothing pending, all 12 options polled
    assert cfg.pending == [] and sum(len(v) for v in cfg.directions.values()) == 12
    office = {o.name: o for o in cfg.directions["to_office"]}
    assert list(office)[0] == "Ferry" and office["Ferry"].stop == "7207"
    assert (office["O bus"].buffer_minutes, office["O bus"].travel_minutes) == (2, 13)
    assert office["Uber → West Oakland BART"].fallback
    assert office["Ferry"].always_prefer and not office["Harbor Bay ferry"].always_prefer
    assert [o.name for o in cfg.directions["to_office"]] == [
        "Ferry", "O bus", "W bus", "Harbor Bay ferry", "BART (Fruitvale)", "Uber → West Oakland BART"]
    assert office["Harbor Bay ferry"].travel_minutes == 27
    assert cfg.location_directions == {"home": "to_office", "office": "to_home"}
    assert (cfg.default_arrive_by, cfg.default_home_by) == ("09:30", "17:05")
    # budget check from the docs: ~46 calls/hr mornings, ~38 evenings, under 50
    assert (round(cfg.calls_per_hour("to_office")), round(cfg.calls_per_hour("to_home"))) == (46, 38)


@pytest.mark.parametrize("hhmm,day,at,expected", [
    ("08:00", 10, None, ("to_office", "window")),
    ("17:00", 10, None, ("to_home", "window")),
    ("13:00", 10, None, ("to_home", "clock")),
    ("17:00", 10, "home", ("to_office", "location:home")),  # still home at 5pm → show outbound
    ("08:00", 10, "not_home", ("to_office", "window")),      # unknown zone → ignored
])
def test_resolve_direction(hhmm, day, at, expected):
    h, m = map(int, hhmm.split(":"))
    assert make_cfg().resolve_direction(datetime(2026, 11, day, h, m, tzinfo=TZ), None, at) == expected


# ─── Recommendation ───────────────────────────────────────────────────────

async def test_ferry_preferred_when_within_slack():
    # Ferry departs +25: leave_in 25-8-3=14, ETA +55. O departs +10: leave_in 3, ETA +58.
    r = await planned({"FERRY": siri([dep("SEA", 25)]), "O": siri([dep("O", 10)])})
    assert r["recommended"]["option"] == "Ferry"
    assert r["recommended"]["leave_in"] == 14 and r["recommended"]["reason"] == "preferred"
    assert r["next_leave_in"] == 14


async def test_falls_back_to_bus_when_ferry_far_off():
    # Ferry +60 → ETA +90; O +10 → ETA +58. 32 min worse > 15 slack → bus.
    r = await planned({"FERRY": siri([dep("SEA", 60)]), "O": siri([dep("O", 10)])})
    assert r["recommended"]["option"] == "O bus"
    assert "arrive >15 min later" in r["recommended"]["reason"]


async def test_o_and_w_compete_on_eta():
    r = await planned({"O": siri([dep("O", 20)]), "W": siri([dep("W", 12)])})
    assert r["recommended"]["option"] == "W bus"


async def test_buffers_hide_uncatchable_departures():
    # Ferry in 10: needs 8 travel + 3 buffer = 11 → missed. Next at +40.
    r = await planned({"FERRY": siri([dep("SEA", 10), dep("SEA", 40)])})
    ferry = r["options"][0]
    assert ferry["next"]["mins"] == 40 and ferry["next"]["leave_in"] == 29


async def test_line_and_destination_filters():
    r = await planned({
        "O": siri([dep("OX", 6), dep("O", 15)]),                      # OX filtered out
        "FTVL": siri([dep("Orange", 20, "Richmond"), dep("Green", 25, "Daly City")]),
    })
    opts = {o["option"]: o for o in r["options"]}
    assert opts["O bus"]["next"]["line"] == "O"
    assert opts["BART"]["next"]["dest"] == "Daly City"


async def test_calendar_deadline_picks_preferred_on_time_option():
    deadline = datetime.fromtimestamp(T0, TZ) + timedelta(minutes=60)
    # Ferry +45 → ETA +75 (late); BART +20 → ETA +41 (on time); O +10 → ETA +58 (on time)
    r = await planned({"FERRY": siri([dep("SEA", 45)]), "O": siri([dep("O", 10)]),
                       "FTVL": siri([dep("G", 20, "Daly City")])}, arrive_by=deadline)
    assert r["recommended"]["option"] == "O bus"                 # priority 2 beats BART's 3
    assert r["recommended"]["on_time"] is True
    assert r["options"][0]["next"]["on_time"] is False           # ferry flagged late
    assert r["backup"]["option"] == "BART"                       # on-time beats the late ferry


async def test_calendar_deadline_nothing_on_time_picks_earliest():
    deadline = datetime.fromtimestamp(T0, TZ) + timedelta(minutes=30)
    r = await planned({"O": siri([dep("O", 10)]), "FTVL": siri([dep("G", 20, "Daly City")])},
                      arrive_by=deadline)
    assert r["recommended"]["option"] == "BART"                  # ETA +41 vs +58
    assert r["recommended"]["reason"].startswith("running late: earliest arrival, ~11 min")


async def test_shared_stop_split_by_destination():
    r = await planned({"MONT": siri([dep("Yellow", 6, "Antioch"),
                                     dep("Green", 9, "Berryessa")])}, direction="to_home")
    opts = {o["option"]: o for o in r["options"]}
    assert opts["BART Fruitvale"]["next"]["dest"] == "Berryessa"
    assert opts["BART WOAK+Uber"]["next"]["dest"] == "Antioch"   # any eastbound


async def test_minutes_tick_without_refetch():
    clk = FakeClock(T0)
    p = Poller(make_cfg(), client=mock_511({"O": siri([dep("O", 15)])}), clock=clk)
    await p.refresh("to_office")
    assert p.plan("to_office")["options"][1]["next"]["leave_in"] == 8
    clk.t += 180
    assert p.plan("to_office")["options"][1]["next"]["leave_in"] == 5


# ─── HA hints gate polling ────────────────────────────────────────────────

def test_hints_pause_and_resume_polling():
    clk = FakeClock(T0)
    p = Poller(make_cfg(), client=mock_511({}), clock=clk)
    assert p.should_poll("to_office")                  # no hint → fail open
    p.set_hint("home", True)
    assert p.should_poll("to_office")
    p.set_hint("not_home", True)
    assert not p.should_poll("to_office")              # left home → stop spending budget
    assert p.should_poll("to_home")                    # no office zone configured → unaffected
    p.set_hint("home", False)
    assert not p.should_poll("to_office")              # WFH/holiday
    clk.t += 601
    assert p.should_poll("to_office")                  # hint expired → fail open


# ─── Rate limiting ────────────────────────────────────────────────────────

def test_rate_limiter_sliding_window():
    clk = FakeClock(0.0)
    rl = RateLimiter(limit=3, window=3600, clock=clk)
    assert all(rl.try_acquire() for _ in range(3)) and not rl.try_acquire()
    clk.t = 3601
    assert rl.try_acquire() and rl.used == 1


async def test_cache_and_budget_cap_calls():
    clk, calls = FakeClock(T0), []
    p = Poller(make_cfg(request_budget_per_hour=6), client=mock_511({}, calls), clock=clk)
    await p.refresh("to_office")
    await p.refresh("to_office")
    assert len(calls) == 5                            # 5 unique stops, second call cached
    clk.t += 241                                      # buses (240s) due, ferry/BART (600s) not
    await p.refresh("to_office")
    assert len(calls) == 6 and p.limiter.used == 6    # 2 due, budget allowed only 1


async def test_429_backoff_keeps_old_data():
    clk, n = FakeClock(T0), {"i": 0}
    def handler(req):
        n["i"] += 1
        return (httpx.Response(200, content=siri([dep("O", 20)])) if n["i"] == 1
                else httpx.Response(429))
    cfg = make_cfg()
    cfg.directions = {"to_office": cfg.directions["to_office"][1:2]}  # just the O bus
    p = Poller(cfg, clock=clk, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await p.refresh("to_office")
    clk.t += 241
    await p.refresh("to_office")
    r = p.plan("to_office")
    assert r["recommended"]["line"] == "O" and "429" in r["errors"][0]
    assert not p.limiter.try_acquire()


# ─── 511 robustness + setup ───────────────────────────────────────────────

async def test_visits_for_other_stops_are_dropped():
    """If 511 ignored the stop filter and answered agency-wide, other stops' buses
    must not leak into this stop's departures."""
    body = json.loads(siri([dep("O", 10), dep("X", 5)])[3:])
    visits = body["ServiceDelivery"]["StopMonitoringDelivery"]["MonitoredStopVisit"]
    visits[0]["MonitoredVehicleJourney"]["MonitoredCall"]["StopPointRef"] = "O"
    visits[1]["MonitoredVehicleJourney"]["MonitoredCall"]["StopPointRef"] = "99999"
    p = Poller(make_cfg(), clock=FakeClock(T0), client=mock_511({"O": json.dumps(body).encode()}))
    await p.refresh("to_office")
    assert [a["line"] for a in p.cache["AC:O"].arrivals] == ["O"]


async def test_setup_report_marks_matching_stop():
    from transit_board.setup import setup_report
    cfg = make_cfg()
    bus = _option({"name": "O bus", "mode": "bus", "priority": 2, "agency": "AC",
                   "stop": "TODO", "stop_query": "santa clara bay", "lines": ["O"],
                   "dest_match": ["San Francisco"]})
    cfg.pending = [("to_office", bus)]
    stops = {"Contents": {"dataObjects": {"ScheduledStopPoint": [
        {"id": "55001", "Name": "Santa Clara Av & Bay St"},
        {"id": "55002", "Name": "Santa Clara Av:Bay St"},
        {"id": "55003", "Name": "Santa Clara Av & Grand St"}]}}}

    def handler(req):
        if req.url.path.endswith("/stops"):
            return httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps(stops).encode())
        sc = req.url.params["stopcode"]
        return httpx.Response(200, content=siri(
            [dep("O", 9, "San Francisco")] if sc == "55001" else [dep("O", 4, "Fruitvale BART")]))

    p = Poller(cfg, clock=FakeClock(T0), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    report = await setup_report(cfg, p.client, p.limiter)
    assert "55003" not in report                                   # Grand St filtered out
    assert "✓ O → San Francisco" in report                         # SF-bound side marked
    assert "  O → Fruitvale BART" in report                        # other side unmarked
    assert p.limiter.used == 3                                     # 1 stops list + 2 peeks


# ─── Remote overrides (app Configuration tab) ─────────────────────────────

def test_overrides_set_stop_codes_and_times(monkeypatch):
    import json as _json
    monkeypatch.setenv("API_KEY_511", "x")
    monkeypatch.setenv("TRANSIT_OVERRIDES", _json.dumps([
        "to_office/Ferry.stop=12345",
        "to_office/Ferry.travel_minutes=10",
        "to_office/O bus.lines=O, OX",
        "slack_minutes=10",
        "to_office/Nope.stop=1",          # bad: unknown option → logged, ignored
        "garbage",                        # bad: no '='
    ]))
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "transit_board_app", "default_config.yaml"))
    office = {o.name: o for o in cfg.directions["to_office"]}
    ferry = office["Ferry"]
    assert (ferry.stop, ferry.travel_minutes, ferry.buffer_minutes) == ("12345", 10, 3)
    assert cfg.slack_minutes == 10
    assert office["O bus"].lines == ("O", "OX")


def test_short_lineref_used_for_display():
    payload = {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": [
        {"MonitoredVehicleJourney": {"LineRef": "O", "PublishedLineName": "Santa Clara - Encinal Transbay",
                                     "DestinationName": "Salesforce Transit Center Bay 28",
                                     "MonitoredCall": {"ExpectedArrivalTime": iso(4)}}},
        {"MonitoredVehicleJourney": {"LineRef": "Blue-N", "PublishedLineName": "Daly City to Dublin/Pleasanton",
                                     "DestinationName": "Dublin / Pleasanton",
                                     "MonitoredCall": {"ExpectedArrivalTime": iso(6)}}}]}}}
    ac, bart = parse_stop_monitoring(payload)
    assert (ac["line"], ac["line_name"]) == ("O", "Santa Clara - Encinal Transbay")
    assert bart["line"] == "Daly City to Dublin/Pleasanton"      # long ref → published name
    opt = _option({"name": "O", "mode": "bus", "priority": 2, "agency": "AC", "stop": "1",
                   "lines": ["O"], "dest_match": ["Transit"]})
    assert opt.accepts(ac)


async def test_fallback_never_sets_baseline_but_rescues():
    p = Poller(make_cfg(), client=mock_511({
        "FERRY": siri([dep("SEA", 25)]),          # ETA +55
        "O": siri([dep("O", 10)]),                # ETA +58
        "WOAK": siri([dep("Y", 25)]),             # Uber+BART: leave_in 2, ETA +38 (fastest)
    }), clock=FakeClock(T0))
    p.cfg.directions["to_office"][4] = _option({
        "name": "Uber+BART", "mode": "bart", "priority": 4, "agency": "BA", "stop": "WOAK",
        "travel_minutes": 20, "ride_minutes": 8, "egress_minutes": 5, "fallback": True})
    await p.refresh("to_office")
    r = p.plan("to_office")
    # Uber is 17 min faster than the ferry (> 15 slack), but as a fallback it can't knock it out
    assert r["options"][4]["next"]["leave_in"] == 2      # Uber IS catchable
    assert r["recommended"]["option"] == "Ferry"

    # Only the fallback makes the deadline → it's recommended, and says why
    r = p.plan("to_office", datetime.fromtimestamp(T0, TZ) + timedelta(minutes=40))
    assert r["recommended"]["option"] == "Uber+BART"
    assert r["recommended"]["reason"] == "only the fallback arrives on time"


# ─── Home Assistant sensor publishing ─────────────────────────────────────

def mock_ha(states: dict, posted: list):
    """Fake Core REST API: GET /states/<id> from `states` (missing → 404); record POSTs."""
    def handler(req):
        eid = req.url.path.rsplit("/", 1)[-1]
        if req.method == "POST":
            posted.append((eid, json.loads(req.content)))
            return httpx.Response(200, json={})
        if eid not in states:
            return httpx.Response(404, json={"message": "Entity not found."})
        return httpx.Response(200, json={"entity_id": eid, "state": states[eid]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_publish_reads_hints_and_writes_sensor():
    from transit_board.app import build_plan
    from transit_board.ha import Entities, HomeAssistant, publish_once
    clk = FakeClock(T0)
    p = Poller(make_cfg(), clock=clk, client=mock_511({"FERRY": siri([dep("SEA", 25)]),
                                                       "O": siri([dep("O", 10)])}))
    deadline = datetime.fromtimestamp(T0 + 90 * 60, ZoneInfo("UTC")).isoformat()  # HA timestamp sensors are UTC
    posted = []
    ha = HomeAssistant(mock_ha({"person.me": "home", "input_boolean.commute_today": "on",
                                "sensor.commute_arrive_by": deadline}, posted), base="http://ha/api")
    ent = Entities(person="person.me")

    await publish_once(ha, ent, lambda **h: build_plan(p, **h))

    [(eid, body)] = posted
    assert eid == "sensor.commute_leave_in"
    assert body["state"] == 14                                   # ferry: 25 - 8 travel - 3 buffer
    a = body["attributes"]
    assert a["recommended"]["option"] == "Ferry" and a["icon"] == "mdi:ferry"
    assert a["direction"] == "to_office" and a["direction_source"] == "location:home"
    assert a["arrive_by"] is not None                            # deadline passed through while home
    assert (a["unit_of_measurement"], a["device_class"]) == ("min", "duration")
    assert "next_leave_in" not in a                              # that's the state, not an attribute


async def test_publish_with_missing_entities_and_nothing_catchable():
    from transit_board.app import build_plan
    from transit_board.ha import Entities, HomeAssistant, publish_once
    p = Poller(make_cfg(), clock=FakeClock(T0), client=mock_511({}))
    posted = []
    # No person configured, no toggle, no calendar sensor: all hints fall back
    ha = HomeAssistant(mock_ha({}, posted), base="http://ha/api")
    await publish_once(ha, Entities(), lambda **h: build_plan(p, **h))
    [(_, body)] = posted
    assert body["state"] == "unavailable"
    assert body["attributes"]["direction_source"] == "window"


async def test_left_home_pauses_polling_and_drops_deadline():
    from transit_board.ha import Entities, HomeAssistant, read_hints
    ha = HomeAssistant(mock_ha({"person.me": "not_home", "input_boolean.commute_today": "on",
                                "sensor.commute_arrive_by": "2026-11-10T17:55:00+00:00"}, []),
                       base="http://ha/api")
    h, _ = await read_hints(ha, Entities(person="person.me"))
    assert h == {"at": "not_home", "commute": True, "arrive_by": None}


# ─── Calendar context (synthetic events shaped like a multi-office calendar) ─

from transit_board.calendar_ctx import CalendarRules, calendar_context

WORK, HOL = "calendar.work", "calendar.holidays"
RULES = CalendarRules(work_calendars=[WORK], holiday_calendar=HOL)
TUE_0700 = datetime(2026, 11, 10, 7, 0, tzinfo=TZ)


def ev(summary, start, end=None, location=None, description=None):
    e = {"summary": summary, "start": start, "end": end or start}
    if location: e["location"] = location
    if description: e["description"] = description
    return e


def test_hybrid_sf_room_counts_other_office_rooms_dont():
    events = {WORK: [
        ev("Standup", "2026-11-10T09:00:00-08:00", location="https://zoom.us/j/1"),
        ev("Planning", "2026-11-10T10:00:00-08:00", location="NYC-2-Room (8) [VC], LON-1-Room (4) [VC]"),
        ev("Brainstorm", "2026-11-10T11:00:00-08:00",
           location="https://zoom.us/j/2, NYC-6-Room (8) [VC], SF-10-Room (6) [VC]"),
    ]}
    c = calendar_context(events, RULES, TUE_0700)
    assert c["commute_today"] is True
    assert c["arrive_by"] == "2026-11-10T10:55:00-08:00"          # 5 min before the hybrid SF meeting
    assert c["first_in_person"].startswith("11:00 Brainstorm")


def test_sf_addresses_count_and_past_events_skipped():
    events = {WORK: [
        ev("Breakfast", "2026-11-10T06:30:00-08:00", location="1 Main St, San Francisco, CA"),  # already started
        ev("Offsite", "2026-11-10T09:00:00-08:00", location="Studio at 500 Market: short walk"),
    ]}
    assert calendar_context(events, RULES, TUE_0700)["arrive_by"] == "2026-11-10T08:55:00-08:00"


def test_public_holiday_is_flagged_not_off_and_observance_ignored():
    hol = {HOL: [ev("Veterans Day", "2026-11-10", "2026-11-11", description="Public holiday")]}
    c = calendar_context(hol, RULES, TUE_0700)
    # Startup default: still a commute day; the holiday is flagged so HA can ask
    assert (c["commute_today"], c["holiday"]) == (True, "Veterans Day")
    assert "ask" in c["reason"]
    obs = {HOL: [ev("Black Friday", "2026-11-10", "2026-11-11", description="Observance\nTo hide observances")]}
    c = calendar_context(obs, RULES, TUE_0700)
    assert (c["commute_today"], c["holiday"]) == (True, None)
    # OOO on a holiday is still off, and nobody needs asking
    ooo = {**hol, WORK: [ev("OOO", "2026-11-10", "2026-11-11")]}
    assert calendar_context(ooo, RULES, TUE_0700)["commute_today"] is False


def test_ooo_needs_all_day_or_long_block():
    short = {WORK: [ev("PTO planning", "2026-11-10T09:00:00-08:00", "2026-11-10T09:30:00-08:00")]}
    assert calendar_context(short, RULES, TUE_0700)["commute_today"] is True
    allday = {WORK: [ev("OOO", "2026-11-09", "2026-11-12")]}               # multi-day, covers today
    assert calendar_context(allday, RULES, TUE_0700)["commute_today"] is False
    long = {WORK: [ev("Out of office", "2026-11-10T08:00:00-08:00", "2026-11-10T17:00:00-08:00")]}
    assert calendar_context(long, RULES, TUE_0700)["commute_today"] is False


def test_weekend_is_not_a_commute_day():
    sat = datetime(2026, 11, 14, 8, 0, tzinfo=TZ)
    assert calendar_context({WORK: []}, RULES, sat)["reason"] == "weekend"


async def test_publish_uses_calendar_and_toggle_can_veto():
    from transit_board.app import build_plan
    from transit_board.ha import CalendarCache, Entities, HomeAssistant, publish_once
    clk = FakeClock(T0)                                            # Tue 08:00
    p = Poller(make_cfg(), clock=clk, client=mock_511({"O": siri([dep("O", 10)])}))
    posted, toggle = [], {"state": "on"}

    def handler(req):
        if req.url.path.endswith("/services/calendar/get_events"):
            assert "return_response" in str(req.url)
            return httpx.Response(200, json={"changed_states": [], "service_response": {
                WORK: {"events": [ev("Design review", "2026-11-10T09:30:00-08:00", location="SF-4-Room (6)")]},
                HOL: {"events": []}}})
        eid = req.url.path.rsplit("/", 1)[-1]
        if req.method == "POST":
            posted.append(json.loads(req.content)); return httpx.Response(200, json={})
        states = {"person.me": "home", "input_boolean.commute_today": toggle["state"]}
        return (httpx.Response(200, json={"state": states[eid]}) if eid in states
                else httpx.Response(404, json={}))

    ha = HomeAssistant(httpx.AsyncClient(transport=httpx.MockTransport(handler)), base="http://ha/api")
    cal = CalendarCache(RULES)
    now = datetime.fromtimestamp(T0, TZ)
    await publish_once(ha, Entities(person="person.me"), lambda **h: build_plan(p, **h), cal, now)
    a = posted[-1]["attributes"]
    assert a["commute_today"] is True and a["arrive_by"] == "2026-11-10T09:25-08:00"
    assert a["calendar"]["first_in_person"].startswith("09:30 Design review")
    assert a["polling_paused"] is False

    toggle["state"] = "off"                                        # sick day the calendar doesn't know
    await publish_once(ha, Entities(person="person.me"), lambda **h: build_plan(p, **h), cal, now)
    a = posted[-1]["attributes"]
    assert a["commute_today"] is False and a["polling_paused"] is True


# ─── Night-before planning: GTFS + tomorrow planner ──────────────────────

import zipfile as _zip
from datetime import date as _date


def gtfs_zip(path, stop_times, trips, routes, calendar=None, calendar_dates=None):
    """Write a tiny GTFS zip. Each arg is a list of dicts → CSV."""
    def csvtext(rows):
        cols = list(rows[0]) if rows else ["x"]
        return "﻿" + ",".join(cols) + "\n" + "".join(",".join(str(r[c]) for c in cols) + "\n" for r in rows)
    with _zip.ZipFile(path, "w") as z:
        z.writestr("stop_times.txt", csvtext(stop_times))
        z.writestr("trips.txt", csvtext(trips))
        z.writestr("routes.txt", csvtext(routes))
        if calendar is not None: z.writestr("calendar.txt", csvtext(calendar))
        if calendar_dates is not None: z.writestr("calendar_dates.txt", csvtext(calendar_dates))
    return path


WED = _date(2026, 11, 11)
CAL_ALL = [{"service_id": "WK", "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1, "friday": 1,
            "saturday": 0, "sunday": 0, "start_date": "20260101", "end_date": "20261231"}]


def test_gtfs_services_exceptions_and_after_midnight(tmp_path):
    from transit_board.schedule import active_services, departures_from_zip
    p = gtfs_zip(tmp_path / "f.zip",
        stop_times=[{"trip_id": "t1", "arrival_time": "08:10:00", "departure_time": "08:12:00", "stop_id": "S1", "stop_sequence": 1},
                    {"trip_id": "t2", "arrival_time": "25:05:00", "departure_time": "25:05:00", "stop_id": "S1", "stop_sequence": 1},
                    {"trip_id": "t3", "arrival_time": "09:00:00", "departure_time": "09:00:00", "stop_id": "S1", "stop_sequence": 1},
                    {"trip_id": "t1", "arrival_time": "08:30:00", "departure_time": "08:30:00", "stop_id": "OTHER", "stop_sequence": 2},
                    {"trip_id": "t2", "arrival_time": "25:30:00", "departure_time": "25:30:00", "stop_id": "OTHER", "stop_sequence": 2},
                    {"trip_id": "t3", "arrival_time": "09:30:00", "departure_time": "09:30:00", "stop_id": "OTHER", "stop_sequence": 2}],
        trips=[{"trip_id": "t1", "route_id": "r1", "service_id": "WK", "trip_headsign": "San Francisco"},
               {"trip_id": "t2", "route_id": "r1", "service_id": "WK", "trip_headsign": "Owl"},
               {"trip_id": "t3", "route_id": "r1", "service_id": "HOL", "trip_headsign": "Holiday only"}],
        routes=[{"route_id": "r1", "route_short_name": "O", "route_long_name": "Santa Clara"}],
        calendar=CAL_ALL,
        calendar_dates=[{"service_id": "HOL", "date": "20261111", "exception_type": 1}])
    z = _zip.ZipFile(p)
    assert active_services(z, WED) == {"WK", "HOL"}
    deps = departures_from_zip(z, {"S1"}, WED, TZ)["S1"]
    times = [datetime.fromtimestamp(d["arrival_ts"], TZ).strftime("%m-%d %H:%M") for d in deps]
    assert times == ["11-11 08:12", "11-11 09:00", "11-12 01:05"]   # departure_time; 25:05 → next day
    assert deps[0]["line"] == "O" and deps[0]["dest"] == "San Francisco" and not deps[0]["realtime"]


def _sched(*mins_by_stop):
    """{stop_key: [dep at 06:00 + m minutes, ...]} on Thu 2026-11-12."""
    base = datetime(2026, 11, 12, 6, 0, tzinfo=TZ).timestamp()
    out = {}
    for key, line, dest, mins in mins_by_stop:
        out.setdefault(key, []).extend({"line": line, "line_ref": line, "dest": dest, "realtime": False,
                                        "arrival_ts": base + m * 60} for m in mins)
    return out


def test_plan_leg_latest_on_time_and_slack():
    from transit_board.tomorrow import plan_leg
    opts = make_cfg().directions["to_office"]
    opts[4] = _option({"name": "Uber+BART", "mode": "bart", "priority": 4, "agency": "BA", "stop": "WOAK",
                       "travel_minutes": 20, "ride_minutes": 8, "egress_minutes": 5, "fallback": True})
    deadline = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
    # Leave = dep − travel − buffer; ETA = dep + ride + egress (make_cfg values)
    deps = _sched(("SB:FERRY", "SEA", "SF", [60, 120]),   # 08:00: leave 07:49, ETA 08:30
                  ("AC:O", "O", "SF", [110]),             # 07:50: leave 07:43, ETA 08:38
                  ("BA:FTVL", "G", "Daly City", [130]))   # 08:10: leave 07:55, ETA 08:31 (latest leave)
    r = plan_leg(opts, deps, deadline, deadline - timedelta(hours=4), 15, TZ)
    # Ferry means leaving 6 min before the latest-leaving option: within 15 min slack → ferry
    assert (r["option"], r["depart"][11:16], r["leave_at"][11:16]) == ("Ferry", "08:00", "07:49")
    assert r["reason"] == "preferred" and r["on_time"]
    assert (r["backup"]["option"], r["backup"]["depart"][11:16]) == ("O bus", "07:50")

    # Slack 5: ferry (6 min earlier) and O (12 min) are out → BART, leave as late as possible
    r = plan_leg(opts, deps, deadline, deadline - timedelta(hours=4), 5, TZ)
    assert (r["option"], r["leave_at"][11:16]) == ("BART", "07:55")


def test_plan_leg_fallback_and_late():
    from transit_board.tomorrow import plan_leg
    opts = make_cfg().directions["to_office"]
    opts[4] = _option({"name": "Uber+BART", "mode": "bart", "priority": 4, "agency": "BA", "stop": "WOAK",
                       "travel_minutes": 20, "ride_minutes": 8, "egress_minutes": 5, "fallback": True})
    deadline = datetime(2026, 11, 12, 6, 50, tzinfo=TZ)
    deps = _sched(("AC:O", "O", "SF", [20]), ("BA:WOAK", "Y", "SFO", [30]))   # O ETA 07:08; Uber ETA 06:43
    r = plan_leg(opts, deps, deadline, deadline - timedelta(hours=4), 15, TZ)
    assert r["option"] == "Uber+BART" and r["reason"] == "only the fallback arrives on time"
    late = plan_leg(opts, _sched(("AC:O", "O", "SF", [20])), deadline, deadline - timedelta(hours=4), 15, TZ)
    assert late["reason"].startswith("running late") and "18 min" in late["reason"]


async def test_build_tomorrow_targets_from_calendar():
    from transit_board.tomorrow import build_tomorrow
    cfg = make_cfg(default_arrive_by="09:30", default_home_by="18:30", home_location_regex="Alameda",
                   location_directions={"home": "to_office", "office": "to_home"})
    rules = CalendarRules(work_calendars=[WORK], holiday_calendar=HOL)
    events = {WORK: [ev("Design review", "2026-11-12T08:30:00-08:00", location="SF-4-Room (6)"),
                     ev("Kids' recital", "2026-11-12T17:30:00-08:00", location="School Hall, Alameda, CA"),
                     ev("Dinner", "2026-11-12T19:30:00-08:00", location="Somewhere, Oakland")],
              HOL: []}

    async def get_events(cals, start, end):
        assert start.date() == _date(2026, 11, 12)
        return events

    async def deps_for(agency, stops, day):
        base = datetime(2026, 11, 12, 6, 0, tzinfo=TZ).timestamp()
        mk = lambda line, dest, mins: [{"line": line, "line_ref": line, "dest": dest, "realtime": False,
                                        "arrival_ts": base + m * 60} for m in mins]
        table = {("SB", "FERRY"): mk("SEA", "SF", [60, 120]),
                 ("AC", "O"): mk("O", "SF", [90]),
                 ("BA", "MONT"): mk("G", "Berryessa", [600, 630, 660])}   # 16:00, 16:30, 17:00
        return {s: table.get((agency, s), []) for s in stops}

    now = datetime(2026, 11, 11, 20, 0, tzinfo=TZ)
    t = await build_tomorrow(cfg, deps_for, now, rules, get_events)
    assert t["date"] == "2026-11-12" and t["commute"]
    m = t["morning"]
    assert m["target"][11:16] == "08:25"                           # meeting 08:30 − 5, earlier than 09:30
    # 08:00 ferry lands 08:30 (late). 07:00 ferry means leaving 06:49, 34 min before the
    # 07:30 O (leave 07:23, ETA 08:18): beyond the 15-min slack, so the O wins.
    assert (m["option"], m["depart"][11:16], m["leave_at"][11:16]) == ("O bus", "07:30", "07:23")
    assert m["backup"]["option"] == "Ferry"
    e = t["evening"]
    assert t["home_event"] == "17:30 Kids' recital"                # Oakland dinner ignored
    assert e["target"][11:16] == "17:20"
    assert (e["option"], e["depart"][11:16]) == ("BART Fruitvale", "17:00")   # 17:00+16 = 17:16 ≤ 17:20: latest on time


async def test_build_tomorrow_day_off_and_payload():
    from transit_board.ha import tomorrow_payload
    from transit_board.tomorrow import build_tomorrow
    cfg = make_cfg()
    async def nothing(*a): return {}
    sat = await build_tomorrow(cfg, nothing, datetime(2026, 11, 13, 20, 0, tzinfo=TZ))   # Fri → Sat
    assert (sat["commute"], sat["morning"]) == (False, None)
    assert tomorrow_payload(sat)[0] == "unknown"
    state, attrs = tomorrow_payload({"commute": True, "morning": {"leave_at": "2026-11-12T07:49-08:00"}})
    assert state == "2026-11-12T07:49:00-08:00" and attrs["device_class"] == "timestamp"


async def test_schedule_store_downloads_once_and_caches(tmp_path):
    from transit_board.schedule import ScheduleStore
    src = gtfs_zip(tmp_path / "src.zip",
        stop_times=[{"trip_id": "t1", "arrival_time": "08:00:00", "departure_time": "08:00:00", "stop_id": "7207", "stop_sequence": 1},
                    {"trip_id": "t1", "arrival_time": "08:20:00", "departure_time": "08:20:00", "stop_id": "SFFB", "stop_sequence": 2}],
        trips=[{"trip_id": "t1", "route_id": "SEA", "service_id": "WK", "trip_headsign": "San Francisco Ferry Building"}],
        routes=[{"route_id": "SEA", "route_short_name": "", "route_long_name": "Alameda Seaplane"}],
        calendar=CAL_ALL)
    calls = []
    def handler(req):
        calls.append(req.url.params["operator_id"])
        return httpx.Response(200, content=open(src, "rb").read())
    store = ScheduleStore("k", str(tmp_path / "cache"), httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          RateLimiter(50), TZ)
    d1 = await store.departures("SB", {"7207"}, WED)
    d2 = await store.departures("SB", {"7207"}, WED)
    assert calls == ["SB"]                                         # one download, then cache
    assert d1 == d2 and d1["7207"][0]["line"] == "Alameda Seaplane"


def test_gtfs_terminal_skips_arrivals_and_fills_missing_headsign(tmp_path):
    """Ferry-dock shape: the dock is the first stop of outbound trips and the last stop
    of inbound ones, and the feed has no trip_headsign (as SF Bay Ferry's doesn't)."""
    from transit_board.schedule import departures_from_zip
    p = gtfs_zip(tmp_path / "ferry.zip",
        stop_times=[{"trip_id": "out", "arrival_time": "07:00:00", "departure_time": "07:00:00", "stop_id": "DOCK", "stop_sequence": 1},
                    {"trip_id": "out", "arrival_time": "07:20:00", "departure_time": "07:20:00", "stop_id": "SFFB", "stop_sequence": 2},
                    {"trip_id": "in", "arrival_time": "07:40:00", "departure_time": "07:40:00", "stop_id": "SFFB", "stop_sequence": 1},
                    {"trip_id": "in", "arrival_time": "08:00:00", "departure_time": "08:00:00", "stop_id": "DOCK", "stop_sequence": 2}],
        trips=[{"trip_id": "out", "route_id": "SEA", "service_id": "WK", "trip_headsign": ""},
               {"trip_id": "in", "route_id": "SEA", "service_id": "WK", "trip_headsign": ""}],
        routes=[{"route_id": "SEA", "route_short_name": "SEA", "route_long_name": "Alameda Seaplane"}],
        calendar=CAL_ALL)
    with _zip.ZipFile(p, "a") as z:
        z.writestr("stops.txt", "stop_id,stop_name\nDOCK,Alameda Seaplane Lagoon\nSFFB,San Francisco Ferry Building Gate F\n")
    deps = departures_from_zip(_zip.ZipFile(p), {"DOCK"}, WED, TZ)["DOCK"]
    assert len(deps) == 1                                          # the 08:00 arrival is gone
    assert deps[0]["dest"] == "San Francisco Ferry Building Gate F"
    ferry = _option({"name": "Ferry", "mode": "ferry", "priority": 1, "agency": "SB", "stop": "DOCK",
                     "dest_match": ["San Francisco", "Ferry Building"]})
    assert ferry.accepts(deps[0])                                  # the filter that silently failed before


def test_line_stops_lists_a_lines_stops(tmp_path):
    from transit_board.schedule import line_stops
    p = gtfs_zip(tmp_path / "ac.zip",
        stop_times=[{"trip_id": "w1", "arrival_time": "07:10:00", "departure_time": "07:10:00", "stop_id": "A", "stop_sequence": 1},
                    {"trip_id": "w1", "arrival_time": "07:50:00", "departure_time": "07:50:00", "stop_id": "B", "stop_sequence": 2},
                    {"trip_id": "x1", "arrival_time": "07:15:00", "departure_time": "07:15:00", "stop_id": "C", "stop_sequence": 1}],
        trips=[{"trip_id": "w1", "route_id": "rW", "service_id": "WK", "trip_headsign": "San Francisco"},
               {"trip_id": "x1", "route_id": "rX", "service_id": "WK", "trip_headsign": "Elsewhere"}],
        routes=[{"route_id": "rW", "route_short_name": "W", "route_long_name": "Broadway"},
                {"route_id": "rX", "route_short_name": "X", "route_long_name": "Other"}],
        calendar=CAL_ALL)
    with _zip.ZipFile(p, "a") as z:
        z.writestr("stops.txt", "stop_id,stop_name\nA,Central Av & 8th St\nB,Transit Center Bay 22\nC,Central Av & 9th St\n")
    r = line_stops(_zip.ZipFile(p), "W", WED, "central")
    assert [(s["stop"], s["name"], s["headsigns"]) for s in r] == [("A", "Central Av & 8th St", ["San Francisco"])]


# ─── "Always prefer the ferry" + adding options via overrides ─────────────

def _ferry_first_cfg():
    cfg = make_cfg()
    f = cfg.directions["to_office"][0]
    cfg.directions["to_office"][0] = _option({"name": f.name, "mode": "ferry", "priority": 1, "agency": "SB",
        "stop": "FERRY", "travel_minutes": 8, "ride_minutes": 20, "egress_minutes": 10, "always_prefer": True})
    return cfg


async def test_always_prefer_ferry_beats_a_much_sooner_bus():
    # Ferry in 70 min (ETA +100) vs O in 10 (ETA +58): 42 min worse, way past the 15-min slack
    p = Poller(_ferry_first_cfg(), clock=FakeClock(T0),
               client=mock_511({"FERRY": siri([dep("SEA", 70)]), "O": siri([dep("O", 10)])}))
    await p.refresh("to_office")
    r = p.plan("to_office")
    assert r["recommended"]["option"] == "Ferry" and r["backup"]["option"] == "O bus"
    # ...unless it would make you late for a deadline
    r = p.plan("to_office", datetime.fromtimestamp(T0, TZ) + timedelta(minutes=80))
    assert r["recommended"]["option"] == "O bus"


def test_always_prefer_in_night_before_plan():
    from transit_board.tomorrow import plan_leg
    opts = _ferry_first_cfg().directions["to_office"]
    deadline = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
    deps = _sched(("SB:FERRY", "SEA", "SF", [60]),        # 07:00: leave 06:49, ETA 07:30
                  ("BA:FTVL", "G", "Daly City", [130]))   # 08:10: leave 07:55
    r = plan_leg(opts, deps, deadline, deadline - timedelta(hours=4), 15, TZ)
    assert (r["option"], r["leave_at"][11:16]) == ("Ferry", "06:49")   # 66 min earlier, still the ferry


def test_clone_override_adds_an_option(monkeypatch):
    import json as _json
    monkeypatch.setenv("API_KEY_511", "x")
    monkeypatch.setenv("TRANSIT_OVERRIDES", _json.dumps([
        "to_office/Harbor Bay ferry.clone=Ferry",
        "to_office/Harbor Bay ferry.stop=7206",
        "to_office/Harbor Bay ferry.priority=3",
        "to_office/BART (Fruitvale).priority=4",
        "to_office/Uber → West Oakland BART.priority=5",
        "to_office/Harbor Bay ferry.travel_minutes=27",
        "to_office/Ferry.always_prefer=true",
        "to_office/Harbor Bay ferry.clone=Nope",          # bad source → logged, ignored
    ]))
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "transit_board_app", "default_config.yaml"))
    office = {o.name: o for o in cfg.directions["to_office"]}
    hb = office["Harbor Bay ferry"]
    assert (hb.stop, hb.priority, hb.travel_minutes, hb.mode, hb.buffer_minutes) == ("7206", 3, 27, "ferry", 3)
    assert not hb.always_prefer and office["Ferry"].always_prefer   # clone doesn't inherit the flag
    assert [o.name for o in cfg.directions["to_office"]] == [
        "Ferry", "O bus", "W bus", "Harbor Bay ferry", "BART (Fruitvale)", "Uber → West Oakland BART"]


# ─── Regression: 2026-11-12 morning, "leave now" fired for the 7:00 ferry ──
# Target was 08:55; three pushes went out (06:42 Ferry 07:00, 07:07, 07:22 O bus
# "preferred options would be late") because the live plan offered each option's
# NEXT departure, and the 08:20 ferry was beyond 511's ~1 h live horizon.

def _morning_poller(responses, sched=None, clock_min=0):
    p = Poller(_ferry_first_cfg(), clock=FakeClock(T0 + clock_min * 60), client=mock_511(responses))
    p.scheduled = sched or {}
    return p


async def test_live_offers_latest_on_time_departure_not_next():
    # T0 = 08:00 here; deadline 09:30. Ferries at +10 and +50 (ETA +40, +80 ≤ 90)
    p = _morning_poller({"FERRY": siri([dep("SEA", 10, "San Francisco"), dep("SEA", 50, "San Francisco")])})
    await p.refresh("to_office")
    r = p.plan("to_office", datetime.fromtimestamp(T0, TZ) + timedelta(minutes=90))
    assert r["recommended"]["option"] == "Ferry" and r["recommended"]["depart"][11:16] == "08:50"
    assert r["next_leave_in"] == 50 - 8 - 3       # no "leave now" for the 08:10 boat


async def test_timetable_fills_in_beyond_live_horizon():
    # Live feed only shows a ferry at +10; the on-time +50 sailing exists only in GTFS
    sched = {"SB:FERRY": [{"line": "SEA", "line_ref": "SEA", "dest": "San Francisco", "realtime": False,
                           "arrival_ts": T0 + 10 * 60}, {"line": "SEA", "line_ref": "SEA",
                           "dest": "San Francisco", "realtime": False, "arrival_ts": T0 + 50 * 60}]}
    p = _morning_poller({"FERRY": siri([dep("SEA", 10, "San Francisco")]), "O": siri([dep("O", 20)])}, sched)
    await p.refresh("to_office")
    r = p.plan("to_office", datetime.fromtimestamp(T0, TZ) + timedelta(minutes=90))
    rec = r["recommended"]
    assert (rec["option"], rec["depart"][11:16], rec["realtime"]) == ("Ferry", "08:50", False)
    assert rec["reason"] == "preferred"            # not "preferred options would be late"
    # the +10 scheduled sailing duplicates the live one and isn't added twice
    ferry = r["options"][0]
    assert ferry["later"] == []


async def test_timetable_covers_lines_with_no_live_data():
    # W publishes no live predictions at its stop; the timetable still offers it
    sched = {"AC:W": [{"line": "W", "line_ref": "W", "dest": "San Francisco", "realtime": False,
                       "arrival_ts": T0 + 30 * 60}]}
    p = _morning_poller({"W": siri([dep("30", 16, "MLK & 11th")])}, sched)   # only line 30 live
    await p.refresh("to_office")
    w = next(o for o in p.plan("to_office")["options"] if o["option"] == "W bus")
    assert w["next"]["line"] == "W" and w["next"]["realtime"] is False


def test_default_deadline_matches_night_before_targets():
    from transit_board.app import default_deadline
    cfg = make_cfg(location_directions={"home": "to_office", "office": "to_home"},
                   default_arrive_by="09:30", default_home_by="18:30")
    at = lambda h, m: datetime(2026, 11, 12, h, m, tzinfo=TZ)
    assert default_deadline(cfg, "to_office", at(7, 0)) == at(9, 30)
    assert default_deadline(cfg, "to_home", at(16, 0)) == at(18, 30)
    assert default_deadline(cfg, "to_office", at(10, 0)) is None      # already past: no deadline
