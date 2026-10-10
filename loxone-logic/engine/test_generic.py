"""Capa genérica: adaptación, servicios con plantilla, avisos y catálogo de interfaz."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
import ha_bridge as hb
from ha_bridge import service_for, _tpl, Bridge

HERE = os.path.dirname(os.path.abspath(__file__))
UI = json.load(open(os.path.join(HERE, "..", "ui_catalogo_ha.json"), encoding="utf-8"))
CAT = json.load(open(os.path.join(HERE, "..", "catalogo_ha.json"), encoding="utf-8"))

def test_tpl():
    assert _tpl({"a": "{v}", "b": "x{v}"}, 5) == {"a": 5, "b": "x5"}

def test_service_explicit_and_default():
    assert service_for("cover.x", 30, "cover.open_cover") == ("cover", "open_cover", {"entity_id": "cover.x"})
    assert service_for("media_player.m", 0.5, "media_player.volume_set", {"volume_level": "{v}"})[2]["volume_level"] == 0.5
    assert service_for("light.l", 40)[2]["brightness_pct"] == 40
    assert service_for("scene.s", 0) is None

def proj(per, blocks=None, wires=None):
    return {"blocks": blocks or [{"id": "b", "type": "scaler", "params": {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}}],
            "wires": wires or [], "consts": {}, "periphery": per, "settings": {}}

def test_pulse_equals_then_resets():
    p = proj([{"name": "btn", "dir": "in", "target": "b.V", "entity": "sensor.btn", "adapt": {"pulse": True, "equals": "single,press"}}])
    b = Bridge(p, "/tmp/_t.json")
    b.states["sensor.btn"] = {"state": "single"}; b.push_inputs("sensor.btn")
    b.engine.cycle(1.0); assert b.engine.out["b"]["Sv"] == 1
    b.engine.cycle(1.0); assert b.engine.out["b"]["Sv"] == 0, "el pulso dura un ciclo"

def test_ui_catalog_covers_all_blocks():
    import runtime
    ids = {b["id"] for b in CAT["bloques"]}
    assert ids == set(UI) and ids <= set(runtime.REGISTRY), (ids ^ set(UI), ids - set(runtime.REGISTRY))
    for b in CAT["bloques"]:
        for sec in ("inputs", "outputs"):
            assert {p["abbr"] for p in b[sec]} <= set(UI[b["id"]][sec]), (b["id"], sec)

def test_incompatible_project_is_set_aside(tmp_path):
    import subprocess
    p = tmp_path / "p.json"; p.write_text(json.dumps({"blocks": [{"id": "x", "type": "switch"}], "wires": [], "consts": {}, "periphery": []}))
    try: Bridge(json.load(open(p)), str(p), dry=True); assert False
    except KeyError as e: assert "switch" in str(e)

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

def test_simulate_input():
    p = {"blocks": [{"id": "_pt0", "type": "scaler", "params": {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}}], "wires": [], "consts": {}, "settings": {},
         "periphery": [{"name": "btn", "dir": "in", "target": "_pt0.V", "entity": "event.x_action", "adapt": {"pulse": True, "equals": "single"}}]}
    b = Bridge(p, "/tmp/_t5.json", dry=True)
    b.simulate("event.x_action", "single"); b.engine.cycle(1.0)
    assert b.engine.out["_pt0"]["Sv"] == 1
    b.engine.cycle(1.0); assert b.engine.out["_pt0"]["Sv"] == 0

def test_pulse_ignores_attribute_only_updates():
    p = {"blocks": [{"id": "_pt0", "type": "scaler", "params": {"V1": 0, "Sv1": 0, "V2": 1, "Sv2": 1}}], "wires": [], "consts": {}, "settings": {},
         "periphery": [{"name": "btn", "dir": "in", "target": "_pt0.V", "entity": "sensor.x", "adapt": {"pulse": True, "equals": "single"}}]}
    b = Bridge(p, "/tmp/_t6.json", dry=True)
    b.states["sensor.x"] = {"state": "single"}
    b.push_inputs("sensor.x", same_state=True); b.engine.cycle(1.0)
    assert b.engine.out["_pt0"]["Sv"] == 0
    b.push_inputs("sensor.x"); b.engine.cycle(1.0)
    assert b.engine.out["_pt0"]["Sv"] == 1

def test_app_model_virtuals_and_status():
    p = {"blocks": [], "wires": [], "consts": {}, "settings": {}, "periphery": [], "pages": [{"id": "p1", "name": "Salón"}],
         "virtuals": [{"id": "ha1", "name": "Brillo", "kind": "slider", "value": 40}],
         "ha_nodes": [{"id": "ha1", "dir": "in", "virt": "slider", "name": "Brillo", "min": 0, "max": 100, "step": 1, "page": "p1", "value": 40},
                      {"id": "ha2", "dir": "out", "rgb": True, "name": "Lámpara", "entity": "light.l", "page": "p1"},
                      {"id": "ha3", "dir": "out", "name": "Oculta", "entity": "light.o", "page": "p1", "app": False}]}
    b = Bridge(p, "/tmp/_t10.json", dry=True)
    b.states["light.l"] = {"state": "on", "attributes": {"rgb_color": [255, 0, 0]}}
    m = b.api_app()
    assert [r["name"] for r in m["rooms"]] == ["Salón"]
    cs = {c["type"]: c for c in m["rooms"][0]["controls"]}
    assert set(cs) == {"slider", "light"} and cs["slider"]["value"] == 40 and cs["light"]["rgb"] == [255, 0, 0]

SCN = [{"id": 1, "name": "Cálido", "rgb": [100, 68, 30]}, {"id": 2, "name": "Blanco", "rgb": [100, 100, 100], "br": 60}]

def light_project(lights, scenes=SCN, remote=True):
    """Controlador de luz coordinando N luces (nodos) y un mando (nodo) cableado a Alternar/Apagar."""
    nodes = [{"id": f"L{k}", "dir": "out", "light": True, "entity": e, "name": e, "page": "p1"} for k, e in enumerate(lights)]
    wires = [{"f": "luz", "fp": "L", "t": n["id"], "tp": "L"} for n in nodes]
    if remote:
        nodes.append({"id": "M", "dir": "in", "remote": True, "entity": "event.estanteria_action", "name": "Mando", "page": "p1"})
        wires += [{"f": "M", "fp": "P1", "t": "luz", "tp": "Tg"}, {"f": "M", "fp": "P2", "t": "luz", "tp": "Off"}]
    return {"blocks": [{"id": "luz", "type": "ha-light", "config": {"scenes": scenes}}], "wires": [], "consts": {}, "settings": {}, "periphery": [],
            "pages": [{"id": "p1", "name": "P"}], "ha_nodes": nodes, "ha_wires": wires}

def test_ha_light_controller_drives_wired_lights():
    import asyncio
    p = light_project(["light.ida_luz", "light.aplique"])
    b = Bridge(p, "/tmp/_t11.json", dry=True); hb.CYCLE = 0.02
    b.states["light.ida_luz"] = {"state": "off", "attributes": {"supported_color_modes": ["xy"]}}
    b.states["light.aplique"] = {"state": "off", "attributes": {"supported_color_modes": ["brightness"]}}
    b.load(p)
    async def go():
        t = asyncio.create_task(b.loop(None)); await asyncio.sleep(0.15)
        b.simulate("event.estanteria_action", "single"); await asyncio.sleep(0.2)
        b.block_cmd("luz", "Scene", 2); await asyncio.sleep(0.2)
        b.simulate("event.estanteria_action", "double"); await asyncio.sleep(0.2); t.cancel()
    asyncio.run(go())
    calls = sorted((c["data"]["entity_id"], c["service"], tuple(sorted(c["data"]))) for c in b.calls)
    ons = {}
    for c in reversed(list(b.calls)):                      # b.calls va de más nuevo a más antiguo; nos quedamos con la última orden de cada luz
        if c["service"] == "light.turn_on": ons[c["data"]["entity_id"]] = c["data"]
    assert ons["light.ida_luz"]["rgb_color"] == [255, 255, 255] and ons["light.ida_luz"]["brightness_pct"] == 60   # escena 2, luz de color
    assert "rgb_color" not in ons["light.aplique"] and ons["light.aplique"]["brightness_pct"] == 60                    # luz solo regulable: solo brillo
    offs = {c["data"]["entity_id"] for c in b.calls if c["service"] == "light.turn_off"}
    assert offs == {"light.ida_luz", "light.aplique"}, calls                                                           # 2 pulsaciones apagan las dos

def test_ha_light_follows_ha_state():
    p = light_project(["light.ida_luz"], remote=False)
    b = Bridge(p, "/tmp/_t14.json", dry=True)
    b.states["light.ida_luz"] = {"state": "off", "attributes": {}}; b.load(p); b.engine.cycle(1.0)
    assert b.engine.out["luz"]["O"] == 0
    b.engine.cycle(3.0); b.states["light.ida_luz"] = {"state": "on", "attributes": {}}; b.push_inputs("light.ida_luz"); b.engine.cycle(1.0)
    assert b.engine.out["luz"]["O"] == 1
    try: b.block_cmd("luz", "Hack", 1); assert False
    except ValueError: pass

def test_ha_light_app_card_by_area():
    p = light_project(["light.ida_luz", "light.aplique"], remote=False)
    p["blocks"][0]["name"] = "Luz techo"
    b = Bridge(p, "/tmp/_t12.json", dry=True)
    b.ent_area = {"light.ida_luz": "Dormitorio", "light.aplique": "Dormitorio"}; b.area_meta = {"Dormitorio": {"floor": "Planta alta", "level": 1}}
    b.states["light.ida_luz"] = {"state": "on", "attributes": {"rgb_color": [255, 0, 0], "supported_color_modes": ["xy"]}}
    b.states["light.aplique"] = {"state": "off", "attributes": {"supported_color_modes": ["color_temp"]}}
    m = b.api_app()
    assert [(r["name"], r["floor"]) for r in m["rooms"]] == [("Dormitorio", "Planta alta")]
    c = m["rooms"][0]["controls"][0]
    assert c["type"] == "ha-light" and c["name"] == "Luz techo" and c["lights"] == 2 and c["scenes"][0]["name"] == "Cálido"
    assert c["caps"]["color"] and c["caps"]["temp"] and c["rgb"] == [255, 0, 0]

def test_rgb_for_adapts_to_light_capabilities():
    from ha_bridge import rgb_for, light_caps
    ct = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500}
    rgb = {"supported_color_modes": ["xy", "color_temp"], "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500}
    dim = {"supported_color_modes": ["brightness"]}; onoff = {"supported_color_modes": ["onoff"]}
    v = {"r": 255, "g": 100, "b": 0, "br": 40, "ct": 0, "k": 0}
    assert rgb_for("light.x", v, rgb)[2] == {"entity_id": "light.x", "rgb_color": [255, 100, 0], "brightness_pct": 40}
    assert rgb_for("light.x", v, ct)[2] == {"entity_id": "light.x", "brightness_pct": 40}        # sin color: solo brillo
    assert rgb_for("light.x", v, dim)[2] == {"entity_id": "light.x", "brightness_pct": 40}
    assert rgb_for("light.x", v, onoff)[2] == {"entity_id": "light.x"}
    w = {**v, "ct": 1, "k": 100}
    assert rgb_for("light.x", w, ct)[2]["color_temp_kelvin"] == 6500 and rgb_for("light.x", {**w, "k": 0}, rgb)[2]["color_temp_kelvin"] == 2000
    assert "rgb_color" not in rgb_for("light.x", w, rgb)[2]
    assert rgb_for("light.x", {**v, "br": 0}, rgb)[1] == "turn_off"
    assert light_caps(onoff)["bri"] is False and light_caps(rgb)["color"] and light_caps(rgb)["temp"]

def test_ha_light_color_temp_and_scene_white():
    cfg = {"entity": "light.l", "scenes": [{"id": 1, "name": "Lectura", "temp": 20, "br": 70}, {"id": 2, "name": "Rojo", "rgb": [100, 0, 0]}]}
    p = {"blocks": [{"id": "luz", "type": "ha-light", "config": cfg}], "wires": [], "consts": {}, "settings": {}, "periphery": []}
    b = Bridge(p, "/tmp/_t13.json", dry=True)
    b.block_cmd("luz", "Scene", 1); b.engine.cycle(1.0); o = b.engine.out["luz"]
    assert (o["Ct"], o["K"], o["Br"], o["M"]) == (1, 20, 70, 1)
    b.engine.cycle(1.0); b.block_cmd("luz", "Scene", 2); b.engine.cycle(1.0); o = b.engine.out["luz"]
    assert o["Ct"] == 0 and (o["R"], o["G"]) == (100, 0)
    b.engine.cycle(1.0); b.block_cmd("luz", "Col", "#00ff00"); b.engine.cycle(1.0); o = b.engine.out["luz"]
    assert (o["R"], o["G"], o["B"], o["M"]) == (0, 100, 0, 0)
    b.engine.cycle(1.0); b.block_cmd("luz", "Temp", 80); b.engine.cycle(1.0); o = b.engine.out["luz"]
    assert (o["Ct"], o["K"], o["O"]) == (1, 80, 1)


def test_app_no_duplicate_light_card_and_page_fallback():
    p = light_project(["light.ida_luz"], remote=True)
    b = Bridge(p, "/tmp/_t15.json", dry=True)            # la luz no tiene área en HA: cae a la página del editor
    b.states["light.ida_luz"] = {"state": "on", "attributes": {"friendly_name": "Ida luz"}}
    m = b.api_app()
    assert [r["name"] for r in m["rooms"]] == ["P"] and len(m["rooms"][0]["controls"]) == 1 and m["rooms"][0]["controls"][0]["type"] == "ha-light"
