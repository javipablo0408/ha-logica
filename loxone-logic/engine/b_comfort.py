"""Confort: iluminación, persianas, ventanas, accesos, presencia, alarmas, exterior, mezcladora."""
import math
from runtime import Block, block, g, clamp, sun_times
from b_basic import PID  # noqa

# =============================================================== helpers
class Motion:
    """Modelo de recorrido de motor: posición 0..1 con tiempos de apertura/cierre (0 = abierto en persianas)."""
    def __init__(self, pos=0.0): self.pos = pos
    def move(self, target, dt, opd, cld):
        d = target - self.pos
        if abs(d) < 1e-9: return 0
        rate = (1 / max(opd, 1e-6)) if d < 0 else (1 / max(cld, 1e-6))   # d<0: hacia 0 = abrir
        step = min(abs(d), rate * dt)
        self.pos += step if d > 0 else -step
        return 1 if d > 0 else -1

# =============================================================== ILUMINACIÓN
@block("lighting-controller")
class LightingController(Block):
    """doc: ver catálogo (Lc1-8, M+/M-, Mood, Off, Mo, P, On, Alarm, Buzzer, Br, MBr, DisP, DisPc,
    moods 98/99, Moet, Pto, Pm, Met, Brt, Afi, MaxAbr, Sts/Str/MinBr/MaxBr).
    cfg['moods'] = {"1": {"Lc1": 100, "Lc2": 50}, ...}; cfg['auto_mood'] = id para P/Mo si Pm=0.
    rec: fundidos (Ft) no modelados; el recorrido de M+ incluye 0 (apagado) al final."""
    STATE = ("lv", "mood")
    def init(self):
        self.lv = {f"Lc{k}": 0.0 for k in range(1, 19)}; self.last = {f"Lc{k}": self.p["MaxBr"] for k in range(1, 19)}
        self.mood = 0; self.src = None            # src: 'manual' | 'auto'
        self.t_manual = 0.0; self.t_motion = None; self.pto = 0.0; self.clicks = []
        self.pulse2 = self.pulse3 = 0.0; self.alarm_t = 0.0; self.buz = False
        self.hold = {}
    def _ids(self): return sorted(int(k) for k in self.cfg.get("moods", {}) if int(k) not in (98, 99))
    def _apply(self, mid):
        moods = self.cfg.get("moods", {})
        for k in range(1, 19): self.lv[f"Lc{k}"] = 0.0
        if mid in (0, None): self.mood = 0; return
        if str(mid) in moods:
            for k, v in moods[str(mid)].items(): self.lv[k] = clamp(v, 0, 100)
        elif mid == 99:
            for k in range(1, 19):
                if f"Lc{k}" in self.cfg.get("circuits", [f"Lc{j}" for j in range(1, 9)]): self.lv[f"Lc{k}"] = self.p["MaxAbr"]
        self.mood = mid
    def _auto_id(self):
        pm = int(self.p["Pm"])
        if pm: return pm
        return self.cfg.get("auto_mood", (self._ids() or [99])[0])
    def _off(self, src="manual"):
        self._apply(0); self.src = None; self.t_motion = None
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        dispc = bool(g(i, "DisPc")); out_pulse2 = out_pulse3 = 0
        # ---- alarma (domina incluso con Off)
        if g(i, "Alarm"):
            self.alarm_t += dt
            on = int(self.alarm_t / max(p["Afi"] / 2, 1e-6)) % 2 == 0
            lv = {k: (p["MaxAbr"] if on else 0) for k in self.lv}
            out = {k: (v if k in self.cfg.get("circuits", [f"Lc{j}" for j in range(1, 9)]) else 0) for k, v in lv.items()}
            out.update({"M": 99, "2C": 0, "3C": 0}); return out
        self.alarm_t = 0
        self.t_manual += dt; self.pto = max(0, self.pto - dt)
        if reset or lock:
            if reset: out_pulse2 = 1
            self._off()
        elif not dispc:
            # ---- circuitos individuales
            for k in range(1, 9):
                key = f"Lc{k}"
                if self.rise(key, i.get(key)):
                    if self.lv[key] > 0: self.last[key] = self.lv[key]; self.lv[key] = 0.0
                    else: self.lv[key] = self.last[key] if not p["Lv"] else p["MaxBr"]
                    self.mood = -1 if any(self.lv.values()) else 0; self.src = "manual"; self.t_manual = 0
                    if not any(self.lv.values()): self.pto = p["Pto"]
            # ---- moods +/-
            ids = self._ids()
            for key, d in (("M+", 1), ("M-", -1)):
                if self.rise(key, i.get(key)):
                    now_t = self._clock = getattr(self, "_clock", 0.0)
                    self.clicks = [c for c in self.clicks if now_t - c <= p["Tdc"]] + [now_t]
                    if len(self.clicks) == 2: self._off(); out_pulse2 = 1
                    elif len(self.clicks) >= 3: self._off(); out_pulse2 = out_pulse3 = 1; self.clicks = []
                    else:
                        seq = [0] + ids
                        cur = self.mood if self.mood in seq else 0
                        self._apply(seq[(seq.index(cur) + d) % len(seq)])
                    self.src = "manual"; self.t_manual = 0
            if i.get("Mood") is not None and self._prev.get("Mv") != i["Mood"]:
                self._apply(int(i["Mood"])); self.src = "manual"; self.t_manual = 0
            self._prev["Mv"] = i.get("Mood")
            if self.rise("On", i.get("On")): self._apply(99); self.src = "manual"; self.t_manual = 0
            if self.rise("Buzzer", i.get("Buzzer")):
                self._apply(98 if "98" in self.cfg.get("moods", {}) else 99); self.src = "manual"; self.t_manual = 0
        self._clock = getattr(self, "_clock", 0.0) + dt
        # ---- presencia / movimiento
        bright = g(i, "Br") > p["Brt"] and i.get("Br") is not None
        dis_p = bool(g(i, "DisP"))
        mo, pr = bool(g(i, "Mo")), bool(g(i, "P"))
        mr, mf = self.edges("Mo", mo); pr_r, pr_f = self.edges("P", pr)
        if not lock and not dis_p and not bright and self.pto <= 0:
            if (mr or pr_r) and self.mood == 0: self._apply(self._auto_id()); self.src = "auto"
        if mf and self.src == "auto" and p["Moet"] > 0: self.t_motion = p["Moet"]
        if mo: self.t_motion = None
        if pr_f and self.src == "auto" and not mo: self._off()
        if self.t_motion is not None:
            self.t_motion -= dt
            if self.t_motion <= 0: self._off()
        if dis_p and self.src == "auto" and pr: self._off()
        # ---- timeout de operación manual
        if self.src == "manual" and self.mood != 0 and p["Met"] > 0 and self.t_manual >= p["Met"]: self._off()
        # ---- brillo maestro
        mb = i.get("MBr"); scale = 1.0 if mb is None else clamp(mb, 0, 100) / 100
        out = {k: v * scale for k, v in self.lv.items()}
        out.update({"M": self.mood, "2C": out_pulse2, "3C": out_pulse3})
        return out

