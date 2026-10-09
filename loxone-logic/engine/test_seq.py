import datetime as dt
from runtime import REGISTRY, Ctx, Engine
import b_basic, b_seq  # noqa

def mk(bid, params=None, cfg=None, ctx=None): return REGISTRY[bid](bid, params, cfg, ctx)

def test_stepper_sequencer():
    s = mk("stepper", {"M": 3}); vals = []
    for _ in range(5): s.step({"S": 1}, 0.1); vals.append(s.step({"S": 0}, 0.1)["V"])
    assert vals == [1, 2, 3, 0, 1]
    q = mk("sequencer", {"Max": 3, "Dv": 1}); o = []
    for _ in range(4): q.step({"Tr": 1}, .1); o.append(q.step({"Tr": 0}, .1)["Sel"])
    assert o == [2, 3, 1, 2]
    assert q.step({"R": 1}, .1)["Sel"] == 1
    assert q.step({"R": 0, "P": 3}, .1)["O3"] == 1

def test_scene():
    s = mk("scene", cfg={"actions": [{"port": "A1", "value": 50}, {"port": "A2", "value": 0}]})
    assert s.step({"Act": 0}, .1) == {}
    assert s.step({"Act": 1}, .1) == {"A1": 50, "A2": 0}

def test_automatic_rule():
    r = mk("automatic-rule", cfg={"rules": [{"if": "I1 > 5 and not I2", "then": {"Q1": 1}, "else": {"Q1": 0}}]})
    assert r.step({"I1": 6, "I2": 0}, .1)["Q1"] == 1 and r.step({"I1": 6, "I2": 1}, .1)["Q1"] == 0

def test_sequence_controller():
    sc = mk("sequence-controller", {"Interval": 100}, {"sequences": {"1": ["AQ1=AI1*2", "WAIT 1", "AQ1=0", "END"]}})
    sc.step({"S1": 1, "AI1": 4}, .1)
    outs = [sc.step({"S1": 0, "AI1": 4}, .1) for _ in range(30)]
    assert any(o.get("AQ1") == 8 for o in outs[:5]) and outs[-1]["AQ1"] == 0

def test_schedule_and_modes():
    ctx = Ctx(now=dt.datetime(2026, 10, 5, 7, 0))   # lunes
    s = mk("schedule", cfg={"entries": [{"days": [0, 1, 2, 3, 4], "start": "06:00", "end": "08:00", "value": 1},
                                       {"days": [5, 6], "start": "09:00", "end": "11:00", "value": 1}]}, ctx=ctx)
    assert s.step({}, .1)["O"] == 1
    ctx.now = dt.datetime(2026, 10, 5, 12, 0); assert s.step({}, .1)["O"] == 0
    ctx.now = dt.datetime(2026, 10, 10, 10, 0); assert s.step({}, .1)["O"] == 1   # sábado
    s2 = mk("schedule", cfg={"entries": [{"start": "00:00", "end": "23:59", "mode": "vacaciones"}]}, ctx=ctx)
    assert s2.step({}, .1)["O"] == 0
    ot = mk("operating-times-periphery", cfg={"entries": [{"mode": "vacaciones", "from": "2026-10-01", "to": "2026-10-31"}]}, ctx=ctx)
    ot.step({}, .1); assert s2.step({}, .1)["O"] == 1

def test_schedule_overnight():
    ctx = Ctx(now=dt.datetime(2026, 10, 5, 23, 30))
    s = mk("schedule", cfg={"entries": [{"start": "22:00", "end": "06:00"}]}, ctx=ctx)
    assert s.step({}, .1)["O"] == 1
    ctx.now = dt.datetime(2026, 10, 6, 3, 0); assert s.step({}, .1)["O"] == 1
    ctx.now = dt.datetime(2026, 10, 6, 12, 0); assert s.step({}, .1)["O"] == 0

def test_times_sun_madrid():
    ctx = Ctx(now=dt.datetime(2026, 6, 21, 14, 0))   # mediodía solar ≈ 14:00 hora de verano
    t = mk("times", ctx=ctx); o = t.step({}, .1)
    assert 65 < o["SunElevation"] < 75 and o["Daylight"] == 1
    ctx.now = dt.datetime(2026, 12, 21, 3, 0); o = t.step({}, .1); assert o["Night"] == 1 and o["Daylight"] == 0

def test_text_generator():
    t = mk("text-generator", cfg={"template": "Temp <v1> grados"})
    assert t.step({"Tr": 1, "V1": 21.5}, .1)["Txt"] == "Temp 21.5 grados"
    assert t.step({"Tr": 0}, .1)["Txt"] == ""

def test_alarm_clock():
    ctx = Ctx(now=dt.datetime(2026, 10, 5, 6, 59, 59))
    a = mk("alarm-clock", cfg={"enabled": True, "time": 420}, ctx=ctx)
    assert a.step({}, .1)["Buzzer"] == 0
    ctx.now = dt.datetime(2026, 10, 5, 7, 0, 0); assert a.step({}, .1)["Buzzer"] == 1
    assert a.step({"Ca": 1}, .1)["Buzzer"] == 0
