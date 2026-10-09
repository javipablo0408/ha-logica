import datetime as dt
from runtime import REGISTRY, Ctx
import b_basic, b_seq, b_comfort  # noqa

def mk(bid, params=None, cfg=None, ctx=None): return REGISTRY[bid](bid, params, cfg, ctx)
def run(b, seq, dt=0.1): return [b.step(i, dt) for i in seq]
def hold(i, n): return [i] * n

MOODS = {"moods": {"1": {"Lc1": 100, "Lc2": 0}, "2": {"Lc1": 50, "Lc2": 80}}, "circuits": ["Lc1", "Lc2", "Lc3"]}

def test_lighting_moods_cycle_and_off():
    lc = mk("lighting-controller", cfg=MOODS)
    o = lc.step({"M+": 1}, .1); assert o["M"] == 1 and o["Lc1"] == 100
    lc.step({"M+": 0}, 1.0)
    o = lc.step({"M+": 1}, .1); assert o["M"] == 2 and o["Lc2"] == 80
    lc.step({"M+": 0}, 1.0); o = lc.step({"M+": 1}, .1); assert o["M"] == 0 and o["Lc1"] == 0

def test_lighting_double_click_off_pulse():
    lc = mk("lighting-controller", cfg=MOODS)
    lc.step({"M+": 1}, .1); lc.step({"M+": 0}, .1)
    o = lc.step({"M+": 1}, .1)
    assert o["M"] == 0 and o["2C"] == 1

def test_lighting_toggle_circuit_and_master():
    lc = mk("lighting-controller", cfg=MOODS)
    o = lc.step({"Lc1": 1}, .1); assert o["Lc1"] == 100 and o["M"] == -1
    lc.step({"Lc1": 0}, .1); o = lc.step({"Lc1": 1, "MBr": 50}, .1); assert o["Lc1"] == 0
    lc.step({"Lc1": 0, "MBr": 50}, .1); o = lc.step({"Lc1": 1, "MBr": 50}, .1); assert o["Lc1"] == 50

def test_lighting_motion_auto_off_after_moet():
    lc = mk("lighting-controller", {"Moet": 5}, {**MOODS, "auto_mood": 1})
    o = lc.step({"Mo": 1}, .1); assert o["M"] == 1
    for _ in range(10): lc.step({"Mo": 0}, .5)          # 5 s
    assert lc.step({"Mo": 0}, .5)["M"] == 0

def test_lighting_motion_ignored_when_bright_or_disabled():
    lc = mk("lighting-controller", cfg={**MOODS, "auto_mood": 1})
    assert lc.step({"Mo": 1, "Br": 100}, .1)["M"] == 0
    lc = mk("lighting-controller", cfg={**MOODS, "auto_mood": 1})
    assert lc.step({"Mo": 1, "DisP": 1}, .1)["M"] == 0

def test_lighting_alarm_blinks_even_locked():
    lc = mk("lighting-controller", cfg=MOODS)
    outs = run(lc, hold({"Alarm": 1, "Off": 1}, 40), dt=0.1)
    assert outs[0]["M"] == 99 and {o["Lc1"] for o in outs} == {0, 50}

def test_presence_extension_and_warn():
    pr = mk("presence", {"Pet": 10, "Tw": 3})
    assert pr.step({"Act": 1}, .1)["P"] == 1
    outs = run(pr, hold({"Act": 0}, 120), dt=0.1)            # 12 s
    assert outs[0]["P"] == 1 and outs[-1]["P"] == 0
    assert any(o["Warn"] for o in outs) and any(o["Poff"] for o in outs)

def test_presence_reactivation_doubles():
    pr = mk("presence", {"Pet": 10, "Tw": 0})
    run(pr, [{"Act": 1}, {"Act": 0}] + hold({"Act": 0}, 110))
    pr.step({"Act": 1}, .1)
    outs = run(pr, hold({"Act": 0}, 150))                    # 15 s: >Pet pero <2*Pet
    assert outs[-1]["P"] == 1

def test_shading_manual_and_tg_cycle():
    s = mk("automatic-shading", {"Opd": 10, "Cld": 10})
    o = run(s, [{"Cc": 1}] + hold({}, 105), dt=0.1)
    assert abs(o[-1]["Pos"] - 1) < 1e-6 and o[5]["Cl"] == 1
    run(s, [{"Tg": 1}, {"Tg": 0}] + hold({}, 20)); assert 0 < s.m.pos < 1       # empezó a abrir
    s.step({"Tg": 1}, .1); s.step({"Tg": 0}, .1)                                 # parar
    p = s.m.pos; run(s, hold({}, 20)); assert abs(s.m.pos - p) < 1e-9