@block("lighting-central")
class LightingCentral(Block):
    """Fan-out: reenvía las órdenes (M+, M-, Mood, Off, On, Alarm, DisP, DisPc, Rtd, Buzzer) para cablear a cada
    Lighting Controller; Na = nº de miembros encendidos (entradas Mem1..Mem32 = mood actual de cada uno)."""
    CMD = ("M+", "M-", "Mood", "Off", "On", "Alarm", "DisP", "DisPc", "Rtd", "Buzzer")
    def step(self, i, dt):
        out = {k: i[k] for k in self.CMD if i.get(k) is not None}
        out["Na"] = sum(1 for k in range(1, 33) if g(i, f"Mem{k}") not in (0, None))
        return out

@block("daylight-responsive-lighting")
class DaylightLighting(Block):
    """rec: regulación de luz constante. Con Act, Lc sube/baja Sts %/s si Br se aleja de Set más que Hys."""
    STATE = ("lc",)
    def init(self): self.lc = 0.0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset or not g(i, "Act", 1): self.lc = 0.0; return {"Lc": 0.0}
        br, st = g(i, "Br"), g(i, "Set")
        if br < st - self.p["Hys"]: self.lc = clamp(self.lc + self.p["Sts"] * dt, 0, 100)
        elif br > st + self.p["Hys"]: self.lc = clamp(self.lc - self.p["Sts"] * dt, 0, 100)
        return {"Lc": self.lc}

@block("dimmer")
class Dimmer(Block):
    """rec: Tg corto alterna (enciende a último valor o MaxD si Lv); Tg largo (>Di) regula alternando sentido;
    +/- regulan 2 % cada 0.2 s. Set fija nivel."""
    STATE = ("d", "last")
    def init(self): self.d = 0.0; self.last = self.p["MaxD"]; self.t = 0.0; self.down = False; self.dirn = 1; self.acc = 0.0
    def _ramp(self, sign, dt):
        self.acc += dt
        while self.acc >= 0.2:
            self.acc -= 0.2; self.d = clamp(self.d + sign * 2, self.p["MinD"], self.p["MaxD"])
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.d = 0.0; return {"D": 0.0, "S": 0}
        if not g(i, "DisPc"):
            r, f = self.edges("Tg", i.get("Tg"))
            if r: self.down = True; self.t = 0; self.ramping = False
            if self.down:
                self.t += dt
                if self.t >= p["Di"]:
                    if not getattr(self, "ramping", False):
                        self.ramping = True
                        if self.d == 0: self.d = p["MinD"] or 1
                    self._ramp(self.dirn, dt)
            if f and self.down:
                self.down = False
                if getattr(self, "ramping", False): self.dirn = -self.dirn; self.ramping = False
                elif self.d > 0: self.last = self.d; self.d = 0.0
                else: self.d = p["MaxD"] if p["Lv"] else self.last
            for key, s in (("+", 1), ("-", -1)):
                if g(i, key): self._ramp(s, dt)
        if i.get("Set") is not None: self.d = clamp(i["Set"], 0, 100)
        return {"D": self.d, "S": int(self.d > 0)}

