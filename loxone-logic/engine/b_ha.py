"""Bloques propios de Home Assistant: un bloque por dispositivo, con la entidad dentro (config.entity).
No necesitan nodos de entrada/salida de HA: expand() genera la periferia al cargar el proyecto.
La interfaz (app) los coloca sola en su habitación según el área de la entidad en HA."""
from runtime import Block, block, g, clamp

EV_DEFAULT = {
    "E1": "single,short_press,press,on_press,toggle,1_short_release,button_1_press,button_1_press_release",
    "E2": "double,double_press,2_double_press,button_1_double_press",
    "EL": "hold,long_press,long,1_long_press,button_1_hold,button_1_long_press",
}

def _hex_to_pct(v):
    """'#rrggbb' o [r,g,b] 0-255 -> [r,g,b] 0-100."""
    if isinstance(v, str):
        s = v.lstrip("#"); s = (s + "ffffff")[:6] if len(s) < 6 else s[:6]; v = [int(s[k:k + 2], 16) for k in (0, 2, 4)]
    return [round(clamp(x, 0, 255) * 100 / 255, 1) for x in v]

@block("ha-light")
class HaLight(Block):
    """Luz de HA (Zigbee, Z-Wave, ESPHome…): on/off, brillo, color, blanco por temperatura y escenas.
    Entradas: Tg alternar, On, Off, Scene (nº; 0 = apagar), Br (0-100), Col ('#rrggbb'), Temp (0 cálido … 100 frío),
    S (estado real de la luz; lo cablea expand()). Salidas: O, M (escena), Br, Ct (1 = blanco por temperatura), K.
    Lo que se envía se adapta a lo que admite la luz (ver rgb_for)."""
    STATE = ("on", "scene", "br", "rgb", "last", "ct", "k")
    def init(self):
        c = self.cfg
        self.scenes = {int(s["id"]): s for s in c.get("scenes", [])}
        self.on = 0; self.scene = 0; self.br = 100; self.rgb = [100, 100, 100]; self.ct = 0; self.k = 50
        self.last = min(self.scenes) if self.scenes else 0
        self.sprev = None; self.scprev = None; self.brprev = None; self.colprev = None; self.tprev = None
        self.t = 0.0; self.cmd_t = -99.0
    def _cmd(self): self.cmd_t = self.t
    def _apply(self, sid):
        if sid in (0, None): self.on = 0; self.scene = 0; self._cmd(); return
        s = self.scenes.get(int(sid))
        if s is None: return
        self.scene = int(sid); self.last = self.scene; self.on = 1
        if s.get("temp") is not None: self.ct = 1; self.k = clamp(s["temp"], 0, 100)
        elif s.get("rgb"): self.ct = 0; self.rgb = [clamp(v, 0, 100) for v in s["rgb"]]
        self.br = clamp(s.get("br", 100), 1, 100); self._cmd()
    def _turn_on(self):
        if self.last in self.scenes: self._apply(self.last)
        else: self.on = 1; self.scene = 0; self._cmd()
    def step(self, i, dt):
        self.t += dt
        s = i.get("S")
        if s is not None and s != self.sprev:
            self.sprev = s
            if self.t - self.cmd_t > 2.0:                 # sigue a HA, salvo eco de una orden propia
                self.on = int(bool(s))
                if not self.on: self.scene = 0
        if self.rise("Tg", i.get("Tg")):
            if self.on: self.on = 0; self.scene = 0; self._cmd()
            else: self._turn_on()
        if self.rise("On", i.get("On")) and not self.on: self._turn_on()
        if self.rise("Off", i.get("Off")): self.on = 0; self.scene = 0; self._cmd()
        sc = i.get("Scene")
        if sc is not None and sc != self.scprev: self._apply(int(sc))
        self.scprev = sc
        b = i.get("Br")
        if b is not None and b != self.brprev:
            if b <= 0: self.on = 0; self.scene = 0
            else: self.br = clamp(b, 1, 100); self.on = 1; self.scene = 0
            self._cmd()
        self.brprev = b
        col = i.get("Col"); ckey = str(col) if col is not None else None
        if ckey is not None and ckey != self.colprev:
            self.rgb = _hex_to_pct(col); self.ct = 0; self.on = 1; self.scene = 0; self._cmd()
        self.colprev = ckey
        tp = i.get("Temp")
        if tp is not None and tp != self.tprev:
            self.k = clamp(tp, 0, 100); self.ct = 1; self.on = 1; self.scene = 0; self._cmd()
        self.tprev = tp
        r, gg, bb = self.rgb
        return {"O": self.on, "M": self.scene, "R": r, "G": gg, "B": bb, "Br": self.br if self.on else 0, "Ct": self.ct, "K": self.k}

@block("scaler")
class Scaler(Block):
    """Interno (no sale en el catálogo): lo usa el editor para unir directamente dos nodos de HA."""
    def step(self, i, dt):
        p = self.p; v = g(i, "V")
        if p["V2"] == p["V1"]: return {"Sv": p["Sv1"]}
        return {"Sv": p["Sv1"] + (v - p["V1"]) * (p["Sv2"] - p["Sv1"]) / (p["V2"] - p["V1"])}

@block("ha-remote")
class HaRemote(Block):
    """Mando / pulsador de HA (entidad event.* o sensor.*_action). Da un pulso por cada tipo de pulsación."""
    def step(self, i, dt):
        return {"P1": int(g(i, "E1") > 0), "P2": int(g(i, "E2") > 0), "PL": int(g(i, "EL") > 0)}

def expand(project):
    """Periferia que generan los bloques de dispositivo (se añade a la del proyecto, no se guarda)."""
    per = []
    for b in project.get("blocks", []):
        c = b.get("config") or {}; e = c.get("entity"); bid = b["id"]
        if not e: continue
        if b["type"] == "ha-light":
            per.append({"name": f"{bid}·estado", "dir": "in", "target": f"{bid}.S", "entity": e, "adapt": {"equals": "on"}})
            for role, port in (("r", "R"), ("g", "G"), ("b", "B"), ("br", "Br"), ("ct", "Ct"), ("k", "K")):
                q = {"name": f"{bid}·{role}", "dir": "out", "target": f"{bid}.{port}", "entity": e, "group": bid, "role": role}
                if role in "rgb": q["adapt"] = {"scale_out": [0, 100, 0, 255]}
                per.append(q)
        elif b["type"] == "ha-remote":
            ev = {**EV_DEFAULT, **(c.get("events") or {})}
            for port in ("E1", "E2", "EL"):
                q = {"name": f"{bid}·{port}", "dir": "in", "target": f"{bid}.{port}", "entity": e, "adapt": {"pulse": True, "equals": ev[port]}}
                if c.get("attribute"): q["attribute"] = c["attribute"]
                per.append(q)
    return per
