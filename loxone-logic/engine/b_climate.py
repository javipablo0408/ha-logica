"""Clima: Intelligent Room Controller y su cadena (central calor/frío, temperatura de impulsión,
fan coil, AC, HVAC). Reutiliza las fórmulas reconstruidas de climate.py.
Convención de enlace (en Loxone va por API): el IRC publica en su salida 'API' un dict
{"tt","tc","demand","area","heating"}; la central/ITC lo recibe en R1..R32."""
from collections import deque
from runtime import Block, block, g, clamp
from climate import HeatingCurve

CAL_MODES = {"comfort": 1, "eco": 0, "protect": 2}

@block("heating-curve")
class HeatingCurveBlock(Block):
    def init(self): self.h = HeatingCurve(S=self.p["S"], O=self.p["O"], minFt=self.p["minFt"], maxFt=self.p["maxFt"])
    def step(self, i, dt):
        return self.h.step({"Tt": g(i, "Tt", 20), "Ct": g(i, "Ct"), "Dis": g(i, "Dis")})

@block("intelligent-room-controller")
class IRC(Block):
    """doc: ver catálogo. Modos Mode: -1 off; 0-2 automático por calendario; 3 fijo (calor/frío auto), 4 solo calor, 5 solo frío.
    cfg: 'area' (m²), 'calendar': {"0": [{"days":[0..6],"start":"06:00","end":"22:00","state":"comfort"}], ...}.
    Temperaturas: confort ϑch/ϑcc, eco = ϑch-ϑeh / ϑcc+ϑec, protección ϑfp / ϑhp. C/E/Bp temporizan Cet/EBpet; P extiende Pet.
    rec: PI de apertura (Kp 50 %/°C·0.1, Ki 0.01), histéresis calor/frío ϑd, aprendizaje de calentamiento."""
    STATE = ("integ", "heat_mode")
    def init(self):
        self.integ = 0.0; self.heat_mode = True; self.st = "eco"; self.timer = 0.0; self.timer_state = None
        self.p_t = 0.0; self.eco_t = 0.0; self.win_t = 0.0; self.out_range_t = 0.0; self.samp = 0.0; self.dem = 0.0
        self.shd = 0; self.err_t = 0.0; self.pwm_t = 0.0; self.pwm_on = False; self.last_valve_move = 0.0
    def _cal_state(self, mode):
        n = self.ctx.now; m = n.hour * 60 + n.minute
        for e in self.cfg.get("calendar", {}).get(str(mode), []):
            if n.weekday() not in e.get("days", range(7)): continue
            s = int(e["start"][:2]) * 60 + int(e["start"][3:]); f = int(e["end"][:2]) * 60 + int(e["end"][3:])
            if (s <= m < f) if s <= f else (m >= s or m < f): return e["state"]
        return self.cfg.get("default_state", "eco")
    def targets(self, state):
        p = self.p
        if state == "comfort": return p["ϑch"], p["ϑcc"]
        if state == "eco": return p["ϑch"] - p["ϑeh"], p["ϑcc"] + p["ϑec"]
        return p["ϑfp"], p["ϑhp"]
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        tc = i.get("ϑc"); mode = int(g(i, "Mode", 0))
        if tc is None: self.err_t += dt
        else: self.err_t = 0
        err = int(tc is None)
        if lock or mode == -1 or tc is None:
            out = self._out(0, 0, "off", -1, i); out["Error"] = err; out["Os"] = -1; return out
        # --- estado de temperatura
        if mode in (0, 1, 2): state, osv = self._cal_state(mode), None
        else: state = "manual"
        if self.rise("C", i.get("C")): self.timer_state, self.timer = "comfort", p["Cet"]
        if self.rise("E", i.get("E")): self.timer_state, self.timer = "eco", p["EBpet"]
        if self.rise("Bp", i.get("Bp")): self.timer_state, self.timer = "protect", p["EBpet"]
        if self.timer_state:
            self.timer -= dt
            if self.timer <= 0: self.timer_state = None
            else: state = self.timer_state
        pres = bool(g(i, "P")) and not g(i, "DisP")
        if pres and state in ("eco",) and mode in (0, 1, 2):
            self.eco_t += dt
            if self.eco_t >= 1800: state = "comfort"
        else: self.eco_t = 0
        if pres and state == "comfort": self.p_t = p["Pet"]
        elif self.p_t > 0 and mode in (0, 1, 2) and state == "eco": self.p_t -= dt; state = "comfort" if self.p_t > 0 else state
        # --- ventana
        if g(i, "Dwc"):
            self.win_t += dt
            to = i.get("ϑo")
            harm = to is not None and ((to < tc and self.heat_mode) or (to > tc and not self.heat_mode))
            if self.win_t >= p["Ddwc"] and (harm or to is None): state = "protect"
        else: self.win_t = 0
        # --- consignas
        if state == "manual":
            tt = g(i, "ϑt", (p["ϑch"] + p["ϑcc"]) / 2); th = tc_ = tt
            th, tcool = tt, tt
            allow_h, allow_c = mode in (3, 4), mode in (3, 5)
            band = p["ϑd"]
        else:
            th, tcool = self.targets(state); band = p["ϑe"] if state == "eco" else p["ϑd"]
            allow_h = allow_c = True
        # --- decisión calor/frío (cambio sólo si fuera de rango >1 min)
        want = self.heat_mode
        if tc < th - band * 0 and tc < th: want = True
        elif tc > tcool: want = False
        if want != self.heat_mode:
            self.out_range_t += dt
            if self.out_range_t >= 60 and abs((th if want else tcool) - tc) >= band * 0.0: self.heat_mode = want; self.out_range_t = 0; self.integ = 0.0
        else: self.out_range_t = 0
        target = th if self.heat_mode else tcool
        errv = (target - tc) if self.heat_mode else (tc - target)
        # --- PI (valores %) con muestreo 60 s
        self.samp += dt
        if self.samp >= 60:
            self.integ = clamp(self.integ + 0.01 * errv * self.samp, 0, 100); self.samp = 0
        dem = clamp(5 * errv + self.integ, 0, 100) if errv > 0 else (self.integ * 0.5 if errv > -0.3 else 0)
        if (self.heat_mode and not allow_h) or (not self.heat_mode and not allow_c): dem = 0
        if g(i, "Dwc") and state == "protect" and errv < 0: dem = 0
        self.dem = dem
        # --- Shd
        ts = p["ϑsh"] if self.heat_mode else p["ϑsc"]
        if tc >= ts: self.shd = 1
        elif tc <= ts - 0.41: self.shd = 0
        os_ = {"eco": 0, "comfort": 1, "protect": 2, "manual": 3}[state]
        out = self._out(dem if self.heat_mode else 0, dem if not self.heat_mode else 0, state, 1 if self.heat_mode else -1, i)
        out.update({"ϑt": target, "Os": os_, "Shd": self.shd, "Boost": int(abs(target - tc) > 1.5 and dem > 0), "Error": err,
                    "API": {"tt": target, "tc": tc, "demand": dem, "area": self.cfg.get("area", 10), "heating": self.heat_mode, "state": state}})
        return out
    def _out(self, h, c, state, hcm, i):
        o = {"H": h, "C": c, "HC": h or c, "HCm": hcm, "Shd": getattr(self, "shd", 0), "Boost": 0, "Error": 0, "Os": 0, "ϑt": 0, "API": None, "Om": 0}
        for k in (1, 2, 3): o[f"H{k}"] = h if k == 1 else 0; o[f"C{k}"] = c if k == 1 else 0; o[f"HC{k}"] = (h or c) if k == 1 else 0
        return o