@block("rgb-scene-controller")
class RgbSceneController(Block):
    """rec: cfg['scenes'] = [[r,g,b], ...]; + / - cambian de escena; AIs selecciona; O apaga; AI = escena directa."""
    STATE = ("sc",)
    def init(self): self.sc = -1
    def step(self, i, dt):
        scenes = self.cfg.get("scenes", [])
        if g(i, "Dis"): pass
        else:
            if self.rise("+", i.get("+")) and scenes: self.sc = (self.sc + 1) % len(scenes)
            if self.rise("-", i.get("-")) and scenes: self.sc = (self.sc - 1) % len(scenes)
            if i.get("AIs") is not None and 0 <= int(i["AIs"]) < len(scenes): self.sc = int(i["AIs"])
        if g(i, "R") or g(i, "O"): self.sc = -1
        r, gg, b = scenes[self.sc] if 0 <= self.sc < len(scenes) else (0, 0, 0)
        return {"AQr": r, "AQg": gg, "AQb": b, "AQs": self.sc, "AQa": int(self.sc >= 0)}

@block("hotel-lighting-controller")
class HotelLighting(Block):
    """rec (versión reducida): I1-20 alternan AQ1-20; IC apaga todo; R reset; ID puerta; Mo presencia
    mantiene QP; sin presencia TH s tras cerrar puerta apaga todo. Escenas: cfg['scenes'] = {"S10": {"AQ1": 50}}."""
    def init(self): self.aq = {f"AQ{k}": 0.0 for k in range(1, 21)}; self.t = 0.0
    def step(self, i, dt):
        if g(i, "Dis"): return {**self.aq, "QP": 0}
        for k in range(1, 21):
            if self.rise(f"I{k}", i.get(f"I{k}")): self.aq[f"AQ{k}"] = 0.0 if self.aq[f"AQ{k}"] else 100.0
        for s, acts in self.cfg.get("scenes", {}).items():
            if self.rise(s, i.get(s)): self.aq.update(acts)
        if self.rise("IC", i.get("IC")) or self.rise("R", i.get("R")): self.aq = {k: 0.0 for k in self.aq}
        pres = bool(g(i, "Mo")) or bool(g(i, "ID"))
        self.t = 0.0 if pres else self.t + dt
        if self.t >= self.p["TH"] and any(self.aq.values()): self.aq = {k: 0.0 for k in self.aq}
        return {**self.aq, "QP": int(pres), "QD": int(bool(g(i, "ID")))}

# =============================================================== PRESENCIA
@block("presence")
class Presence(Block):
    """doc: Act rising activa, falling inicia Pet; Ext extiende; AE cualquier cambio; Warn Tw antes del final;
    reactivación <30 s duplica Pet en la sesión."""
    STATE = ()
    def init(self):
        self.p_on = False; self.t = 0.0; self.dur = 0.0; self.timer = None; self.mult = 1; self.since_off = 1e9
        self.pon = self.poff = self.warn = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset:
            if self.p_on: self.poff = 0.1
            self.p_on = False; self.timer = None; return {"P": 0, "Poff": int(self.poff > 0), "Pon": 0, "Pd": 0, "Warn": 0}
        act = bool(g(i, "Act")); r, f = self.edges("Act", act)
        ae_change = self._prev.get("AE") is not None and self._prev["AE"] != bool(g(i, "AE")); self._prev["AE"] = bool(g(i, "AE"))
        ext = self.rise("Ext", i.get("Ext"))
        self.since_off += dt
        if r or ae_change:
            if not self.p_on:
                self.mult = 2 if self.since_off < 30 else 1
                self.p_on = True; self.dur = 0; self.pon = 0.1
            self.timer = None
        if f or ae_change:
            if self.p_on: self.timer = p["Pet"] * self.mult
        if ext and self.p_on and self.timer is not None: self.timer = p["Pet"] * self.mult
        if act: self.timer = None
        warn = 0
        if self.p_on:
            self.dur += dt
            if self.timer is not None:
                self.timer -= dt
                tw = p["Tw"]
                if tw >= 2 and self.timer <= tw and self.timer + dt > tw: self.warn = 0.1
                if self.timer <= 0: self.p_on = False; self.timer = None; self.poff = 0.1; self.since_off = 0; self.mult = 1 if False else self.mult
        out = {"P": int(self.p_on), "Pon": int(self.pon > 0), "Poff": int(self.poff > 0), "Pd": self.dur, "Warn": int(self.warn > 0)}
        self.pon = max(0, self.pon - dt); self.poff = max(0, self.poff - dt); self.warn = max(0, self.warn - dt)
        return out

