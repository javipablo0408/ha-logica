#!/usr/bin/env python3
"""Puente Home Assistant <-> motor de bloques + servidor del editor visual y de la app.
Uso:  python3 ha_bridge.py proyecto.json [--live] [--port 8099]
 - Sin --live arranca en SIMULACIÓN (imprime/loguea las llamadas pero no toca HA). Se activa desde el editor.
 - HA_URL (ws://IP:8123/api/websocket) y HA_TOKEN (token de larga duración). En add-on: SUPERVISOR_TOKEN.
 - Editor: http://IP:8099
"""
import asyncio, collections, json, os, sys, threading, time, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websockets
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runtime
__import__("b_ha")
from b_ha import expand as _expand
from runtime import Engine, Ctx

CYCLE = 1.0

def _tpl(x, v):
    """Sustituye {v} (valor del bloque) en plantillas de datos de servicio."""
    if isinstance(x, str): return v if x.strip() == "{v}" else x.replace("{v}", str(v))
    if isinstance(x, dict): return {k: _tpl(y, v) for k, y in x.items()}
    if isinstance(x, list): return [_tpl(y, v) for y in x]
    return x

def service_for(entity, value, explicit=None, data=None):
    """-> (dominio, servicio, datos) según la entidad y el valor (None = nada que hacer).
    explicit = 'dominio.servicio' (servicio personalizado); data = datos extra con plantilla {v}."""
    dom = entity.split(".")[0]; v = value
    if explicit:
        d, s = explicit.split(".", 1); return d, s, {"entity_id": entity, **_tpl(data or {}, v)}
    t = lambda srv, **kw: (dom, srv, {"entity_id": entity, **kw})
    if dom in ("light", "switch", "input_boolean", "fan", "siren", "humidifier", "automation", "media_player", "remote"):
        if dom == "light" and isinstance(v, (int, float)) and not isinstance(v, bool) and v not in (0, 1) and 0 < v <= 100:
            return t("turn_on", brightness_pct=round(v))
        if dom == "fan" and isinstance(v, (int, float)) and v not in (0, 1) and 0 < v <= 100: return t("set_percentage", percentage=round(v))
        return t("turn_on" if v else "turn_off")
    if dom == "cover": return t("set_cover_position", position=int(max(0, min(100, v))))
    if dom in ("input_number", "number"): return t("set_value", value=float(v))
    if dom in ("climate", "water_heater"): return t("set_temperature", temperature=float(v))
    if dom in ("input_text", "text"): return t("set_value", value=str(v))
    if dom in ("input_select", "select"): return t("select_option", option=str(v))
    if dom == "lock": return t("lock" if v else "unlock")
    if dom == "valve": return t("open_valve" if v else "close_valve")
    if dom == "vacuum": return t("start" if v else "return_to_base")
    if dom in ("scene", "script"): return t("turn_on") if v else None
    if dom in ("button", "input_button"): return t("press") if v else None
    return None

COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

def light_caps(attrs):
    """Qué admite una luz según supported_color_modes de HA (None = aún no se sabe)."""
    m = set((attrs or {}).get("supported_color_modes") or [])
    if not m: return None
    return {"color": bool(m & COLOR_MODES), "temp": "color_temp" in m or "rgbww" in m, "bri": bool(m - {"onoff"}),
            "tmin": (attrs or {}).get("min_color_temp_kelvin") or 2000, "tmax": (attrs or {}).get("max_color_temp_kelvin") or 6500}

def rgb_for(entity, vals, attrs=None):
    """Canales r,g,b (0-255), brillo (0-100) y/o temperatura (ct=1, k 0-100: cálido→frío) -> UNA llamada light.*,
    adaptada a lo que admite la luz (attrs = atributos de HA; sin ellos se asume que admite todo)."""
    c = lambda x: int(max(0, min(255, round(float(x or 0)))))
    r, g, b = c(vals.get("r")), c(vals.get("g")), c(vals.get("b"))
    br = vals.get("br"); caps = light_caps(attrs)
    if br is not None:
        if float(br or 0) <= 0: return ("light", "turn_off", {"entity_id": entity})
    elif (r, g, b) == (0, 0, 0) and not vals.get("ct"): return ("light", "turn_off", {"entity_id": entity})
    d = {"entity_id": entity}
    if vals.get("ct") and (caps is None or caps["temp"]):
        k = max(0.0, min(100.0, float(vals.get("k") or 0))) / 100
        lo, hi = (caps["tmin"], caps["tmax"]) if caps else (2000, 6500)
        d["color_temp_kelvin"] = int(round(lo + k * (hi - lo)))
    elif caps is None or caps["color"]:
        if (r, g, b) == (0, 0, 0): r = g = b = 255          # con brillo conectado, color sin elegir = blanco
        d["rgb_color"] = [r, g, b]
    if br is not None and (caps is None or caps["bri"]): d["brightness_pct"] = int(max(1, min(100, round(float(br)))))
    return ("light", "turn_on", d)

