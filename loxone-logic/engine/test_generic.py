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