# =============================================================== PERSIANAS / VENTANAS
@block("automatic-shading", "automatic-blinds-integrated", "skylight-blinds")
class AutomaticShading(Block):
    """doc: entradas/salidas/parámetros del catálogo. Posición 0=abierto..1=cerrado.
    rec: automático solar: entre amanecer+Spos y atardecer+Spoe, si el acimut está dentro de Dir±tolerancia
    y la elevación > 0, cierra a Rd (posición de sombreado). Al final aplica Spe (1 abrir, 2 cerrar)."""
    STATE = ("m", "sun_active")
    def init(self):
        self.m = Motion(0.0); self.target = 0.0; self.sun_active = False; self.sun_off_manual = False
        self.t = 0.0; self.lastdir = 0; self.wind = False; self.in_zone = False; self.stopped = True
        self.tp = 0.0; self.dt_hold = {}
    def _times(self):
        r, s = sun_times(self.ctx); return r, s
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        opd, cld = p.get("Opd", 75), p.get("Cld", p.get("Opd", 75))
        wa, dwc = bool(g(i, "Wa")), bool(g(i, "Dwc"))
        dispc = bool(g(i, "DisPc"))
        manual = False
        if reset: self.target = self.m.pos            # parada
        if wa: self.target = p.get("Wap", 0)
        elif dwc: self.target = 0.0
        elif not lock:
            if not dispc:
                moving = abs(self.m.pos - self.target) > 1e-9
                if self.rise("Tg", i.get("Tg")):
                    if moving: self.target = self.m.pos                       # parar
                    else:
                        self.lastdir = -self.lastdir if self.lastdir else (1 if self.m.pos < 0.5 else -1)
                        self.target = 1.0 if self.lastdir == 1 else 0.0
                    manual = True
                if self.rise("Co", i.get("Co")):
                    self.target = self.m.pos if (moving and self.target == 0.0) else 0.0; manual = True
                if self.rise("Cc", i.get("Cc")):
                    self.target = self.m.pos if (moving and self.target == 1.0) else 1.0; manual = True
                if self.rise("So", i.get("So")): self.target = p.get("Rd", p.get("Sop", 0.8)); manual = True
                for key, end in (("Po", 0.0), ("Pc", 1.0)):
                    held = bool(g(i, key)); was = self._prev.get("h" + key, False); self._prev["h" + key] = held
                    if held: self.target = end; manual = True
                    elif was: self.target = self.m.pos
            if i.get("Pos") is not None and self._prev.get("Posv") != i["Pos"]:
                v = i["Pos"]; self.target = clamp(v / 100 if v > 1 else v, 0, 1); manual = True
            self._prev["Posv"] = i.get("Pos")
        if manual: self.sun_off_manual = True
        # ---- automático solar
        sps = self.rise("Sps", i.get("Sps")); spr = self.rise("Spr", i.get("Spr"))
        if spr: self.sun_off_manual = False
        dis_sp = bool(g(i, "DisSp")); sp_run = 0
        if not (lock or wa or dwc):
            el, az = self.ctx.sun; rise_m, set_m = self._times(); now_m = self.ctx.now.hour * 60 + self.ctx.now.minute
            in_window = rise_m is not None and set_m is not None and rise_m + p.get("Spos", 30) <= now_m <= set_m + p.get("Spoe", -30)
            d = p.get("Dir", -1)
            in_dir = d >= 0 and abs((az - d + 180) % 360 - 180) <= (p.get("Dte", 85) if self.in_zone else p.get("Dts", 85))
            want = in_window and in_dir and el > 0 and not dis_sp and not self.sun_off_manual and (sps or self.sun_active or self._prev.get("enabled", True))
            if sps: self._prev["enabled"] = True
            if want and not self.sun_active: self.sun_active = True
            if want:
                self.in_zone = True; self.target = p.get("Rd", p.get("Sop", 0.8)); sp_run = 1
            elif self.sun_active:
                self.sun_active = False; self.in_zone = False; spe = int(p.get("Spe", 1))
                if spe == 1: self.target = 0.0
                elif spe == 2: self.target = 1.0
        mv = 0 if lock else self.m.move(self.target, dt, opd, cld)
        return {"Pos": self.m.pos, "Op": int(mv < 0), "Cl": int(mv > 0), "Sp": sp_run, "Wds": int(wa or dwc),
                "Off": int(lock), "TPos": self.target, "Im": int(mv != 0)}

@block("window")
class MotorWindow(Block):
    """Ventana motorizada. Pos 0 cerrada..1 abierta (rec: sentido contrario a las persianas)."""
    STATE = ("pos",)
    def init(self): self.m = Motion(0.0); self.target = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if reset: self.target = self.m.pos
        wp = bool(g(i, "Wp"))
        if wp: self.target = 0.0
        elif not lock:
            if self.rise("Co", i.get("Co")): self.target = 1.0
            if self.rise("Cc", i.get("Cc")): self.target = 0.0
            if self.rise("Tg", i.get("Tg")): self.target = 0.0 if self.m.pos > 0.5 or self.target > 0.5 else 1.0
            if self.rise("So", i.get("So")): self.target = p["SoPos"] / 100
            if self.rise("Po", i.get("Po")): self.target = clamp(self.m.pos + 0.25, 0, 1)
            if self.rise("Pc", i.get("Pc")): self.target = clamp(self.m.pos - 0.25, 0, 1)
            if i.get("Pos") is not None and self._prev.get("Pv") != i["Pos"]: self.target = clamp(i["Pos"] / 100, 0, 1)
            self._prev["Pv"] = i.get("Pos")
        if i.get("CPos") is not None: self.m.pos = clamp(i["CPos"] / 100, 0, 1)
        if i.get("Io"): self.m.pos = 1.0
        if i.get("Ic"): self.m.pos = 0.0
        # Motion usa 'mover hacia 0 = abrir' -> aquí invertimos: abrir = pos sube
        d = self.target - self.m.pos
        rate = 1 / max(p["Opd"] if d > 0 else p["Cld"], 1e-6)
        mv = 0 if lock or abs(d) < 1e-9 else (1 if d > 0 else -1)
        if mv: self.m.pos += mv * min(abs(d), rate * dt)
        return {"Pos": self.m.pos * 100, "TPos": self.target * 100, "Op": int(mv > 0), "Cl": int(mv < 0)}

