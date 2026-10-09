"""Capa genérica: adaptación, servicios con plantilla, avisos y catálogo de interfaz."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
import ha_bridge as hb
from ha_bridge import service_for, _tpl, Bridge

HERE = os.path.dirname(os.path.abspath(__file__))
UI = json.load(open(os.path.join(HERE, "..", "ui_catalogo.json"), encoding="utf-8"))
CAT = json.load(open(os.path.join(HERE, "..", "catalogo_loxone.json"), encoding="utf-8"))

def test_tpl():
    assert _tpl({"a": "{v}", "b": "x{v}"}, 5) == {"a": 5, "b": "x5"}

def test_service_explicit_and_default():
    assert service_for("cover.x", 30, "cover.open_cover") == ("cover", "open_cover", {"entity_id": "cover.x"})
    assert service_for("media_player.m", 0.5, "media_player.volume_set", {"volume_level": "{v}"})[2]["volume_level"] == 0.5
    assert service_for("light.l", 40)[2]["brightness_pct"] == 40
    assert service_for("scene.s", 0) is None

def proj(per, blocks=None, wires=None):
    return {"blocks": blocks or [{"id": "b", "type": "push-notification", "config": {"service": "notify.mobile_app_x", "title": "T", "message": "val <v1>"}}],
            "wires": wires or [], "consts": {}, "periphery": per, "settings": {}}

def test_pulse_equals_then_resets():
    p = proj([{"name": "btn", "dir": "in", "target": "b.Tr", "entity": "sensor.btn", "adapt": {"pulse": True, "equals": "single,press"}}])
    b = Bridge(p, "/tmp/_t.json")
    b.states["sensor.btn"] = {"state": "single"}; b.push_inputs("sensor.btn")
    b.engine.cycle(1.0)
    ev = [e for e in b.ctx.events if e.get("kind") == "notify"]
    assert ev, "el pulso debe disparar el aviso"
    c = b.notify_call(ev[0])
    assert c[0:2] == ("notify", "mobile_app_x") and c[2]["title"] == "T" and "message" in c[2]
    b.ctx.events.clear(); b.engine.cycle(1.0)
    assert not [e for e in b.ctx.events if e.get("kind") == "notify"], "sin nuevo pulso no hay aviso"

def test_ui_catalog_covers_all_and_no_loxone_leftovers():
    ids = {b["id"] for b in CAT["bloques"]}
    assert ids <= set(UI), ids - set(UI)
    bad = re.compile(r"\bLoxone\b|Miniserver|\bT[1-5]\b|Tree|Air\b", re.I)
    for k, u in UI.items():
        if u.get("hide"): continue
        for txt in [u["name"], u["category"]] + [v.get("label", "") for sec in ("inputs", "outputs", "params") for v in u.get(sec, {}).values() if not v.get("hide")]:
            assert not bad.search(txt or ""), (k, txt)

def test_rgb_group_single_call():
    import asyncio
    from ha_bridge import rgb_for
    assert rgb_for("light.x", {"r": 255, "g": 0, "b": 128})[2]["rgb_color"] == [255, 0, 128]
    assert rgb_for("light.x", {"r": 0, "g": 0, "b": 0})[1] == "turn_off"
    assert rgb_for("light.x", {"r": 9, "g": 9, "b": 9, "br": 0})[1] == "turn_off"
    assert rgb_for("light.x", {"r": 0, "g": 0, "b": 0, "br": 50})[2]["rgb_color"] == [255, 255, 255]
    sc = {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}
    blocks = [{"id": k, "type": "scaler", "params": sc} for k in "rgb"]
    per = [{"name": f"in_{k}", "dir": "in", "target": f"{k}.V", "entity": f"sensor.{k}"} for k in "rgb"]
    per += [{"name": f"Luz.{k}", "dir": "out", "target": f"{k}.Sv", "entity": "light.ida", "group": "ha1", "role": k, "adapt": {"scale_out": [0, 100, 0, 255]}} for k in "rgb"]
    b = Bridge({"blocks": blocks, "wires": [], "consts": {}, "periphery": per, "settings": {}}, "/tmp/_t2.json", dry=True)
    hb.CYCLE = 0.02
    async def go():
        t = asyncio.create_task(b.loop(None)); await asyncio.sleep(0.1)
        for k, v in zip("rgb", (100, 50, 0)):
            b.states[f"sensor.{k}"] = {"state": str(v)}; b.push_inputs(f"sensor.{k}")
        await asyncio.sleep(0.15); t.cancel()
    asyncio.run(go())
    ons = [c for c in b.calls if c["service"] == "light.turn_on"]
    assert ons and ons[-1]["data"]["rgb_color"] == [255, 128, 0], list(b.calls)
    assert all(c["service"].startswith("light.") for c in b.calls)

def test_virtual_inputs():
    sc = {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}
    p = {"blocks": [{"id": "s", "type": "scaler", "params": sc}, {"id": "c", "type": "scaler", "params": sc}],
         "wires": [], "consts": {}, "settings": {},
         "virtuals": [{"id": "v1", "name": "Nivel", "kind": "slider", "value": 30}, {"id": "v2", "name": "Color", "kind": "color", "value": "#ff0000"}],
         "periphery": [{"name": "Nivel", "dir": "in", "target": "s.V", "vid": "v1", "role": "v"},
                       {"name": "Color.r", "dir": "in", "target": "c.V", "vid": "v2", "role": "r"}]}
    b = Bridge(p, "/tmp/_t3.json", dry=True)
    b.engine.cycle(1.0)
    assert b.engine.out["s"]["Sv"] == 30 and b.engine.out["c"]["Sv"] == 100
    b.set_virtual("v1", 70); b.set_virtual("v2", "#000000"); b.engine.cycle(1.0)
    assert b.engine.out["s"]["Sv"] == 70 and b.engine.out["c"]["Sv"] == 0
    assert b.api_live()["virtual"] == {"v1": 70, "v2": "#000000"}
    try: b.set_virtual("nope", 1); assert False
    except KeyError: pass

def test_button_toggles_light_each_press():
    import asyncio
    sc = {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}
    p = {"blocks": [{"id": "_pt0", "type": "scaler", "params": sc}], "wires": [], "consts": {}, "settings": {},
         "periphery": [{"name": "btn", "dir": "in", "target": "_pt0.V", "entity": "sensor.btn_action", "adapt": {"pulse": True, "equals": "single"}},
                       {"name": "Ida luz", "dir": "out", "target": "_pt0.Sv", "entity": "light.ida", "service": "homeassistant.toggle", "service_when": "rise"}]}
    b = Bridge(p, "/tmp/_t4.json", dry=True); hb.CYCLE = 0.02
    async def go():
        t = asyncio.create_task(b.loop(None)); await asyncio.sleep(0.1)
        for _ in range(3):
            b.states["sensor.btn_action"] = {"state": "single"}; b.push_inputs("sensor.btn_action"); await asyncio.sleep(0.12)
            b.states["sensor.btn_action"] = {"state": ""}; b.push_inputs("sensor.btn_action"); await asyncio.sleep(0.08)
        t.cancel()
    asyncio.run(go())
    assert [c["service"] for c in b.calls].count("homeassistant.toggle") == 3, list(b.calls)

def test_event_entity_reads_event_type():
    from ha_bridge import state_value
    st = {"entity_id": "event.x_action", "state": "2026-10-09T20:00:00", "attributes": {"event_type": "single"}}
    assert state_value(st) == "single"
    assert state_value({"entity_id": "sensor.x", "state": "on", "attributes": {}}) == "on"