def rooms_from(i):
    return [v for k, v in sorted(((k, v) for k, v in i.items() if k.startswith("R") and k[1:].isdigit() and isinstance(v, dict)),
                                 key=lambda kv: int(kv[0][1:]))]

@block("intelligent-temperature-controller")
class FlowTempController(Block):
    STATE = ()
    def init(self): pass
    def step(self, i, dt):
        from climate import FlowTemperatureController
        p = self.p; rooms = [r for r in rooms_from(i) if r]
        if not rooms: return {"Qe": 1, "Qp": 0}
        heating = sum(1 for r in rooms if r["heating"]) >= len(rooms) / 2
        sub = [{"tt": r["tt"], "tc": r["tc"], "demand": r["demand"], "area": r["area"], "heating": heating} for r in rooms if r["heating"] == heating]
        f = FlowTemperatureController(Min=p["Min"], Max=p["Max"], B=p["B"], S=p["S"], N=p["N"], Str=p["Str"], G=p["G"], I=p["I"])
        out = f.step({"rooms": sub, "to": g(i, "ϑo"), "heating": heating, "tb": i.get("Tb")})
        if g(i, "Ib"): out["AQf"] = p["Max"] if heating else p["Min"]
        if g(i, "St"):
            out["Qp"] = 0; out["AQf"] = out["AQb"] = p["Min"] if heating else p["Max"]
        out["AQi"] = p["I"] if heating else -p["I"]; return out