@block("combined-window-contact")
class CombinedWindowContact(Block):
    """rec: S = 0 cerrada, 1 basculante, 2 abierta; Secured=0 con Open=0 -> 0 igualmente (aviso en cfg)."""
    def step(self, i, dt):
        s = 2 if g(i, "Open") else 1 if g(i, "Tilt") else 0
        return {"S": s}

def _central(name, cmds, counts=None):
    @block(name)
    class C(Block):
        def step(self, i, dt):
            out = {k: i[k] for k in cmds if i.get(k) is not None}
            if counts:
                for outk, f in counts.items(): out[outk] = f(i)
            return out
    C.__name__ = name.title().replace("-", "")
    return C
_central("shading-central", ("Tg", "Po", "Pc", "Co", "Cc", "So", "Sps", "DisSp", "Spr", "Wa", "Off", "Pos", "Slat", "T5", "DisPc"),
         {"No": lambda i: sum(1 for k in range(1, 33) if i.get(f"Pos{k}") is not None and i[f"Pos{k}"] <= 0.01),
          "Nc": lambda i: sum(1 for k in range(1, 33) if i.get(f"Pos{k}") is not None and i[f"Pos{k}"] >= 0.99)})
_central("window-central", ("Co", "Cc", "Tg", "Pos", "Po", "Pc", "So", "Wp", "Off"),
         {"No": lambda i: sum(1 for k in range(1, 33) if (i.get(f"Pos{k}") or 0) >= 99),
          "Nc": lambda i: sum(1 for k in range(1, 33) if i.get(f"Pos{k}") is not None and i[f"Pos{k}"] <= 1)})
_central("gate-overview", ("Tg", "Co", "Cc", "T5", "Off", "DisPc", "Po"),
         {"No": lambda i: sum(1 for k in range(1, 33) if (i.get(f"Pos{k}") or 0) >= 0.99),
          "Nc": lambda i: sum(1 for k in range(1, 33) if i.get(f"Pos{k}") is not None and i[f"Pos{k}"] <= 0.01)})
_central("security-overview", ("Tg", "Tgnp", "A", "Anp", "Ad", "Adnp", "Off", "Ca", "DisPc"),
         {"Na": lambda i: sum(1 for k in range(1, 33) if (i.get(f"S{k}") or 0) > 0)})
_central("central", ("Loff", "Lon", "La", "Ja", "Cu", "Cd", "S", "AS", "AD", "AR", "Sp", "St", "T5", "Dis"))

# =============================================================== ACCESOS
@block("garage-gate")
class GarageGate(Block):
    """Pos 0 cerrada..1 abierta. Tg: abrir→parar→cerrar. Spo/Spc (fotocélula) impiden movimiento.
    Io/Ic finales de carrera corrigen la posición. Wl parpadea Wlon/Wloff mientras se mueve.
    rec: salidas Op/Cl se mantienen durante el recorrido; Tg emite pulso Pd en cada orden."""
    STATE = ("pos",)
    def init(self): self.pos = 0.0; self.dirn = 0; self.target = None; self.last = -1; self.pulse = 0.0; self.t = 0.0
    @property
    def pos_(self): return self.pos
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        io, ic = bool(g(i, "Io")), bool(g(i, "Ic"))
        if io: self.pos = 1.0
        if ic: self.pos = 0.0
        if reset: self.dirn = 0; self.target = None
        cmd = None
        if not lock and not g(i, "DisPc"):
            if self.rise("Tg", i.get("Tg")):
                if self.dirn != 0: cmd = 0
                else: cmd = -self.last if self.last in (1, -1) and 0 < self.pos < 1 else (1 if self.pos < 0.5 else -1)
            if self.rise("Co", i.get("Co")): cmd = 0 if self.dirn == 1 else 1
            if self.rise("Cc", i.get("Cc")): cmd = 0 if self.dirn == -1 else -1
            if self.rise("Po", i.get("Po")): self.target = p["PoPos"]; cmd = 1 if self.pos < p["PoPos"] else -1
        if cmd is not None:
            self.dirn = cmd; self.pulse = p["Pd"]
            if cmd: self.last = cmd
            if cmd == 0: self.target = None
        if self.dirn == 1 and g(i, "Spo"): self.dirn = 0
        if self.dirn == -1 and g(i, "Spc"): self.dirn = 0; 
        if self.dirn:
            rate = 1 / (p["Opd"] if self.dirn == 1 else p["Cld"])
            self.pos = clamp(self.pos + self.dirn * rate * dt, 0, 1)
            lim = self.target if self.target is not None else (1.0 if self.dirn == 1 else 0.0)
            if (self.dirn == 1 and self.pos >= lim) or (self.dirn == -1 and self.pos <= lim): self.pos = lim; self.dirn = 0; self.target = None
        mv = self.dirn != 0; self.t = self.t + dt if mv else 0.0
        per = p["Wlon"] + p["Wloff"]
        wl = int(mv and per > 0 and (self.t % per) < p["Wlon"])
        out = {"Tg": int(self.pulse > 0), "Op": int(self.dirn == 1), "Cl": int(self.dirn == -1), "Im": int(mv), "Pos": self.pos, "Wl": wl}
        self.pulse = max(0, self.pulse - dt); return out