def state_value(st, attribute=None):
    if st is None: return None
    if not attribute and str(st.get("entity_id", "")).startswith("event."): attribute = "event_type"   # el estado de un evento es solo una marca de tiempo
    if attribute: return (st.get("attributes") or {}).get(attribute)
    return st.get("state")

_UI = None
def _ui():
    global _UI
    if _UI is None:
        q = os.path.join(HERE, "..", "ui_catalogo_ha.json")
        _UI = json.load(open(q)) if os.path.exists(q) else {}
    return _UI

def _clean(project):
    return {k: v for k, v in project.items() if not k.startswith("_")}

class Bridge:
    def __init__(self, project, path, dry=True):
        self.path, self.dry = path, dry
        self.lock = threading.RLock()
        self.states = {}; self.mid = 0; self.connected = False
        self.calls = collections.deque(maxlen=200); self.err = None
        self.ws = None; self.loop_ = None; self.ha_cfg = {}; self.ent_area = {}; self.ent_dev = {}; self.area_meta = {}; self.ent_devid = {}
        self.load(project)
    # ---------- proyecto
    def load(self, project, snap=None):
        with self.lock:
            ctx = Ctx(); s = project.get("settings", {})
            for k in ("lat", "lon", "tz"):
                v = s.get(k, self.ha_cfg.get(k) if hasattr(self, "ha_cfg") else None)
                if v is not None: setattr(ctx, k, v)
            ctx.now = dt.datetime.now(); ctx.events = []
            old = getattr(self, "engine", None)
            if snap is None and old is not None: snap = old.snapshot()
            ep = {**project, "periphery": list(project.get("periphery", [])) + _expand(project)}
            eng = Engine(ep, ctx)
            if snap:
                same = {x["id"] for x in project["blocks"] if old is not None and old.project_types.get(x["id"]) == x["type"]}
                try: eng.restore({k: v for k, v in snap.items() if k in same})
                except Exception as e: self.err = f"snapshot ignorado: {e}"
            eng.project_types = {b["id"]: b["type"] for b in project["blocks"]}
            self.project, self.ctx, self.engine = project, ctx, eng
            self.last_out = {}; self.first = True
            self.in_map = {}
            self.virt = {v["id"]: v for v in project.get("virtuals", [])}
            for p in ep["periphery"]:
                if p["dir"] == "in" and not p.get("vid"): self.in_map.setdefault(p["entity"], []).append(p)
            self.out_map = {p["name"]: p for p in ep["periphery"] if p["dir"] == "out"}
            for e in self.in_map: self.push_inputs(e, True)
            for p in project.get("periphery", []):
                if p.get("vid") and p["dir"] == "in": self._push_virtual(p, True)
    def _push_virtual(self, p, initial=False):
        v = self.virt.get(p["vid"]) or {}; val = v.get("value")
        if v.get("kind") == "color":
            h = str(val or "#ffffff").lstrip("#"); h = (h + "ffffff")[:6] if len(h) < 6 else h[:6]
            val = round(int(h[{"r": 0, "g": 2, "b": 4}.get(p.get("role"), 0):][:2], 16) * 100 / 255, 1)
        elif v.get("kind") == "button": val = 1 if not initial else None
        if val is None: return
        self.engine.set_periphery(p["name"], val, initial)
    def simulate(self, entity, value):
        """Simula que una entidad de HA cambia de estado (solo localmente; no toca Home Assistant)."""
        with self.lock:
            old = self.states.get(entity) or {}
            at = dict(old.get("attributes") or {})
            if entity.startswith("event."): at["event_type"] = value
            self.states[entity] = {"entity_id": entity, "state": value, "attributes": at}
            if entity in self.in_map: self.push_inputs(entity)
    def set_virtual(self, vid, value):
        with self.lock:
            v = self.virt.get(vid)
            if v is None: raise KeyError(f"entrada virtual desconocida: {vid} (guarda y despliega primero)")
            if v.get("kind") != "button": v["value"] = value
            for p in self.project.get("periphery", []):
                if p.get("vid") == vid and p["dir"] == "in": self._push_virtual(p)
            if v.get("kind") != "button":
                tmp = self.path + ".tmp"; json.dump(self.project, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, self.path)
    def save(self, project):
        Engine({**project, "periphery": list(project.get("periphery", [])) + _expand(project)}, Ctx())   # valida (lanza si hay tipo desconocido, etc.)
        with self.lock:
            if os.path.exists(self.path): os.replace(self.path, self.path + ".bak")
            tmp = self.path + ".tmp"; json.dump(project, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, self.path)
            self.load(project)
    def nid(self): self.mid += 1; return self.mid
    def push_inputs(self, entity, initial=False, same_state=False):
        for p in self.in_map.get(entity, []):
            # un pulsador (pulso) solo reacciona cuando cambia el ESTADO, no cuando HA actualiza atributos con el mismo estado
            if same_state and (p.get("adapt") or {}).get("pulse") and not p.get("attribute"): continue
            self.engine.set_periphery(p["name"], state_value(self.states.get(entity), p.get("attribute")), initial)
    # ---------- HA
    async def call(self, ws, dom, srv, data, name):
        self.calls.appendleft({"t": dt.datetime.now().strftime("%H:%M:%S"), "service": f"{dom}.{srv}", "data": data, "sent": not self.dry, "from": name})
        print(f"[{dt.datetime.now():%H:%M:%S}] {'SIM ' if self.dry else ''}{dom}.{srv} {json.dumps(data, ensure_ascii=False)}", flush=True)
        if not self.dry:
            await ws.send(json.dumps({"id": self.nid(), "type": "call_service", "domain": dom, "service": srv, "service_data": data}))
    def notify_call(self, ev):
        """Evento de aviso de un bloque -> llamada notify.* de HA. config del bloque:
        {"service":"notify.mobile_app_x","title":"…","message":"texto con <v1>"}; por defecto notify.notify."""
        if ev.get("kind") != "notify": return None
        cfg = next((b.get("config") or {} for b in self.project["blocks"] if b["id"] == ev["block"]), {})
        msg = str(cfg.get("message") or ev.get("params", {}).get("(texto)") or "Aviso del motor de lógica")
        for k, v in (ev.get("values") or {}).items(): msg = msg.replace(f"<{k.lower()}>", str(v))
        d, s_ = (cfg.get("service") or "notify.notify").split(".", 1)
        data = {"message": msg}
        title = cfg.get("title") or ev.get("params", {}).get("(nombre)")
        if title: data["title"] = str(title)
        return d, s_, {**data, **(cfg.get("data") or {})}
    async def loop(self, ws):
        last = time.monotonic()
        while True:
            await asyncio.sleep(CYCLE)
            now = time.monotonic(); dtc = now - last; last = now
            pending = []
            with self.lock:
                self.ctx.now = dt.datetime.now() - dt.timedelta(seconds=dtc)
                try: outs = self.engine.cycle(dtc); self.err = None
                except Exception as e: outs = {}; self.err = f"{type(e).__name__}: {e}"
                dirty = set()
                for name, val in outs.items():
                    if self.last_out.get(name) == val: continue
                    first_seen = name not in self.last_out
                    self.last_out[name] = val
                    if self.first and first_seen: continue        # no empuja el estado inicial a HA
                    p = self.out_map[name]
                    if p.get("group"): dirty.add(p["group"]); continue
                    if p.get("service_when") == "rise" and not val: continue
                    c = service_for(p["entity"], val, p.get("service"), p.get("service_data"))
                    if c: pending.append((c, name))
                for g in dirty:
                    mem = [q for q in self.out_map.values() if q.get("group") == g]
                    pending.append((rgb_for(mem[0]["entity"], {q["role"]: self.last_out.get(q["name"]) for q in mem}, (self.states.get(mem[0]["entity"]) or {}).get("attributes")), g))
                self.first = False
                for ev in self.ctx.events:
                    c = self.notify_call(ev)
                    if c: pending.append((c, ev["block"]))
                self.ctx.events.clear()
                if self.engine.ncycles % 60 == 0:
                    try: json.dump(self.engine.snapshot(), open(self.path + ".state.json", "w"), default=str)
                    except Exception: pass
            for c, name in pending: await self.call(ws, *c, name)
    async def run(self, url, token):
        async with websockets.connect(url, max_size=None) as ws:
            assert json.loads(await ws.recv())["type"] == "auth_required"
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            r = json.loads(await ws.recv())
            if r["type"] != "auth_ok": raise SystemExit(f"Auth fallida: {r}")
            aid, eid, did, fid, gid, cid, sid = (self.nid() for _ in range(7))   # HA exige ids crecientes en el orden de envío
            reg = {}
            for i_, t_ in ((aid, "area"), (eid, "entity"), (did, "device"), (fid, "floor")):
                await ws.send(json.dumps({"id": i_, "type": f"config/{t_}_registry/list"}))
            await ws.send(json.dumps({"id": gid, "type": "get_states"}))
            await ws.send(json.dumps({"id": cid, "type": "get_config"}))
            await ws.send(json.dumps({"id": sid, "type": "subscribe_events", "event_type": "state_changed"}))
            task = None
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("id") == cid and m.get("type") == "result" and m.get("success"):
                        c = m["result"]; self.ha_cfg = {"lat": c.get("latitude"), "lon": c.get("longitude"), "tz": c.get("time_zone")}
                        with self.lock:
                            for k, v in self.ha_cfg.items():
                                if v is not None and k not in self.project.get("settings", {}): setattr(self.ctx, k, v)
                    elif m.get("id") in (aid, eid, did, fid) and m.get("type") == "result":
                        if not m.get("success"): print("registro no disponible (sin áreas):", m.get("error"), flush=True)
                        reg[m["id"]] = (m.get("result") or []) if m.get("success") else []
                        if len(reg) == 4:
                            areas = {a["area_id"]: a["name"] for a in reg[aid]}
                            fl = {f["floor_id"]: (f.get("name", ""), f.get("level") if f.get("level") is not None else 0) for f in reg[fid]}
                            meta = {}
                            for a in reg[aid]:
                                fn, lv = fl.get(a.get("floor_id"), ("", 0))
                                meta[a["name"]] = {"floor": fn, "level": lv}
                            self.area_meta = meta
                            dev = {d["id"]: d.get("area_id") for d in reg[did]}
                            with self.lock:
                                self.ent_area = {e["entity_id"]: areas.get(e.get("area_id") or dev.get(e.get("device_id")), "")
                                                 for e in reg[eid]}
                                dn = {d["id"]: d.get("name_by_user") or d.get("name") or "" for d in reg[did]}
                                self.ent_dev = {e["entity_id"]: dn.get(e.get("device_id"), "") for e in reg[eid] if e.get("device_id")}
                                self.ent_devid = {e["entity_id"]: e["device_id"] for e in reg[eid] if e.get("device_id")}
                    elif m.get("id") == gid and m.get("type") == "result":
                        if not m.get("success"): raise OSError(f"get_states falló: {m.get('error')}")
                        with self.lock:
                            for st in m["result"]: self.states[st["entity_id"]] = st
                            for e in self.in_map: self.push_inputs(e, True)
                        self.connected = True; self.ws = ws
                        print(f"Conectado. {len(self.states)} entidades, {len(self.engine.blocks)} bloques, ciclo {CYCLE}s. "
                              f"Modo: {'SIMULACIÓN' if self.dry else 'ACTIVO'}", flush=True)
                        task = asyncio.create_task(self.loop(ws))
                    elif m.get("type") == "event":
                        d = m["event"]["data"]; e = d["entity_id"]
                        with self.lock:
                            old = self.states.get(e); new = d["new_state"]
                            same = bool(old and new and old.get("state") == new.get("state") and not e.startswith("event."))
                            self.states[e] = new
                            if e in self.in_map: self.push_inputs(e, same_state=same)
            finally:
                self.connected = False
                if task: task.cancel()
    # ---------- API del editor
    def api_live(self):
        with self.lock:
            return {"connected": self.connected, "dry": self.dry, "cycles": self.engine.ncycles, "err": self.err,
                    "out": {b: o for b, o in self.engine.out.items()}, "last_out": self.last_out,
                    "calls": list(self.calls)[:30], "virtual": {k: v.get("value") for k, v in self.virt.items()},
                    "in": {p["name"]: state_value(self.states.get(p.get("entity")), p.get("attribute"))
                           for p in self.project.get("periphery", []) if p["dir"] == "in" and not p.get("vid")}}
    def api_app(self):
        """Interfaz de usuario: plantas > habitaciones (áreas de HA) > un dispositivo = una tarjeta, todo ordenado solo.
        Los bloques de dispositivo (ha-light…) van a la habitación del área de su entidad en HA; lo antiguo (controles
        virtuales, controlador de iluminación) va a la habitación que se llame como su página del editor."""
        with self.lock:
            pr = self.project; nodes = pr.get("ha_nodes") or []; byid = {n["id"]: n for n in nodes}
            rooms = {}
            def room(name, pg=None):
                name = (name or "").strip() or "Sin área"; k = name.lower()
                if k not in rooms:
                    m = self.area_meta.get(name) or next((v for a, v in self.area_meta.items() if a.lower() == k), {})
                    rooms[k] = {"id": k, "name": name, "floor": m.get("floor", ""), "level": m.get("level", 0), "controls": []}
                return rooms[k]
            pages = {p["id"]: p["name"] for p in (pr.get("pages") or [])}
            first = next(iter(pages.values()), "Inicio"); ui = pr.get("ui") or {}; wires = pr.get("ha_wires") or []
            pname = lambda pg: pages.get(pg) or first
            def virt(n):
                v = self.virt.get(n["id"]) or {}
                return {"id": n["id"], "type": n["virt"], "name": n.get("name") or n["virt"], "value": v.get("value", n.get("value")),
                        "min": n.get("min", 0), "max": n.get("max", 100), "step": n.get("step", 1)}
            def ent(e):
                st = self.states.get(e) or {}; return st.get("state"), (st.get("attributes") or {})
            for bl in pr.get("blocks", []):
                cfg = bl.get("config") or {}; bid = bl["id"]; out = self.engine.out.get(bid) or {}
                if bl.get("app") is False: continue
                if bl["type"] == "ha-light" and cfg.get("entity"):
                    e = cfg["entity"]; stt, at = ent(e)
                    room(self.ent_area.get(e))["controls"].append({"id": bid, "type": "ha-light", "cat": 0,
                        "name": bl.get("name") or at.get("friendly_name") or self.ent_dev.get(e) or e, "entity": e,
                        "caps": light_caps(at) or {"color": False, "temp": False, "bri": True, "tmin": 2000, "tmax": 6500},
                        "on": bool(out.get("O")), "scene": out.get("M", 0), "br": out.get("Br") or 0,
                        "k": out.get("K", 50), "ct": bool(out.get("Ct")), "rgb": at.get("rgb_color") if stt == "on" else None,
                        "scenes": [{"id": int(s["id"]), "name": s.get("name") or f"Escena {s['id']}"} for s in cfg.get("scenes", [])]})
            used = set()
            for n in nodes:
                if n.get("app") is False or n["id"] in used: continue
                if n.get("virt"): room(pname(n.get("page")))["controls"].append({**virt(n), "cat": 5})
                elif n.get("dir") == "out" and n.get("entity"):
                    stt, at = ent(n["entity"])
                    room(self.ent_area.get(n["entity"]) or pname(n.get("page")))["controls"].append({"id": n["id"], "type": "light" if n.get("rgb") else "status",
                        "cat": 1, "name": n.get("name") or n["entity"], "entity": n["entity"], "state": stt,
                        "rgb": at.get("rgb_color") if n.get("rgb") else None})
            rs = sorted((r for r in rooms.values() if r["controls"]), key=lambda r: (r["level"], r["floor"].lower(), r["name"].lower()))
            for r in rs: r["controls"].sort(key=lambda c: (c.get("cat", 9), str(c.get("name")).lower()))
            return {"dry": self.dry, "connected": self.connected, "rooms": rs}
    def block_cmd(self, block, port, value):
        if port not in ("Tg", "On", "Off", "Scene", "Br", "Col", "Temp"): raise ValueError(f"orden no permitida: {port}")
        with self.lock:
            if (self.engine.project_types or {}).get(block) != "ha-light": raise KeyError(f"no es un bloque de luz: {block}")
            self.engine.inject(block, port, value)
    def scene(self, block, mood):
        with self.lock: self.engine.inject(block, "Mood", int(mood))
    def api_entities(self):
        with self.lock:
            return [{"id": e, "name": (s.get("attributes") or {}).get("friendly_name", e), "state": s.get("state"), "domain": e.split(".")[0], "area": self.ent_area.get(e, ""), "device": self.ent_dev.get(e, "")}
                    for e, s in sorted(self.states.items()) if s]

