"""Reconstrucción de los bloques de clima de Loxone para el motor HA.

Cada bloque lleva en SOURCE qué partes salen de la documentación pública
("doc") y cuáles son reconstruidas ("rec"). Lo "rec" hay que calibrarlo.
Interfaz común: block.step(inputs: dict, dt: float) -> dict de salidas.
"""
from dataclasses import dataclass, field
import math

def clamp(x, lo, hi): return max(lo, min(hi, x))

# ---------------------------------------------------------------- Heating Curve
@dataclass
class HeatingCurve:
    """Flow Temperature Calculator / Heating Curve.
    doc: entradas Tt, Ct, Dis; salidas Ft, Iv; S 0.05-2.5 (1.3), O (0), minFt 15, maxFt 65;
         Iv=1 si Ft sale de [minFt, maxFt]; curva dibujada para consigna 20 °C.
    rec: forma T = Tt + O + S*A*(Tt-Ct)^B (forma estándar 'Heizkurve'), A=1.4347, B=0.9.
         Ajusta con los 2 ejemplos de la ITC (error < 0.3 °C). Calibrar A, B con datos reales."""
    S: float = 1.3; O: float = 0.0; minFt: float = 15.0; maxFt: float = 65.0
    A: float = 1.4347; B: float = 0.9
    SOURCE = {"Iv, límites, rango S": "doc", "fórmula Ft": "rec"}
    def raw(self, Tt, Ct):
        d = max(Tt - Ct, 0.0)
        return Tt + self.O + self.S * self.A * d ** self.B
    def step(self, i, dt=0):
        if i.get("Dis"): Tt = 20.0
        else: Tt = i["Tt"]
        S = clamp(self.S, 0.05, 2.5)
        raw = self.raw(Tt, i["Ct"]) if S == self.S else Tt + self.O + S*self.A*max(Tt-i["Ct"],0)**self.B
        return {"Ft": clamp(raw, self.minFt, self.maxFt),
                "Iv": int(raw < self.minFt or raw > self.maxFt)}

# ------------------------------------------- Flow Temperature Controller (ITC)
@dataclass
class FlowTemperatureController:
    """Intelligent Temperature Controller.
    doc: AQr = Σ ΔT·tamaño; AQl = Σ demanda·área/área_total; G pondera la desviación
         (calor: Tt_corr = Tt + G·dev ; frío G=2, dev 1.5 sobre 20 → 17.0); I = incremento en fase
         calor/frío (0 = sin efecto); AQt = consigna de la sala determinante (máx. en calor, mín. en frío);
         Qp activa si alguna válvula > Str (35 %) y, con Tb, buffer alcanzado; Min 5, Max 40, B 5,
         S 0.5, N 0; Ps parada de bomba; ejemplos 20→30.9, 22→33.8 °C (ext 0, S .5).
    rec: composición completa (qué sala determina AQf) y la fórmula de la curva."""
    Min: float = 5; Max: float = 40; B: float = 5; S: float = 0.5; N: float = 0
    Str: float = 35; G: float = 1; I: float = 2
    SOURCE = {"AQr, AQl, G, Qp": "doc", "AQf (composición con I y curva)": "rec"}
    def step(self, i, dt=0):
        rooms = i["rooms"]                  # [{tt, tc, demand%, area, heating(bool)}]
        heating = i.get("heating", True)
        if not rooms: return {"Qe": 1}
        curve = HeatingCurve(S=self.S, O=self.N, minFt=self.Min, maxFt=self.Max)
        best = None; areas = sum(r["area"] for r in rooms) or 1
        for r in rooms:
            dev = (r["tt"] - r["tc"]) if heating else (r["tc"] - r["tt"])
            corr = r["tt"] + (self.G * dev if heating else -self.G * dev)
            if best is None or (heating and corr > best[0]) or (not heating and corr < best[0]):
                best = (corr, r)
        corr, room = best
        ft = curve.step({"Tt": corr, "Ct": i["to"]})["Ft"]
        inc = self.I if (room["demand"] > 0) else 0
        ft = clamp(ft + (inc if heating else -inc), self.Min, self.Max)
        qp = int(any(r["demand"] > self.Str for r in rooms)
                 and (i.get("tb") is None or (i["tb"] >= ft + self.B if heating else i["tb"] <= ft - self.B)))
        return {"AQt": room["tt"], "AQf": ft, "AQb": ft + (self.B if heating else -self.B), "Qp": qp,
                "AQr": sum(abs(r["tt"]-r["tc"]) * r["area"] for r in rooms),
                "AQl": sum(r["demand"] * r["area"] / areas for r in rooms), "Qe": 0}

