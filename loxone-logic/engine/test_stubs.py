import json, os
import coverage, runtime
from runtime import Engine, Ctx

def test_every_catalog_block_instantiates_and_steps():
    for b in coverage.blocks:
        blk = runtime.REGISTRY[b["id"]](b["id"], None, None, Ctx())
        out = blk.step({}, 1.0)
        assert isinstance(out, dict), b["id"]

def test_notifier_emits_event_on_rising_edge():
    e = Engine({"blocks": [{"id": "n", "type": "push-notification"}], "wires": [], "consts": {"n.(entrada)": 1}}, Ctx())
    e.cycle(1.0); e.cycle(1.0)
    assert len(e.ctx.events) == 1

def test_analogue_correction_linear():
    blk = runtime.REGISTRY["analogue-correction"]("analogue-correction", {"IV1": 0, "DV1": 0, "IV2": 10, "DV2": 100}, None, Ctx())
    assert blk.step({"I": 5}, 1)["O"] == 50

def test_equals_adaptation_for_button_actions():
    e = Engine({"blocks": [{"id": "s", "type": "switch"}], "wires": [], "consts": {},
                "periphery": [{"name": "b", "dir": "in", "target": "s.Tg", "entity": "sensor.b_action", "adapt": {"equals": "single"}}]}, Ctx())
    e.set_periphery("b", "single"); assert e.inputs_ext[("s", "Tg")] == 1
    e.set_periphery("b", "double"); assert e.inputs_ext[("s", "Tg")] == 0
    e.set_periphery("b", "unavailable"); assert e.inputs_ext[("s", "Tg")] is None
