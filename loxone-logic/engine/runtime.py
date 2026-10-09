"""Motor de ejecución estilo Loxone para Home Assistant.

Proyecto (JSON):
{
 "blocks":[{"id":"b1","type":"and","name":"...","params":{...}, "config":{...}}],
 "wires":[["b1.O","b2.I1"], ...],
 "consts":{"b2.I2": 5},
 "periphery":[{"name":"Luz salón","dir":"out","target":"b2.O","entity":"light.salon","adapt":{"scale":[0,100,0,255]}}, ...]
}
Cada ciclo: se evalúan los bloques en orden topológico; los lazos de realimentación usan el valor del ciclo anterior.
"""
from __future__ import annotations
import json, math, os, datetime as dt
from dataclasses import dataclass, field

REGISTRY: dict[str, type] = {}
_CAT = None

def catalog():
    global _CAT
    if _CAT is None:
        p = os.path.join(os.path.dirname(__file__), "..", "catalogo_loxone.json")
        _CAT = {b["id"]: b for b in json.load(open(p))["bloques"]}
    return _CAT

def catalog_defaults(bid):
    out = {}
    for p in catalog().get(bid, {}).get("parameters", []):
        try: out[p["abbr"]] = float(str(p["default"]).split()[0])
        except (ValueError, IndexError): pass
    return out

def block(*ids):
    def deco(cls):
        for i in ids: REGISTRY[i] = cls
        cls.IDS = ids
        return cls
    return deco

def g(i, k, d=0.0):
    v = i.get(k)
    return d if v is None else v

def clamp(x, lo, hi): return max(lo, min(hi, x))

@dataclass
class Ctx:
    now: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 1, 1, 12, 0))
    lat: float = 40.2; lon: float = -3.7            # Valdemoro
    modes: set = field(default_factory=set)          # modos de operación activos
    outdoor_temp: float | None = None
    outdoor_avg48: float | None = None
    tz: str = "Europe/Madrid"
    def tz_hours(self):
        from zoneinfo import ZoneInfo
        return self.now.replace(tzinfo=ZoneInfo(self.tz)).utcoffset().total_seconds() / 3600
    @property
    def sun(self): return sun_position(self.now, self.lat, self.lon, self.tz_hours())

def sun_position(local: dt.datetime, lat, lon, tz_hours=None):
    """Elevación y acimut (0=N, 90=E). Algoritmo NOAA simplificado. 'local' = hora local ingenua;
    tz_hours por defecto se deduce de lon (±) y no incluye horario de verano: pasar UTC real en HA."""
    tz = round(lon / 15) if tz_hours is None else tz_hours
    doy = local.timetuple().tm_yday
    hours = local.hour + local.minute / 60 + local.second / 3600
    g_ = 2 * math.pi / 365 * (doy - 1 + (hours - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868*math.cos(g_) - 0.032077*math.sin(g_) - 0.014615*math.cos(2*g_) - 0.040849*math.sin(2*g_))
    decl = (0.006918 - 0.399912*math.cos(g_) + 0.070257*math.sin(g_) - 0.006758*math.cos(2*g_) + 0.000907*math.sin(2*g_) - 0.002697*math.cos(3*g_) + 0.00148*math.sin(3*g_))
    tst = (hours*60 + eqt + 4*lon - 60*tz) % 1440
    ha = math.radians(tst/4 - 180); la = math.radians(lat)
    cz = math.sin(la)*math.sin(decl) + math.cos(la)*math.cos(decl)*math.cos(ha)
    zen = math.acos(clamp(cz, -1, 1)); elev = 90 - math.degrees(zen)
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha)*math.sin(la) - math.tan(decl)*math.cos(la))) + 180
    return elev, az % 360

class Block:
    """Base. step(i, dt) -> dict de salidas. i: entradas (None = sin conectar)."""
    IDS: tuple = ()
    DEFAULTS: dict = {}
    STATE: tuple = ()              # atributos persistentes (remanencia)
    def __init__(self, bid=None, params=None, config=None, ctx=None):
        self.bid = bid or (self.IDS[0] if self.IDS else None)
        self.p = {**catalog_defaults(self.bid), **self.DEFAULTS, **(params or {})}
        self.cfg = config or {}; self.iid = self.bid
        self.ctx = ctx or Ctx()
        self._prev = {}; self._offt = {}; self._offprev = {}
        self.init()
    def init(self): pass
    # ---- ayudas
    def rise(self, k, v):
        v = bool(v); r = v and not self._prev.get(k, False); self._prev[k] = v; return r
    def edges(self, k, v):
        v = bool(v); old = self._prev.get(k, False); self._prev[k] = v
        return (v and not old), (old and not v)
    def off(self, v, dt, key="Off"):
        """Semántica Off: pulso <200 ms = reset (devuelve True un ciclo); >200 ms = bloqueo."""
        v = bool(v); t = self._offt.get(key, 0.0); was = self._offprev.get(key, False)
        reset = False
        if v: t += dt
        else:
            if was and t < 0.2: reset = True
            t = 0.0
        self._offt[key] = t; self._offprev[key] = v
        return reset, (v and t >= 0.2)
    def get_state(self): return {k: getattr(self, k) for k in self.STATE}
    def set_state(self, s):
        for k, v in s.items(): setattr(self, k, v)
    def step(self, i, dt): raise NotImplementedError