@block("access-controller", "authentication-nfc-code-touch")
class AccessController(Block):
    """rec: cfg['authorized'] = {"id": "nombre"}; Eid con id válido -> P (Pd s) y Txt; si no, Pd (denegado)."""
    def init(self): self.tp = self.td = 0.0; self.txt = ""
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        dur = self.p.get("Pd", self.p.get("Don", 3)) or 3
        eid = i.get("Eid")
        if eid not in (None, "") and not lock and self._prev.get("eid") != eid:
            users = self.cfg.get("authorized", {})
            if str(eid) in users: self.tp = dur; self.txt = f"{users[str(eid)]} {self.ctx.now:%Y-%m-%d %H:%M}"
            else: self.td = dur
        self._prev["eid"] = eid
        out = {"P": int(self.tp > 0), "Pd": int(self.td > 0), "Txt": self.txt,
               "As": int(self.tp > 0), "Ad": int(self.td > 0), "O1": int(self.tp > 0)}
        self.tp = max(0, self.tp - dt); self.td = max(0, self.td - dt); return out

# =============================================================== ALARMAS
@block("burglar-alarm")
class BurglarAlarm(Block):
    """doc: armado con/sin presencia, retardo de armado Ard, escalones Sa/Aa/Va/Ia/Ea/Ra, MaxA, Ca, contactos.
    S: 0 desarmada, 1 armada con sensores de movimiento, 2 sin. rec: segundo sensor (Spt) no modelado."""
    STATE = ("s",)
    def init(self):
        self.s = 0; self.arming = None; self.arm_mode = 1; self.alarm_t = None; self.cause = ""
        self.rtad = 0.0; self.ca = False
    def _sensors(self, i):
        wd = [k for k in ("Wc", "Dc", "Gb", "Ot") if g(i, k)]
        pres = ["P"] if g(i, "P") and self.s == 1 else []
        return wd + pres
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if reset or lock:
            self.s = 0; self.arming = None; self.alarm_t = None
        else:
            dis = bool(g(i, "DisPc"))
            if not dis:
                if self.rise("A", i.get("A")): self._arm(1, 0, i)
                if self.rise("Anp", i.get("Anp")): self._arm(2, 0, i)
                if self.rise("Ad", i.get("Ad")): self._arm(1, p["Ard"], i)
                if self.rise("Adnp", i.get("Adnp")): self._arm(2, p["Ard"], i)
                if self.rise("Tg", i.get("Tg")):
                    if self.s or self.arming: self.s = 0; self.arming = None; self.alarm_t = None
                    else: self._arm(1, p["Ard"], i)
                if self.rise("Tgnp", i.get("Tgnp")):
                    if self.s or self.arming: self.s = 0; self.arming = None; self.alarm_t = None
                    else: self._arm(2, p["Ard"], i)
            if self.arming is not None:
                self.arming[0] -= dt; self.rtad = max(0, self.arming[0])
                if self.arming[0] <= 0: self.s = self.arming[1]; self.arming = None
        if self.rise("Ca", i.get("Ca")): self.alarm_t = None
        active = self._sensors(i)
        if self.s and active and self.alarm_t is None and not self._prev.get("confirmed"): 
            self.alarm_t = 0.0; self.cause = ",".join(active)
        if self.alarm_t is not None:
            self.alarm_t += dt
            if p["MaxA"] > 0 and self.alarm_t >= p["MaxA"] + p["Rad"]: self.alarm_t = None
        if not self.s: self.alarm_t = None
        t = self.alarm_t
        def on(delay): return int(t is not None and t >= delay)
        return {"S": self.s, "Sa": on(p["Sad"]), "Aa": on(p["Aad"]), "Va": on(p["Vad"]), "Ia": on(p["Iad"]),
                "Ea": on(p["Ead"]), "Ra": on(p["Rad"]), "N": len(active), "Rtad": self.rtad if self.arming else 0,
                "Ca": self.cause if t is not None else "", "WDs": int(any(g(i, k) for k in ("Wc", "Dc")))}
    def _arm(self, mode, delay, i):
        if (g(i, "Wc") or g(i, "Dc")) and not self.p["Aoc"]: return          # contacto abierto bloquea
        if delay > 0: self.arming = [delay, mode]; self.rtad = delay
        else: self.s = mode; self.arming = None