def test_shading_wind_alarm_and_window_contact():
    s = mk("automatic-shading", {"Opd": 1, "Cld": 1, "Wap": 0.0})
    run(s, hold({"Cc": 1}, 30)); assert s.m.pos > 0.9
    o = run(s, hold({"Wa": 1}, 30)); assert s.m.pos < 0.1 and o[-1]["Wds"] == 1
    run(s, hold({"Wa": 0, "Cc": 1}, 2)); 
    run(s, hold({"Dwc": 1}, 30)); assert s.m.pos < 0.1

def test_shading_sun_automatic():
    ctx = Ctx(now=dt.datetime(2026, 6, 21, 14, 0))             # sol alto al sur a las 14:00
    s = mk("automatic-shading", {"Opd": 2, "Cld": 2, "Dir": 180, "Rd": 0.8, "Spe": 1}, ctx=ctx)
    o = run(s, hold({"Sps": 1}, 60))
    assert o[-1]["Sp"] == 1 and abs(s.m.pos - 0.8) < 1e-6
    ctx.now = dt.datetime(2026, 6, 21, 23, 0)
    run(s, hold({}, 60)); assert s.m.pos < 0.05                                # fin: abre (Spe=1)

def test_shading_sun_automatic_wrong_direction():
    ctx = Ctx(now=dt.datetime(2026, 6, 21, 14, 0))
    s = mk("automatic-shading", {"Dir": 0, "Dts": 30, "Dte": 30}, ctx=ctx)     # fachada norte
    run(s, hold({"Sps": 1}, 30)); assert s.m.pos == 0

def test_window_motor():
    w = mk("window", {"Opd": 5, "Cld": 5, "SoPos": 50})
    run(w, [{"Co": 1}] + hold({}, 60), dt=0.1); assert abs(w.m.pos - 1) < 1e-6
    o = run(w, [{"Wp": 1}] + hold({"Wp": 1}, 60)); assert o[-1]["Pos"] < 1
    assert o[-1]["Pos"] == 0

def test_garage_cycle_and_photocell():
    g = mk("garage-gate", {"Opd": 10, "Cld": 10})
    o = run(g, [{"Tg": 1}, {"Tg": 0}] + hold({}, 30)); assert o[0]["Op"] == 1 and 0 < o[-1]["Pos"] < 1
    run(g, [{"Tg": 1}, {"Tg": 0}]); p = g.pos; run(g, hold({}, 20)); assert g.pos == p       # parar
    run(g, [{"Tg": 1}, {"Tg": 0}] + hold({"Spc": 1}, 30)); assert g.pos <= p + 0.01 or g.dirn == 0  # fotocélula no cierra
    g2 = mk("garage-gate", {"Opd": 10, "Cld": 10}); run(g2, [{"Co": 1}] + hold({}, 105)); assert g2.pos == 1

def test_burglar_arm_delay_and_alarm_stages():
    b = mk("burglar-alarm", {"Ard": 5, "Aad": 2, "Iad": 4, "Rad": 8, "MaxA": 20})
    b.step({"Ad": 1}, .1); o = run(b, hold({"Ad": 0}, 40))                   # 4 s
    assert o[10]["S"] == 0 and o[-1]["S"] == 0
    o = run(b, hold({}, 20)); assert o[-1]["S"] == 1
    o = run(b, hold({"Wc": 1}, 1)); assert o[0]["Sa"] == 1 and o[0]["Aa"] == 0
    o = run(b, hold({"Wc": 1}, 30)); assert o[-1]["Aa"] == 1 and o[-1]["Ia"] == 0
    o = run(b, hold({"Wc": 1}, 60)); assert o[-1]["Ia"] == 1
    o = run(b, [{"Ca": 1, "Wc": 0}]); assert o[0]["Aa"] == 0 and o[0]["S"] == 1  # confirma, sigue armada

def test_burglar_presence_modes():
    b = mk("burglar-alarm"); b.step({"A": 1}, .1)
    assert b.step({"P": 1}, .1)["Sa"] == 1                                     # armada con presencia: P dispara
    b2 = mk("burglar-alarm"); b2.step({"Anp": 1}, .1)
    assert b2.step({"P": 1}, .1)["Sa"] == 0                                    # sin presencia: P ignorada
    b3 = mk("burglar-alarm"); assert b3.step({"A": 1, "Wc": 1}, .1)["S"] == 0   # ventana abierta bloquea
    b3 = mk("burglar-alarm", {"Aoc": 1}); assert b3.step({"A": 1, "Wc": 1}, .1)["S"] == 1