class CentralHC(Block):
    """Núcleo común de centrales calor/frío: promedia la demanda de salas (R1..R32 del IRC o Dh/Dc),
    aplica umbrales, tiempos mínimos, límites exteriores (ϑLimH/ϑLimC con Otm) y modos."""
    def init(self):
        self.state = "off"; self.since = 1e9; self.t_run = 0.0; self.t2 = 0.0; self.avg = deque(); self.clk = 0.0
        self.fan_run = 0.0; self.filter_t = 0.0
    def demands(self, i):
        rooms = [r for r in rooms_from(i) if r]
        if rooms:
            h = [r["demand"] for r in rooms if r["heating"]]; c = [r["demand"] for r in rooms if not r["heating"]]
            return (sum(h) / len(h) if h else 0.0), (sum(c) / len(c) if c else 0.0), len(h), len(c)
        return g(i, "Dh"), g(i, "Dc"), int(g(i, "Dh") > 0), int(g(i, "Dc") > 0)
    def outdoor(self, i, dt):
        to = i.get("ϑo", self.ctx.outdoor_temp); self.clk += dt
        if to is not None:
            self.avg.append((self.clk, to))
            while self.avg and self.clk - self.avg[0][0] > 48 * 3600: self.avg.popleft()
        avg = sum(v for _, v in self.avg) / len(self.avg) if self.avg and self.clk >= 24 * 3600 else None
        otm = int(self.p.get("Otm", 2))
        used = {0: None, 1: avg, 2: self.ctx.outdoor_avg48 if self.ctx.outdoor_avg48 is not None else avg, 3: to}[otm]
        if used is None and otm in (1, 2): used = to   # sin media de 48 h todavía: usa la actual
        return to, avg, used
    def decide(self, i, dt, mode, sot_h, sot_c, minrt):
        dh, dc, nh, nc = self.demands(i)
        to, avg, used = self.outdoor(i, dt)
        limH, limC = self.p.get("ϑLimH", 18), self.p.get("ϑLimC", 15)
        can_h = mode in (0, 1) and dh >= sot_h and nh > 0 and (used is None or used <= limH)
        can_c = mode in (0, 2) and dc >= sot_c and nc > 0 and (used is None or used >= limC)
        self.since += dt
        if self.state != "off" and self.since < minrt * 60: want = self.state
        elif can_h and can_c: want = "heat" if dh * nh >= dc * nc else "cool"
        else: want = "heat" if can_h else "cool" if can_c else "off"
        if want != self.state: self.state, self.since = want, 0
        return self.state, to, avg, dh, dc

