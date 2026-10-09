import datetime as dt
from runtime import REGISTRY, Ctx, Engine
import b_basic, b_seq, b_comfort, b_energy, b_audio, b_climate  # noqa

def mk(bid, params=None, cfg=None, ctx=None): return REGISTRY[bid](bid, params, cfg, ctx)
def run(b, seq, dt=1.0): return [b.step(i, dt) for i in seq]
def hold(i, n): return [i] * n

CAL = {"calendar": {"0": [{"days": list(range(7)), "start": "07:00", "end": "22:00", "state": "comfort"}]}, "area": 20}

def test_irc_comfort_eco_targets():
    ctx = Ctx(now=dt.datetime(2026, 1, 12, 12, 0)); r = mk("intelligent-room-controller", cfg=CAL, ctx=ctx)
    o = r.step({"ϑc": 20.0, "Mode": 0}, 1); assert o["Os"] == 1 and o["ϑt"] == 22.5 and o["H"] > 0 and o["HCm"] == 1
    ctx.now = dt.datetime(2026, 1, 12, 3, 0); o = r.step({"ϑc": 20.0, "Mode": 0}, 1); assert o["Os"] == 0 and o["ϑt"] == 19.5

def test_irc_no_demand_inside_band_and_off_mode():
    ctx = Ctx(now=dt.datetime(2026, 1, 12, 12, 0)); r = mk("intelligent-room-controller", cfg=CAL, ctx=ctx)
    o = r.step({"ϑc": 23.5, "Mode": 0}, 1); assert o["H"] == 0 and o["C"] == 0
    o = r.step({"ϑc": 15.0, "Mode": -1}, 1); assert o["H"] == 0 and o["Os"] == -1

def test_irc_heat_cool_changeover_needs_a_minute():
    ctx = Ctx(now=dt.datetime(2026, 7, 1, 12, 0)); r = mk("intelligent-room-controller", cfg=CAL, ctx=ctx)
    o = r.step({"ϑc": 26.0, "Mode": 0}, 1); assert o["HCm"] == 1 and o["C"] == 0          # aún calor (cambio pendiente)
    o = run(r, hold({"ϑc": 26.0, "Mode": 0}, 70))[-1]; assert o["HCm"] == -1 and o["C"] > 0

def test_irc_timers_and_window():
    ctx = Ctx(now=dt.datetime(2026, 1, 12, 3, 0)); r = mk("intelligent-room-controller", {"Cet": 100, "Ddwc": 10}, CAL, ctx)
    o = r.step({"ϑc": 20.0, "Mode": 0, "C": 1}, 1); assert o["Os"] == 1
    r.step({"ϑc": 20.0, "Mode": 0, "C": 0}, 1)
    o = run(r, hold({"ϑc": 20.0, "Mode": 0}, 105))[-1]; assert o["Os"] == 0                # caduca el confort
    o = run(r, hold({"ϑc": 20.0, "Mode": 3, "ϑt": 22, "Dwc": 1, "ϑo": 0}, 12))[-1]; assert o["Os"] == 2 and o["H"] == 0   # ventana → protección

def test_irc_shading_demand_hysteresis_and_error():
    ctx = Ctx(now=dt.datetime(2026, 7, 1, 12, 0)); r = mk("intelligent-room-controller", cfg=CAL, ctx=ctx)
    assert r.step({"ϑc": 27.6, "Mode": 0}, 1)["Shd"] == 1
    assert r.step({"ϑc": 27.2, "Mode": 0}, 1)["Shd"] == 1 and r.step({"ϑc": 27.0, "Mode": 0}, 1)["Shd"] == 0
    assert r.step({"Mode": 0}, 1)["Error"] == 1

def test_irc_to_flow_temperature_chain_in_engine():
    ctx = Ctx(now=dt.datetime(2026, 1, 12, 12, 0))
    proj = {"blocks": [{"id": "r1", "type": "intelligent-room-controller", "config": CAL},
                       {"id": "r2", "type": "intelligent-room-controller", "config": {**CAL, "area": 40}},
                       {"id": "itc", "type": "intelligent-temperature-controller"},
                       {"id": "cc", "type": "climate-controller"}],
            "wires": [["r1.API", "itc.R1"], ["r2.API", "itc.R2"], ["r1.API", "cc.R1"], ["r2.API", "cc.R2"]],
            "consts": {"r1.ϑc": 15.0, "r2.ϑc": 16.0, "r1.Mode": 0, "r2.Mode": 0, "itc.ϑo": 0.0, "cc.ϑo": 0.0}}
    e = Engine(proj, ctx)
    for _ in range(3): e.cycle(1.0)
    itc, cc = e.out["itc"], e.out["cc"]
    assert itc["AQf"] > 25 and itc["Qp"] == 1 and itc["AQr"] > 0
    assert cc["H"] == 1 and cc["C"] == 0