def test_emergency_alarm():
    e = mk("emergency-alarm", {"Ta": 1, "Tc": 1})
    assert run(e, hold({"Tg": 1}, 5))[-1]["A"] == 0 and run(e, hold({"Tg": 1}, 10))[-1]["A"] == 1
    o = run(e, hold({"Ca": 1}, 15)); assert o[-1]["A"] == 0 and any(x["Cc"] for x in o)

def test_fire_alarm_pre_main():
    f = mk("fire-water-alarm", {"Mad": 5})
    o = run(f, hold({"S": 1}, 10)); assert o[0]["Pa"] == 1 and o[-1]["Ma"] == 0
    o = run(f, hold({"S": 1}, 50)); assert o[-1]["Ma"] == 1 and o[-1]["Mas"] == 1
    f2 = mk("fire-water-alarm", {"Mad": 5}); o = run(f2, [{"T": 50}]); assert o[0]["Pa"] == 1
    f3 = mk("fire-water-alarm", {"Mad": 1, "Sm": 1}); o = run(f3, hold({"S": 1}, 30)); assert o[-1]["Mas"] == 0

def test_alarm_chain():
    c = mk("alarm-chain", {"Rt": 1, "MaxR": 1})
    o = run(c, hold({"A": 1}, 25)); assert o[0]["A1"] == 1 and o[0]["A2"] == 0 and o[-1]["A3"] == 1
    assert run(c, [{"Au": 1, "A": 1}])[0]["A10"] == 1
    assert run(c, [{"Ca": 1}])[0]["A1"] == 0

def test_irrigation_sequence_and_rain():
    ir = mk("irrigation", {"Tv1": 1, "Tv2": 1, "Tv3": 1})
    o = run(ir, [{"Act": 1}] + hold({}, 15)); assert o[0]["V1"] == 1 and o[-1]["V2"] == 1
    o = run(ir, hold({"Ra": 1}, 2)); assert o[-1]["P"] == 0

def test_wind_gauge():
    w = mk("wind-gauge", {"Avgt": 2, "F": 2, "W": 50}); o = run(w, hold({"F": 30}, 30))
    assert o[-1]["Avg"] == 60 and o[-1]["Wa"] == 1

def test_dimmer():
    d = mk("dimmer", {"MaxD": 100, "Di": 0.4}); 
    o = run(d, [{"Tg": 1}, {"Tg": 0}]); assert o[-1]["D"] == 100
    o = run(d, [{"Tg": 1}, {"Tg": 0}]); assert o[-1]["D"] == 0
    d.step({"Tg": 1}, .1); run(d, hold({"Tg": 1}, 20)); d.step({"Tg": 0}, .1); assert d.d > 0

def test_mixing_valve_pi_and_error():
    m = mk("mixing-valve-controller", {"St": 1, "Td": 10})
    o = run(m, hold({"ϑt": 40, "ϑc": 30}, 100)); assert o[-1]["V"] > 0 and any(x["O"] for x in o)
    o = run(m, hold({"Off": 1, "ϑt": 40, "ϑc": 30}, 400)); assert o[-1]["V"] == 0       # Offm=2: cierra
    m2 = mk("mixing-valve-controller"); o = run(m2, hold({"ϑt": 50, "ϑc": 30}, 6100), dt=0.1); assert o[-1]["Error"] == 1

def test_access_controller():
    a = mk("access-controller", {"Pd": 2}, {"authorized": {"123": "Javi"}})
    o = a.step({"Eid": "123"}, .1); assert o["P"] == 1 and "Javi" in o["Txt"]
    a.step({"Eid": None}, .1); o = a.step({"Eid": "999"}, .1); assert o["Pd"] == 1

def test_standby_killer():
    s = mk("standby-killer", cfg={"threshold": 10, "delay_min": 1})
    assert run(s, hold({"Power": 3, "Empty": 1}, 600))[-1]["Relay"] == 0
    assert s.step({"Motion": 1}, .1)["Relay"] == 1

def test_central_fanout():
    c = mk("shading-central"); o = c.step({"Cc": 1, "Pos1": 1.0, "Pos2": 0.0}, .1)
    assert o["Cc"] == 1 and o["Nc"] == 1 and o["No"] == 1