class Engine:
    def __init__(self, project, ctx=None):
        self.project = project; self.ctx = ctx or Ctx()
        self.blocks = {}
        for b in project["blocks"]:
            cls = REGISTRY[b["type"]]
            self.blocks[b["id"]] = cls(b["type"], b.get("params"), b.get("config"), self.ctx)
            self.blocks[b["id"]].iid = b["id"]
        self.wires = [(tuple(a.split(".", 1)), tuple(c.split(".", 1))) for a, c in project.get("wires", [])]
        self.consts = {tuple(k.split(".", 1)): v for k, v in project.get("consts", {}).items()}
        self.periph = project.get("periphery", [])
        self.inputs_ext = {}                      # (block, port) -> valor desde periferia
        self.out = {bid: {} for bid in self.blocks}
        self.order = self._toposort()
        self.ncycles = 0; self._pulses = set()
    def _toposort(self):
        deps = {b: set() for b in self.blocks}
        for (sb, _), (db, _) in self.wires:
            if sb != db: deps[db].add(sb)
        order, state = [], {}
        def visit(n):
            if state.get(n) == 2: return
            if state.get(n) == 1: return           # lazo: se usa el valor anterior
            state[n] = 1
            for d in sorted(deps[n]): visit(d)
            state[n] = 2; order.append(n)
        for n in self.blocks: visit(n)
        return order
    DIGITAL = {"on": 1, "off": 0, "true": 1, "false": 0, "home": 1, "not_home": 0, "locked": 1, "unlocked": 0, "detected": 1, "clear": 0,
               "playing": 1, "paused": 0, "idle": 0, "standby": 0, "open": 1, "closed": 0, "opening": 1, "closing": 0, "disarmed": 0,
               "armed_home": 1, "armed_away": 1, "armed_night": 1, "armed_vacation": 1, "armed_custom_bypass": 1, "heat": 1, "cool": 1}
    def set_periphery(self, name, value, initial=False):
        """Entrega a los bloques el valor de una entidad de HA. initial=True: carga inicial (los pulsos no se disparan)."""
        for p in self.periph:
            if p["name"] == name and p["dir"] == "in":
                b, port = p["target"].split(".", 1); key = (b, port); a = p.get("adapt", {})
                v = self._adapt_in(p, value)
                if a.get("pulse"):
                    if initial or not v: v = 0
                    else: self._pulses.add(key)
                self.inputs_ext[key] = v
    def _adapt_in(self, p, v):
        """Perfil de adaptación de una entrada: no disponible -> fallback; equals (lista separada por comas) -> 0/1;
        pulse sin equals -> 1 en cada cambio con valor real; texto -> número; invert; scale [e0,e1,s0,s1]."""
        a = p.get("adapt", {})
        if v in ("unavailable", "unknown", None, ""):
            return 0 if a.get("pulse") and "equals" not in a else a.get("fallback")
        if "equals" in a:
            return 1 if str(v).lower() in [x.strip().lower() for x in str(a["equals"]).split(",")] else 0
        if a.get("pulse"): return 1
        if isinstance(v, str):
            v = self.DIGITAL.get(v.lower(), v)
            try: v = float(v)
            except (TypeError, ValueError): return v
        elif isinstance(v, bool): v = int(v)
        if a.get("invert"): v = 0 if v else 1
        if "scale" in a:
            i0, i1, o0, o1 = a["scale"]; v = o0 + (v - i0) * (o1 - o0) / (i1 - i0)
            lo, hi = min(o0, o1), max(o0, o1); v = max(lo, min(hi, v))
        return v
    def _adapt_out(self, p, v):
        a = p.get("adapt", {})
        if a.get("invert"): v = 0 if v else 1
        if "scale_out" in a:
            i0, i1, o0, o1 = a["scale_out"]; v = o0 + (v - i0) * (o1 - o0) / (i1 - i0)
        return v
    def cycle(self, dt=0.1):
        self.ctx.now += dt_to_td(dt)
        for bid in self.order:
            i = {}
            for (sb, sp), (db, dp) in self.wires:
                if db == bid: i[dp] = self.out[sb].get(sp)
            for (cb, cp), v in self.consts.items():
                if cb == bid: i[cp] = v
            for (eb, ep), v in self.inputs_ext.items():
                if eb == bid: i[ep] = v
            self.out[bid] = self.blocks[bid].step(i, dt) or {}
        self.ncycles += 1
        for k in self._pulses: self.inputs_ext[k] = 0      # el pulso dura un ciclo
        self._pulses.clear()
        res = {}
        for p in self.periph:
            if p["dir"] == "out":
                b, port = p["target"].split(".", 1)
                v = self.out[b].get(port)
                if v is not None: res[p["name"]] = self._adapt_out(p, v)
        return res
    def snapshot(self): return {b: o.get_state() for b, o in self.blocks.items() if o.STATE}
    def restore(self, snap):
        for b, s in snap.items(): self.blocks[b].set_state(s)

def dt_to_td(sec): return dt.timedelta(seconds=sec)

_SUNT = {}
def sun_times(ctx):
    """Minutos desde medianoche de amanecer y atardecer (elevación -0.833°) del día de ctx.now; cacheado por día."""
    key = (ctx.now.date(), ctx.lat, ctx.lon, ctx.tz)
    if key not in _SUNT:
        rise = sets = None; prev = None
        for m in range(0, 1440, 2):
            t = dt.datetime.combine(ctx.now.date(), dt.time(m // 60, m % 60))
            e = sun_position(t, ctx.lat, ctx.lon, ctx.tz_hours())[0] if True else 0
            if prev is not None:
                if prev <= -0.833 < e and rise is None: rise = m
                if prev > -0.833 >= e: sets = m
            prev = e
        _SUNT[key] = (rise, sets)
    return _SUNT[key]
