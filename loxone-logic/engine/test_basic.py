import pytest
from runtime import REGISTRY, Engine
import b_basic  # noqa

def run(bid, seq, dt=0.1, params=None, cfg=None):
    b = REGISTRY[bid](bid, params, cfg); out = []
    for i in seq: out.append(b.step(i, dt))
    return out

def hold(i, n): return [i] * n

def test_logic():
    assert run("and", [{"I1": 1, "I2": 1}])[0]["O"] == 1
    assert run("and", [{"I1": 1, "I2": 0}])[0]["O"] == 0
    assert run("or", [{"I1": 0, "I2": 1}])[0]["O"] == 1
    assert run("exclusive-or", [{"I1": 1, "I2": 1}])[0]["O"] == 0
    assert run("not", [{"I": 0}])[0]["O"] == 1

def test_switch_on_delay():
    o = run("switch-on-delay", hold({"Tr": 1}, 15), params={"Don": 1})
    assert o[5]["O"] == 0 and o[11]["O"] == 1
    assert run("switch-on-delay", hold({"Tr": 1}, 5) + [{"Tr": 0}], params={"Don": 1})[-1]["O"] == 0

def test_switch_off_delay():
    o = run("switch-off-delay", [{"Tr": 1}] + hold({"Tr": 0}, 15), params={"Don": 1})
    assert o[0]["O"] == 1 and o[5]["O"] == 1 and o[-1]["O"] == 0

def test_monoflop_retrigger():
    seq = [{"Tr": 1}, {"Tr": 0}] + hold({"Tr": 0}, 15)
    o = run("monoflop", seq, params={"D": 1})
    assert o[0]["O"] == 1 and o[5]["O"] == 1 and o[-1]["O"] == 0

def test_flipflops():
    assert run("flipflop-sr", [{"S": 1, "R": 1}])[0]["O"] == 1   # set domina
    assert run("flipflop-rs", [{"S": 1, "R": 1}])[0]["O"] == 0   # reset domina
    o = run("flipflop-sr", [{"Tg": 1}, {"Tg": 0}, {"Tg": 1}])
    assert [x["O"] for x in o] == [1, 1, 0]

def test_threshold_hysteresis():
    o = run("threshold-switch", [{"V": 6}, {"V": 3}, {"V": 0.5}])
    assert [x["O"] for x in o] == [1, 1, 0]

def test_counters():
    o = run("counter", [{"C": 1}, {"C": 0}, {"C": 1}, {"C": 0}, {"C": 1}])
    assert o[-1]["V"] == 3
    o = run("up-down-counter", [{"C": 1, "Dir": 0}, {"C": 0}, {"C": 1, "Dir": 1}], params={"Sv": 5, "Von": 10, "Voff": 4})
    assert o[0]["V"] == 6 and o[-1]["V"] == 5

def test_math():
    assert run("add-2-way", [{"V1": 2, "V2": 3}])[0]["O"] == 5
    assert run("multiply", [{"V1": 2, "V2": 0, "V3": 4}])[0]["O"] == 8   # 0 se ignora
    assert run("divide", [{"V1": 6, "V2": 0}])[0]["O"] == 0
    assert run("modulo", [{"V1": 7, "V2": 3}])[0] == {"Int": 2, "Dec": 1.0}
    assert run("average", [{"V1": 2, "V2": 4}])[0]["Avg"] == 3

def test_formula():
    from b_basic import eval_formula
    assert abs(eval_formula("(I1+(I2*0,005))/SIN(I3)", {"I1": 1, "I2": 200, "I3": 1.5707963}) - 2.0) < 1e-6
    assert eval_formula("IF(I1 > 0;1;0)", {"I1": 3}) == 1
    assert eval_formula("MAX(I1*I2;100)", {"I1": 5, "I2": 30}) == 150
    assert eval_formula("I1^2", {"I1": 3}) == 9
    o = run("formula", [{"I1": 1, "I2": 0}], cfg={"formula": "I1/I2"})[0]
    assert o["E"] == 1

def test_scaler_limiter():
    assert run("scaler", [{"V": 5}], params={"V1": 0, "Sv1": 0, "V2": 10, "Sv2": 100})[0]["Sv"] == 50
    assert run("analogue-min-max-limiter", [{"V": 50}], params={"Min": 0, "Max": 10})[0]["V"] == 10