@block("emergency-alarm")
class EmergencyAlarm(Block):
    """rec: Tg mantenido Ta s dispara; A dispara directo; Ca mantenido Tc s cancela (Cc confirma)."""
    STATE = ("a",)
    def init(self): self.a = 0; self.th = 0.0; self.tc = 0.0; self.p_on = self.p_off = 0.0; self.cc = 0.0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.a = 0
        old = self.a
        if g(i, "Tg"):
            self.th += dt
            if self.th >= self.p["Ta"]: self.a = 1
        else: self.th = 0
        if g(i, "A"): self.a = 1
        if g(i, "Ca"):
            self.tc += dt
            if self.tc >= self.p["Tc"] and self.a: self.a = 0; self.cc = 0.1
        else: self.tc = 0
        out = {"A": self.a, "Aon": int(self.a and not old), "Aoff": int(old and not self.a), "Ca": int(bool(g(i, "Ca"))), "Cc": int(self.cc > 0)}
        self.cc = max(0, self.cc - dt); return out

@block("fire-water-alarm")
class FireWaterAlarm(Block):
    """rec: S/F/T>Maxϑ -> alarma previa Pa; tras Mad s sin Ca -> alarma principal Ma. W -> agua (Pa/Ma igual).
    Sirenas Pas/Mas salvo modo silencioso Sm; MaxA limita la duración de sirena."""
    STATE = ("t",)
    def init(self): self.t = None; self.sir = 0.0; self.cancel_pre = False; self.at = 0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset or self.rise("Ca", i.get("Ca")): self.t = None; self.sir = 0
        trig = bool(g(i, "S") or g(i, "F") or g(i, "W") or (i.get("T") is not None and i["T"] > p["Maxϑ"]))
        if trig and self.t is None and not lock: self.t = 0.0; self.at = 1 if g(i, "S") else 2 if g(i, "W") else 3 if g(i, "F") else 4
        if self.t is not None:
            self.t += dt; self.sir += dt
        pa = self.t is not None
        ma = pa and self.t >= p["Mad"]
        siren_ok = not p["Sm"] and (p["MaxA"] <= 0 or self.sir <= p["MaxA"])
        return {"Pa": int(pa and not ma), "Ma": int(ma), "Pas": int(pa and not ma and siren_ok), "Mas": int(ma and siren_ok),
                "At": self.at if pa else 0, "Ta": self.t or 0}

