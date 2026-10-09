#!/usr/bin/env python3
"""Puente Home Assistant <-> motor Loxone-like + servidor del editor visual.
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
for m in ("b_basic", "b_seq", "b_comfort", "b_energy", "b_audio", "b_climate", "b_stubs"): __import__(m)
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

def state_value(st, attribute=None):
    if st is None: return None
    if attribute: return (st.get("attributes") or {}).get(attribute)
    return st.get("state")

_UI = None
def _ui():
    global _UI
    if _UI is None:
        p = os.path.join(HERE, "..", "ui_catalogo.json")
        _UI = json.load(open(p)) if os.path.exists(p) else {}
    return _UI

def _clean(project):
    return {k: v for k, v in project.items() if not k.startswith("_")}

class Bridge:
    def __init__(self, project, path, dry=True):
        self.path, self.dry = path, dry
        self.lock = threading.RLock()
        self.states = {}; self.mid = 0; self.connected = False
        self.calls = collections.deque(maxlen=200); self.err = None
        self.ws = None; self.loop_ = None; self.ha_cfg = {}; self.ent_area = {}
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
            eng = Engine(project, ctx)
            if snap:
                same = {x["id"] for x in project["blocks"] if old is not None and old.project_types.get(x["id"]) == x["type"]}
                try: eng.restore({k: v for k, v in snap.items() if k in same})
                except Exception as e: self.err = f"snapshot ignorado: {e}"
            eng.project_types = {b["id"]: b["type"] for b in project["blocks"]}
            self.project, self.ctx, self.engine = project, ctx, eng
            self.last_out = {}; self.first = True
            self.in_map = {}
            for p in project.get("periphery", []):
                if p["dir"] == "in": self.in_map.setdefault(p["entity"], []).append(p)
            self.out_map = {p["name"]: p for p in project.get("periphery", []) if p["dir"] == "out"}
            for e in self.in_map: self.push_inputs(e, True)
    def save(self, project):
        Engine(project, Ctx())                       # valida (lanza si hay tipo desconocido, etc.)
        with self.lock:
            if os.path.exists(self.path): os.replace(self.path, self.path + ".bak")
            tmp = self.path + ".tmp"; json.dump(project, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, self.path)
            self.load(project)
    def nid(self): self.mid += 1; return self.mid
    def push_inputs(self, entity, initial=False):
        for p in self.in_map.get(entity, []):
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
                for name, val in outs.items():
                    if self.last_out.get(name) == val: continue
                    first_seen = name not in self.last_out
                    self.last_out[name] = val
                    if self.first and first_seen: continue        # no empuja el estado inicial a HA
                    p = self.out_map[name]
                    if p.get("service_when") == "rise" and not val: continue
                    c = service_for(p["entity"], val, p.get("service"), p.get("service_data"))
                    if c: pending.append((c, name))
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
            gid, sid, cid, aid, eid, did = (self.nid() for _ in range(6))
            reg = {}
            for i_, t_ in ((aid, "area"), (eid, "entity"), (did, "device")):
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
                    elif m.get("id") in (aid, eid, did) and m.get("type") == "result":
                        reg[m["id"]] = m.get("result") or []
                        if len(reg) == 3:
                            areas = {a["area_id"]: a["name"] for a in reg[aid]}
                            dev = {d["id"]: d.get("area_id") for d in reg[did]}
                            with self.lock:
                                self.ent_area = {e["entity_id"]: areas.get(e.get("area_id") or dev.get(e.get("device_id")), "")
                                                 for e in reg[eid]}
                    elif m.get("id") == gid and m.get("type") == "result":
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
                            self.states[e] = d["new_state"]
                            if e in self.in_map: self.push_inputs(e)
            finally:
                self.connected = False
                if task: task.cancel()
    # ---------- API del editor
    def api_live(self):
        with self.lock:
            return {"connected": self.connected, "dry": self.dry, "cycles": self.engine.ncycles, "err": self.err,
                    "out": {b: o for b, o in self.engine.out.items()}, "last_out": self.last_out,
                    "calls": list(self.calls)[:30],
                    "in": {p["name"]: state_value(self.states.get(p["entity"]), p.get("attribute"))
                           for p in self.project.get("periphery", []) if p["dir"] == "in"}}
    def api_entities(self):
        with self.lock:
            return [{"id": e, "name": (s.get("attributes") or {}).get("friendly_name", e), "state": s.get("state"), "domain": e.split(".")[0], "area": self.ent_area.get(e, "")}
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
            if p in ("/", "/index.html"):
                f = os.path.join(HERE, "editor.html")
                return self._send(200, open(f, "rb").read() if os.path.exists(f) else b"editor.html no encontrado", "text/html")
            if p == "/api/catalog":
                reg = runtime.REGISTRY
                out = []
                ui = _ui()
                for b in runtime.catalog().values():
                    c = reg.get(b["id"]); out.append({**b, "ui": ui.get(b["id"], {}), "impl": "no" if c is None else ("stub" if getattr(c, "STUB", False) else "ok")})
                return self._send(200, out)
            if p == "/api/entities": return self._send(200, br.api_entities())
            if p == "/api/project":
                with br.lock: return self._send(200, _clean(br.project))
            if p == "/api/live": return self._send(200, br.api_live())
            self._send(404, {"error": "no existe"})
        def do_PUT(self):
            n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
            try:
                if self.path == "/api/project":
                    br.save(json.loads(body)); return self._send(200, {"ok": True})
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
    b = Bridge(project, path, dry="--live" not in sys.argv)
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