@block("climate-controller")
class ClimateControllerBlock(CentralHC):
    """doc: ver catálogo. Mode -1 off, 0 auto, 1 solo calor, 2 solo frío (-2 = automático por defecto).
    rec: etapa 2 tras Tt2s s (o inmediata bajo ϑminS2); Ah bajo ϑminHP; ventilador con post-ventilación Fod."""
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        mode = 0 if int(p["Mode"]) == -2 else int(p["Mode"])
        if lock: mode = -1
        if g(i, "Mh"): mode = 1
        st, to, avg, dh, dc = self.decide(i, dt, mode, p["Sot"], p["Sot"], p["MinHr"])
        if g(i, "Mh") and st != "heat": self.state, st, self.since = "heat", "heat", 0
        running = st != "off"
        self.t_run = self.t_run + dt if running else 0
        s2 = running and (self.t_run >= p["Tt2s"] or g(i, "B") or (to is not None and to < p["ϑminS2"] and st == "heat"))
        ah = int(st == "heat" and (g(i, "Ah") or g(i, "B") or (to is not None and to < p["ϑminHP"])))
        self.fan_run = p["Fod"] if running else max(0, self.fan_run - dt)
        fan = int(running or self.fan_run > 0 or g(i, "F"))
        if p["Dfc"]:
            self.filter_t += dt if fan else 0
        if self.rise("Cfc", i.get("Cfc")): self.filter_t = 0
        vd_ok = self.since >= p["Vd"]
        return {"H": int(st == "heat" and vd_ok), "H2": int(st == "heat" and s2), "C": int(st == "cool" and vd_ok),
                "C2": int(st == "cool" and s2), "Ah": ah, "Sv": int(st == "cool"), "F": fan,
                "Fc": int(bool(p["Dfc"]) and self.filter_t >= p["Dfc"] * 86400), "ϑoa": avg if avg is not None else -1000}

@block("ac-central-controller", "fan-coil-central-controller")
class AcCentral(CentralHC):
    """doc: Mode 0 off, 1 auto por demanda, 2 solo calor, 3 solo frío; SotH/SotC; MinHrt/MinCrt; Otm/ϑLimH/ϑLimC;
    Hac/Cac confirmaciones (fan coil); AvMode = modos permitidos."""
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        mode = int(p["Mode"]) if p["Mode"] >= 0 else 1
        m_int = {0: -1, 1: 0, 2: 1, 3: 2}.get(mode, 0)
        if lock: m_int = -1
        st, to, avg, dh, dc = self.decide(i, dt, m_int, p.get("SotH", 0), p.get("SotC", 0), max(p.get("MinHrt", 0), p.get("MinCrt", 0)))
        out = {"H": int(st == "heat"), "C": int(st == "cool"), "ϑoa": avg if avg is not None else -1000,
               "AvMode": {-1: 0, 0: 3, 1: 1, 2: 2}[m_int] if m_int >= 0 else 0, "Sv": int(st == "cool")}
        if "Hac" in i and not g(i, "Hac"): out["HeatOk"] = 0
        return out