@block("alarm-chain")
class AlarmChain(Block):
    """doc: 10 etapas; cada una se activa tras Rt s; si sigue activa tras la última, reinicia (MaxR repeticiones,
    0 ilimitadas). Au activa todas; AEs emergencia; Ca confirma."""
    def init(self): self.t = None; self.rep = 0; self.stage = 0; self.ae = 0; self.urgent = False
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset or self.rise("Ca", i.get("Ca")): self.t = None; self.stage = 0; self.rep = 0; self.ae = 0
        a, au, aes = bool(g(i, "A")), bool(g(i, "Au")), bool(g(i, "AEs"))
        if (a or au or aes) and self.t is None and not lock: self.t = 0.0; self.rep = 0
        if not (a or au or aes) and not aes: 
            if self.t is not None and not self.ae: self.t = None; self.stage = 0
        self.urgent = au
        if aes: self.ae = 1
        n = 0
        if self.t is not None:
            self.t += dt; rt = p["Rt"]
            if au or rt == 0: n = 10
            else:
                n = min(10, int(self.t // rt) + 1)
                if self.t >= rt * 10:
                    self.rep += 1; self.t = 0
                    if p["MaxR"] and self.rep >= p["MaxR"]: self.ae = 1
        self.stage = n
        out = {f"A{k}": int(k <= n) for k in range(1, 11)}
        out.update({"AEs": self.ae, "As": -1 if n == 10 and au else n})
        return out

# =============================================================== EXTERIOR / METEO
@block("irrigation")
class Irrigation(Block):
    """rec: Act arranca el programa (V1..V8 secuencial, Tv1..8 s cada válvula); Sel arranca una válvula;
    Ra (lluvia) o Raf (previsión) bloquean; Av = nº de válvula activa; MaxR/MaxRa no modelados."""
    def init(self): self.idx = None; self.t = 0.0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset or g(i, "Ra") or g(i, "Raf"): self.idx = None
        elif self.rise("Act", i.get("Act")): self.idx = 1; self.t = 0
        if i.get("Sel") and not lock and self._prev.get("sel") != i["Sel"]: self.idx = int(i["Sel"]); self.t = 0; self.single = True
        self._prev["sel"] = i.get("Sel")
        if self.idx:
            self.t += dt
            if self.t >= self.p.get(f"Tv{self.idx}", 600):
                self.t = 0; self.idx = None if getattr(self, "single", False) or self.idx >= 8 else self.idx + 1
                if self.idx is None: self.single = False
        out = {f"V{k}": int(self.idx == k) for k in range(1, 9)}
        out.update({"P": int(self.idx is not None), "Av": self.idx or 0}); return out

@block("wind-gauge")
class WindGauge(Block):
    """F (Hz) × factor F = velocidad; Avg media móvil Avgt s; AvgMax máx. de la media; Wa si Avg>W."""
    def init(self): self.buf = []; self.t = 0.0; self.mx = 0.0; self.wa = 0
    def step(self, i, dt):
        v = g(i, "F") * self.p["F"]; self.t += dt; self.buf.append((self.t, v))
        self.buf = [(t, x) for t, x in self.buf if self.t - t <= self.p["Avgt"]]
        avg = sum(x for _, x in self.buf) / len(self.buf); self.mx = max(self.mx, avg)
        if avg > self.p["W"]: self.wa = 1
        elif avg < self.p["W"] * 0.9: self.wa = 0
        return {"Avg": avg, "G": v, "AvgMax": self.mx, "Wa": self.wa}

@block("sunshine-input")
class SunshineInput(Block):
    """rec: Sunshine=1 si Lux > on (cfg 'on'=20000) y apaga bajo 'off'=10000; con Irr (W/m²) umbral 'irr_on'=300."""
    STATE = ("s",)
    def init(self): self.s = 0
    def step(self, i, dt):
        lux, irr = i.get("Lux", i.get("Luminosidad")), i.get("Irr", i.get("Irradiancia"))
        if lux is not None:
            if lux > self.cfg.get("on", 20000): self.s = 1
            elif lux < self.cfg.get("off", 10000): self.s = 0
        elif irr is not None:
            if irr > self.cfg.get("irr_on", 300): self.s = 1
            elif irr < self.cfg.get("irr_off", 200): self.s = 0
        return {"Sunshine": self.s}

@block("weather-service")
class WeatherService(Block):
    """Entradas = campos meteorológicos (Temperature, WindSpeed...) que cablea el adaptador HA (weather.*); se reenvían."""
    FIELDS = ("AbsoluteIrradiance Dewpoint ErrorForecast Gusts HazardWarning LastUpdate ParticulatePollution "
              "PerceivedTemperature Precipitation Pressure RelativeHumidity RelativeIrradiance SolarIrradiance Sunshine "
              "Temperature TimeWeatherData WeatherDataError WeatherType WindDirection WindSpeed").split()
    def step(self, i, dt):
        out = {k: i.get(k) for k in self.FIELDS if i.get(k) is not None}
        if "Temperature" in out and self.p.get("Offset"): out["Temperature"] += self.p["Offset"]
        return out

# =============================================================== ENERGÍA / SISTEMA
@block("standby-killer")
class StandbyKiller(Block):
    """rec: relé off si potencia < Umbral durante Retardo min y sala vacía; vuelve a on con movimiento."""
    STATE = ("relay",)
    def init(self): self.relay = 1; self.t = 0.0
    def step(self, i, dt):
        thr, delay = self.cfg.get("threshold", 10), self.cfg.get("delay_min", 5) * 60
        empty = bool(g(i, "Empty"))
        if g(i, "Motion"): self.relay = 1; self.t = 0
        if empty and g(i, "Power", 1e9) < thr:
            self.t += dt
            if self.t >= delay: self.relay = 0
        else: self.t = 0
        return {"Relay": self.relay}

@block("mixing-valve-controller")
class MixingValve(Block):
    """rec: PI en forma de velocidad cada St s; posición objetivo 0-100 % → V=0-10 V; O/C = mover hacia el objetivo
    a la velocidad 100 %/Td. Error si |ϑt-ϑc|>5 °C >10 min. Mode 1 = invertido."""
    STATE = ("target", "pos")
    def init(self): self.target = 0.0; self.pos = 0.0; self.t = 1e9; self.ep = 0.0; self.err_t = 0.0; self.integ = 0.0
    def step(self, i, dt):
        p = self.p
        if g(i, "Off"):
            self.target = {0: self.pos, 1: 100.0, 2: 0.0}[int(p["Offm"])]
        else:
            self.t += dt
            if self.t >= p["St"] and i.get("ϑt") is not None and i.get("ϑc") is not None:
                e = (i["ϑt"] - i["ϑc"]) * (-1 if p["Mode"] else 1)
                self.integ += p["Ki%"] / 100 * e * self.t
                self.target = clamp(p["Kp%"] / 100 * e * 100 + self.integ * 100, p["MinP"], p["MaxP"]); self.t = 0
        err = abs(g(i, "ϑt") - g(i, "ϑc")) > 5
        self.err_t = self.err_t + dt if err else 0
        d = self.target - self.pos; step = min(abs(d), 100 / max(p["Td"], 1e-6) * dt)
        mv = 0 if abs(d) < 1e-9 else (1 if d > 0 else -1); self.pos += mv * step
        v = (100 - self.pos) if p["Inv"] else self.pos
        return {"V": v / 10, "O": int(mv > 0), "C": int(mv < 0), "Error": int(self.err_t > 600)}