def test_climate_controller_limits_modes_stage2_and_avg():
    cc = mk("climate-controller", {"Tt2s": 5, "Sot": 30}); room = {"tt": 22, "tc": 19, "demand": 60, "area": 10, "heating": True}
    o = run(cc, hold({"R1": room, "ϑo": 5}, 3))[-1]; assert o["H"] == 1 and o["H2"] == 0
    o = run(cc, hold({"R1": room, "ϑo": 5}, 10))[-1]; assert o["H2"] == 1
    assert cc.step({"R1": room, "ϑo": 25}, 1)["H"] == 0                       # exterior > ϑLimH(2: usa media→None→permite)
    cc2 = mk("climate-controller", {"Mode": 2}); assert cc2.step({"R1": room, "ϑo": 5}, 1)["H"] == 0   # solo frío
    cc3 = mk("climate-controller", {"Otm": 3}); assert cc3.step({"R1": room, "ϑo": 25}, 1)["H"] == 0    # exterior 25 > 18
    assert cc3.step({"R1": room, "ϑo": 5, "B": 1}, 1)["Ah"] == 1

def test_climate_controller_auto_changeover_by_demand():
    cc = mk("climate-controller", {"Otm": 3})
    h = {"tt": 22, "tc": 19, "demand": 40, "area": 10, "heating": True}
    c = {"tt": 24, "tc": 27, "demand": 80, "area": 10, "heating": False}
    o = cc.step({"R1": h, "R2": c, "ϑo": 16.0}, 1); assert o["C"] == 1 and o["H"] == 0 and o["Sv"] == 1   # gana la mayor

def test_flow_temp_boost_stop_and_buffer():
    itc = mk("intelligent-temperature-controller"); room = {"tt": 22, "tc": 20, "demand": 80, "area": 10, "heating": True}
    o = itc.step({"R1": room, "ϑo": 0, "Ib": 1}, 1); assert o["AQf"] == 40
    o = itc.step({"R1": room, "ϑo": 0, "St": 1}, 1); assert o["Qp"] == 0 and o["AQf"] == 5
    o = itc.step({"R1": room, "ϑo": 0, "Tb": 10}, 1); assert o["Qp"] == 0                      # buffer sin alcanzar AQb+B
    o = itc.step({"R1": room, "ϑo": 0, "Tb": 60}, 1); assert o["Qp"] == 1

def test_heating_curve_block():
    h = mk("heating-curve", {"S": 0.5, "O": 0, "minFt": 5, "maxFt": 70}); o = h.step({"Tt": 20, "Ct": 0}, 1)
    assert abs(o["Ft"] - 30.9) < 0.5 and o["Iv"] == 0

def test_fan_coil_unit():
    f = mk("fan-coil-unit-controller", {"Mode": 1}); o = run(f, hold({"ϑc": 19, "ϑt": 22}, 5))[-1]
    assert o["H"] > 0 and o["C"] == 0 and o["Fan"] > 0 and o["S"] == 1
    o = f.step({"ϑc": 19, "ϑt": 22, "Ha": 0}, 1); assert o["H"] == 0                           # sin energía
    o = f.step({"ϑc": 19, "ϑt": 22, "Dwc": 1}, 1); assert o["S"] == 0
    o = f.step({"ϑc": 19, "ϑt": 22, "Sm": 1}, 1); assert o["Fan"] <= 10
    o = f.step({"ϑc": 19, "ϑt": 22, "Bm": 1}, 1); assert o["Fan"] == 100

def test_fan_coil_fresh_air_co2():
    f = mk("fan-coil-fresh-air-unit-controller", {"Mode": 4}); o = f.step({"ϑc": 22, "ϑt": 22, "CO2": 1500}, 1)
    assert o["Fan"] > 0

def test_fan_coil_central_and_ac_central():
    c = mk("fan-coil-central-controller", {"Mode": 1, "SotH": 30}); room = {"tt": 22, "tc": 19, "demand": 60, "area": 10, "heating": True}
    o = c.step({"R1": room, "ϑo": 5}, 1); assert o["H"] == 1 and o["AvMode"] == 3
    c = mk("ac-central-controller", {"Mode": 3}); assert c.step({"R1": room, "ϑo": 5}, 1)["H"] == 0

def test_ac_unit():
    a = mk("ac-control"); a.step({"Tg": 1, "Mode": 1}, 1)
    o = a.step({"Tg": 0, "Mode": 1, "ϑt": 99, "ϑc": 23}, 1); assert o["Status"] == 1 and o["ϑt"] == 40
    assert a.step({"Dwc": 1}, 1)["Status"] == 0

def test_hvac_controller_stage2():
    h = mk("hvac-controller", {"Tt2s": 3}); room = {"tt": 22, "tc": 19, "demand": 60, "area": 10, "heating": True}
    o = run(h, hold({"R1": room, "ϑo": 5, "Mode": 0}, 5))[-1]; assert o["W/W1"] == 1 and o["W2"] == 1 and o["G"] == 1
