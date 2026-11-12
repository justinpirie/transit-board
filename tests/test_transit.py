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
    # Every stop is still TODO: nothing is polled, everything waits for /setup
    assert cfg.directions["to_office"] == [] and len(cfg.pending) == 10
    names = [o.name for d, o in cfg.pending if d == "to_office"]
    assert names[0] == "Ferry" and all(o.stop_query for _, o in cfg.pending)
    bus = next(o for _, o in cfg.pending if o.name == "O bus")
    assert (bus.buffer_minutes, bus.travel_minutes) == (2, 13)


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
    [ferry] = cfg.directions["to_office"]               # only the resolved option is polled
    assert (ferry.stop, ferry.travel_minutes, ferry.buffer_minutes) == ("12345", 10, 3)
    assert cfg.slack_minutes == 10
    obus = next(o for d, o in cfg.pending if d == "to_office" and o.name == "O bus")
    assert obus.lines == ("O", "OX")


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