# ------------------------------------------------------ Intelligent Room Controller
@dataclass
class RoomController:
    """Intelligent Room Controller (una sala).
    doc: ϑcc (conf. frío) 24.5, ϑch (conf. calor) 22.5; objetivo por defecto = punto medio;
         Eco Min = ϑch-ϑeh ; Eco Max = ϑcc+ϑec; ϑExc ≤ ϑd-0.5 (una conf.) o (ϑcc-ϑch)/2 (dos);
         heat-up/cool-down aprendido con la mediana de las últimas 8 operaciones, inicial 600 min/°C
         calor y 120 min/°C frío; PWM: ON = apertura % del intervalo (10-60 min, mín. 1 min);
         salida Shd con histéresis 0.4 °C; Boost si |gap| > 1.5 °C; ventana abierta corta demanda;
         si fuera de rango >1 min se cambia entre calor y frío.
    rec: PI de apertura de válvula (Kp, Ki, tiempo de muestreo) y umbral exacto calor/frío."""
    cc: float = 24.5; ch: float = 22.5; eh: float = 3.0; ec: float = 3.0; d: float = 1.0
    Kp: float = 50.0; Ki: float = 0.01; St: float = 60.0
    heat_rate: list = field(default_factory=list); cool_rate: list = field(default_factory=list)
    _integ: float = 0.0; _t: float = 0.0; _out_of_range: float = 0.0; _mode: str = "heat"
    SOURCE = {"consignas, eco, PWM, aprendizaje, Shd, Boost": "doc", "PI de válvula, umbral calor/frío": "rec"}
    def targets(self, mode):
        mid = (self.cc + self.ch) / 2
        return {"comfort": mid, "eco_min": self.ch - self.eh, "eco_max": self.cc + self.ec}.get(mode, mid)
    def rate(self, heating):
        r = self.heat_rate if heating else self.cool_rate
        if not r: return 600.0 if heating else 120.0
        s = sorted(r[-8:]); n = len(s)
        return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2
    def pwm_interval(self, ramp_c_per_min):
        r = abs(ramp_c_per_min)
        if r >= 1: return 10.0
        if r <= 0.1: return 60.0
        return 10 + (1 - r) / 0.9 * 50
    def step(self, i, dt):
        tc, tt = i["tc"], i["tt"]; window = i.get("window", 0)
        low, high = tt - self.d, tt + self.d
        if tc < low: want = "heat"
        elif tc > high: want = "cool"
        else: want = self._mode
        self._out_of_range = self._out_of_range + dt if want != self._mode else 0
        if self._out_of_range > 60: self._mode, self._out_of_range = want, 0
        heating = self._mode == "heat"
        err = (tt - tc) if heating else (tc - tt)
        self._t += dt
        if self._t >= self.St:
            self._integ = clamp(self._integ + self.Ki * err * self._t, 0, 100); self._t = 0
        demand = 0.0 if window or err <= 0 else clamp(self.Kp * err * 0.1 + self._integ, 0, 100)
        return {"mode": self._mode, "demand": demand, "boost": int(abs(tt - tc) > 1.5),
                "H": demand if heating else 0, "C": 0 if heating else demand}
    def shading(self, tc, shd_on, heating, ts):   # Shd con 0.4 °C de histéresis
        if tc >= ts: return 1
        if tc <= ts - 0.41: return 0
        return shd_on

# ------------------------------------------------ Heating and Cooling Controller
@dataclass
class ClimateController:
    """Heating and Cooling Controller (central de calor/frío).
    doc: patrón común de AC/Fan Coil Central: Mode 0 off, 1 auto por demanda, 2 solo calor,
         3 solo frío; umbrales SotH/SotC 30 %; mín. tiempo de marcha; límites exteriores ϑLimH 18,
         ϑLimC 15; Otm 0-3; salidas H/C; se pausa el lado con demanda opuesta.
    rec: desempate cuando hay demanda de ambos lados (gana la mayor, con inercia MinRt)."""
    mode: int = 1; SotH: float = 30; SotC: float = 30; limH: float = 18; limC: float = 15
    MinRt: float = 0; _state: str = "off"; _since: float = 1e9
    SOURCE = {"modos, umbrales, límites": "doc", "desempate": "rec"}
    def step(self, i, dt):
        self._since += dt
        dh, dc, to = i["demand_heat"], i["demand_cool"], i["to"]
        can_h = self.mode in (1, 2) and dh >= self.SotH and to <= self.limH
        can_c = self.mode in (1, 3) and dc >= self.SotC and to >= self.limC
        if self._state != "off" and self._since < self.MinRt * 60:
            want = self._state
        elif can_h and can_c: want = "heat" if dh >= dc else "cool"
        else: want = "heat" if can_h else "cool" if can_c else "off"
        if want != self._state: self._state, self._since = want, 0
        return {"H": int(self._state == "heat"), "C": int(self._state == "cool")}