def test_dewpoint():
    d = run("dewpoint-calculator", [{"ϑ": 20, "H": 50}])[0]["ϑd"]
    assert abs(d - 9.3) < 0.2

def test_pi_and_twopos():
    o = run("2-position-controller", [{"PV": 4}, {"PV": 5.1}, {"PV": 5.4}], params={"SP": 5, "Hys": 0.5})
    assert [x["O"] for x in o] == [1, 1, 0]
    pi = run("pi-controller", hold({"PV": 3, "Auto": 1}, 30), params={"SP": 5, "Kp": 2, "Ki": 0, "St": 1, "Max": 10})
    assert pi[-1]["CO"] == 4

def test_ramp():
    o = run("ramp-controller", hold({"S": 1}, 100), params={"Sv": 5, "Sts": 0.1, "L1": 7, "L2": 3})
    assert abs(o[-1]["V"] - 3) < 1e-9 or o[-1]["V"] < 5

def test_stairwell_warning():
    o = run("stairwell-light-switch", [{"Tr": 1}] + hold({"Tr": 0}, 200), params={"Don": 10, "Tw": 2, "Dw": 0.5})
    assert o[10]["O"] == 1
    assert o[int(8.1/0.1)]["O"] == 0 and o[int(9/0.1)]["O"] == 1 and o[-1]["O"] == 0

def test_comfort_switch_long():
    seq = [{"Tg": 1}] * 8 + [{"Tg": 0}] + hold({"Tg": 0}, 3000)
    assert run("multifunction-switch", seq, params={"Don": 10, "Tlc": 0.5})[-1]["O"] == 1  # permanente

def test_long_click():
    seq = [{"Tr": 1}] * 5 + [{"Tr": 0}]
    o = run("long-click", seq, params={"TI": 0.35})
    assert o[-1]["O1"] == 0 and o[-1]["O2"] == 1 and o[-1]["V"] == 2

def test_double_click():
    o = run("double-click", [{"Tr": 1}, {"Tr": 0}, {"Tr": 1}])
    assert o[-1]["Q"] == 1

def test_radio_buttons():
    o = run("radio-buttons", [{"I3": 1}, {"I3": 0, "+": 1}], params={"Max": 8})
    assert o[0]["N"] == 3 and o[1]["N"] == 4

def test_selection_plus_wraps():
    o = run("selection-switch-plus", [{"+": 1}, {"+": 0}] * 3, params={"Vmin": 1, "Vmax": 3, "Sts": 1, "Vdef": 1})
    assert [x["O"] for x in o][::2] == [2, 3, 1]

def test_edge_detection():
    o = run("edge-detection", [{"I": 1}, {"I": 1}, {"I": 0}], params={"Pd": 0.15})
    assert o[0]["On"] == 1 and o[2]["Off"] == 1

def test_delayed_pulse():
    o = run("delayed-pulse", [{"P": 1}] + hold({"P": 0}, 60), params={"Dd": 1, "Dp": 0.5})
    assert o[5]["P"] == 0 and any(x["P"] for x in o[9:16]) and o[-1]["P"] == 0

def test_engine_wiring_and_periphery():
    proj = {"blocks": [{"id": "a", "type": "and"}, {"id": "n", "type": "not"}],
            "wires": [["a.O", "n.I"]], "consts": {"a.I2": 1},
            "periphery": [{"name": "pulsador", "dir": "in", "target": "a.I1", "entity": "binary_sensor.x"},
                          {"name": "luz", "dir": "out", "target": "n.O", "entity": "light.y"}]}
    e = Engine(proj)
    e.set_periphery("pulsador", "on"); r = e.cycle(0.1)
    assert r["luz"] == 0
    e.set_periphery("pulsador", "off"); assert e.cycle(0.1)["luz"] == 1
    e.set_periphery("pulsador", "unavailable"); e.cycle(0.1)   # no rompe

def test_remanencia():
    e = Engine({"blocks": [{"id": "f", "type": "flipflop-sr"}], "consts": {"f.S": 1}})
    e.cycle(); snap = e.snapshot()
    e2 = Engine({"blocks": [{"id": "f", "type": "flipflop-sr"}]}); e2.restore(snap)
    assert e2.cycle()  is not None and e2.blocks["f"].o == 1