class Fan(Block):
    def init(self):
        self.integ = 0.0; self.samp = 1e9; self.fan = 0.0; self.pause = 0.0; self.mode = int(self.p["Mode"])
        self.vint = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        tc, tt = g(i, "ϑc"), g(i, "ϑt"); mode = int(p["Mode"])
        if self.rise("Pt", i.get("Pt")): self.pause = p["Ptd"]
        self.pause = max(0, self.pause - dt)
        off = lock or mode == 0 or self.pause > 0 or bool(g(i, "Dwc"))
        heat_ok, cool_ok = (i.get("Ha") is None or g(i, "Ha")), (i.get("Ca") is None or g(i, "Ca"))
        heating = tt > tc
        if mode == 2: heating = True
        if mode == 3: heating = False
        err = abs(tt - tc)
        self.samp += dt
        fan_in = g(i, "Fan", -1)
        if self.samp >= p["FϑSt"]:
            self.integ = clamp(self.integ + p["FϑKI"] * err * self.samp, 0, 100); self.samp = 0
        auto = clamp(p["FϑKP"] * err * 0.1 + self.integ, 0, p["Fmax"]) if err > 0.2 else 0
        sm_max = self.cfg.get("silent_speed", 10); steps = int(self.cfg.get("fan_steps", 3))
        co2 = 0
        if "CO2" in i and "CO2t" in p and g(i, "CO2") > p["CO2t"]:
            co2 = clamp(p["Fco2KP"] * (g(i, "CO2") - p["CO2t"]) / 1000 + 0, 0, p["Fmax"]); auto = max(auto, co2)
        fan = auto if fan_in < 0 else fan_in
        if mode == 4: fan = max(fan, 30) if fan_in < 0 else fan
        if g(i, "Sm"): fan = min(fan, sm_max)
        if g(i, "Bm"): fan = 100
        valve = clamp(err * 5, 0, 10) if err > 0.2 else 0
        h = valve if heating and heat_ok and mode in (1, 2) else 0
        c = valve if (not heating) and cool_ok and mode in (1, 3) else 0
        if off: fan = h = c = 0
        fans = int(round(fan / 100 * steps)) if steps else 0
        return {"H": h, "C": c, "HC": h or c, "Fan": fan, "FanS": fans, "ϑc": tc, "ϑt": tt, "Mode": mode,
                "S": int(h > 0 or c > 0 or fan > 0)}
@block("fan-coil-unit-controller")
class FanCoilUnit(Fan): pass
@block("fan-coil-fresh-air-unit-controller")
class FanCoilFreshAir(Fan): pass

@block("ac-control")
class AcUnit(Block):
    """Unidad AC: reenvía consigna/modo/ventilador al aparato; Dwc apaga, Pt pausa Ptd s, límites minT/maxT."""
    STATE = ("on", "setp")
    def init(self): self.on = 0; self.setp = 22.0; self.pause = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if self.rise("Tg", i.get("Tg")): self.on ^= 1
        if self.rise("On", i.get("On")): self.on = 1
        if lock or reset: self.on = 0
        if i.get("ϑt") is not None: self.setp = clamp(i["ϑt"], p["minT"], p["maxT"])
        if self.rise("Pt", i.get("Pt")): self.pause = p["Ptd"]
        self.pause = max(0, self.pause - dt)
        active = self.on and not g(i, "Dwc") and self.pause <= 0
        return {"Status": int(bool(active)), "Mode": g(i, "Mode") if active else 0, "Fan": g(i, "Fan", 1) if active else 0,
                "Adir": g(i, "ADir"), "ϑt": self.setp, "ϑc": g(i, "ϑc")}

@block("hvac-controller")
class HvacController(CentralHC):
    """rec: termostato de etapas (W/W1, W2, Y, Y2, O/B, G, E). Calor si demanda media ≥ Sot; etapa 2 tras Tt2s;
    emergencia E por debajo de mioϑc... ver catálogo. Mode 0 auto, 1 calor, 2 frío, -1 off."""
    def step(self, i, dt):
        p = self.p; mode = int(g(i, "Mode", 0))
        st, to, avg, dh, dc = self.decide(i, dt, mode if mode >= 0 else -1, p["Sot"], p["Sot"], p["minO"] / 60)
        run = st != "off"; self.t2 = self.t2 + dt if run else 0
        stage2 = run and (self.t2 >= p["Tt2s"] or g(i, "B"))
        return {"W/W1": int(st == "heat"), "W2": int(st == "heat" and stage2), "Y": int(st == "cool"), "Y2": int(st == "cool" and stage2),
                "O/B": int(st == "cool"), "G": int(run or g(i, "Fan")), "E": int(st == "heat" and to is not None and to < p["maoϑh"] - 18 and stage2), "Hmd": int(st == "heat")}