def make_handler(br):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body, default=str, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html", "/editor", "/app"):
                home = os.environ.get("INICIO", "editor")
                page = "app.html" if (p == "/app" or (p in ("/", "/index.html") and home == "app")) else "editor.html"
                f = os.path.join(HERE, page)
                return self._send(200, open(f, "rb").read() if os.path.exists(f) else b"editor.html no encontrado", "text/html")
            if p == "/api/catalog":
                reg = runtime.REGISTRY
                out = []
                ui = _ui()
                for b in runtime.catalog().values():
                    c = reg.get(b["id"]); out.append({**b, "ui": ui.get(b["id"], {}), "impl": "no" if c is None else ("stub" if getattr(c, "STUB", False) else "ok")})
                return self._send(200, out)
            if p == "/api/entities": return self._send(200, br.api_entities())
            if p == "/api/entity":
                import urllib.parse
                eid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
                with br.lock: st = br.states.get(eid)
                return self._send(200 if st else 404, {"id": eid, "state": st.get("state"), "attributes": st.get("attributes") or {}} if st else {"error": "entidad desconocida"})
            if p == "/api/project":
                with br.lock: return self._send(200, _clean(br.project))
            if p == "/api/app": return self._send(200, br.api_app())
            if p == "/api/live": return self._send(200, br.api_live())
            self._send(404, {"error": "no existe"})
        def do_PUT(self):
            n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
            try:
                if self.path == "/api/project":
                    br.save(json.loads(body)); return self._send(200, {"ok": True})
                if self.path == "/api/simulate":
                    d = json.loads(body)
                    if not br.dry: raise ValueError("La prueba de entradas solo está disponible en SIMULACIÓN")
                    br.simulate(d["entity"], d.get("value")); return self._send(200, {"ok": True})
                if self.path == "/api/block":
                    d = json.loads(body); br.block_cmd(d["block"], d["port"], d.get("value", 1)); return self._send(200, {"ok": True})
                if self.path == "/api/scene":
                    d = json.loads(body); br.scene(d["block"], d["mood"]); return self._send(200, {"ok": True})
                if self.path == "/api/virtual":
                    d = json.loads(body); br.set_virtual(d["id"], d.get("value")); return self._send(200, {"ok": True})
                if self.path == "/api/mode":
                    br.dry = not bool(json.loads(body).get("active")); br.first = True; br.last_out = {}
                    return self._send(200, {"dry": br.dry})
            except Exception as e:
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "no existe"})
        do_POST = do_PUT
    return H

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args: sys.exit(__doc__)
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8099
    if "--port" in sys.argv: args = [a for a in args if a != str(port)]
    path = os.path.abspath(args[0]); project = json.load(open(path))
    try: b = Bridge(project, path, dry="--live" not in sys.argv)
    except KeyError as e:                       # proyecto con bloques que ya no existen: se aparta y se empieza vacío
        os.replace(path, path + ".incompatible.json")
        print(f"Proyecto incompatible ({e}); guardado como {path}.incompatible.json. Empiezo vacío.", flush=True)
        project = {"settings": {}, "pages": [{"id": "p1", "name": "Página 1"}], "blocks": [], "wires": [], "consts": {}, "periphery": []}
        json.dump(project, open(path, "w")); b = Bridge(project, path, dry="--live" not in sys.argv)
    if os.path.exists(path + ".state.json"):
        try: b.engine.restore(json.load(open(path + ".state.json")))
        except Exception as e: print("snapshot ignorado:", e)
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(b))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Editor: http://0.0.0.0:{port}", flush=True)
    sup = os.environ.get("SUPERVISOR_TOKEN")
    url = os.environ.get("HA_URL") or ("ws://supervisor/core/websocket" if sup else "ws://localhost:8123/api/websocket")
    token = os.environ.get("HA_TOKEN") or sup
    if not token: sys.exit("Falta HA_TOKEN")
    while True:
        try: asyncio.run(b.run(url, token))
        except (OSError, websockets.ConnectionClosed) as e: b.err = f"HA: {e}"; print("reconectando:", e, flush=True); time.sleep(5)
        except KeyboardInterrupt: break

if __name__ == "__main__": main()
