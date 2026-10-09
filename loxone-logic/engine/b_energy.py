"""Energía: contadores, gestores de carga/energía y monitor de flujos."""
import datetime as _dt
from collections import deque
from runtime import Block, block, g, clamp

class Periods:
    """Lecturas por periodo (hoy/ayer/mes/mes anterior/año/año anterior) a partir de una lectura acumulada."""
    def __init__(self): self.mark = None; self.last = {"d": None, "m": None, "y": None}; self.start = {}; self.prev = {"d": 0.0, "m": 0.0, "y": 0.0}
    def update(self, total, now):
        key = {"d": now.date(), "m": (now.year, now.month), "y": now.year}
        if self.mark is None: self.mark = key; self.start = {k: total for k in key}
        for k in "dmy":
            if key[k] != self.mark[k]: self.prev[k] = total - self.start[k]; self.start[k] = total
        self.mark = key
        return {"d": total - self.start["d"], "ld": self.prev["d"], "m": total - self.start["m"], "lm": self.prev["m"],
                "y": total - self.start["y"], "ly": self.prev["y"]}

def _per(out, prefix, pr):
    out.update({f"Rd{prefix}": pr["d"], f"Rld{prefix}": pr["ld"], f"Rm{prefix}": pr["m"], f"Rlm{prefix}": pr["lm"],
                f"Ry{prefix}": pr["y"], f"Rly{prefix}": pr["ly"]})

@block("meter", "utility-meter", "energy-meter-1-phase-tree", "energy-meter-3-phase-tree", "modbus-energy-meter")
class Meter(Block):
    """Contador unidireccional. Si llega Mr (lectura del contador) se usa; si no, integra Pf (kW→kWh)."""
    STATE = ("total",)
    def init(self): self.total = 0.0; self.per = Periods(); self.started = False
    def step(self, i, dt):
        if self.rise("R", i.get("R")): self.total = 0.0; self.per = Periods()
        pf = g(i, "Pf", g(i, "P"))
        if i.get("Mr") is not None: self.total = i["Mr"]
        else: self.total += max(pf, 0) * dt / 3600
        mr = self.total + self.p.get("Mro", 0)
        out = {"Pf": pf, "Mr": mr}; _per(out, "", self.per.update(mr, self.ctx.now)); return out

class Bidir(Block):
    """Bidireccional: Pf>0 consumo (o descarga si storage), Pf<0 entrega (o carga)."""
    POS, NEG = "c", "d"
    STATE = ("tp", "tn")
    def init(self): self.tp = 0.0; self.tn = 0.0; self.pp = Periods(); self.pn = Periods()
    def step(self, i, dt):
        pos, neg = self.POS, self.NEG; p = self.p
        if self.rise("R", i.get("R")): self.tp = self.tn = 0.0; self.pp = Periods(); self.pn = Periods()
        pf = g(i, "Pf")
        if i.get(f"Mr{pos}") is not None: self.tp = i[f"Mr{pos}"]
        elif pf > 0: self.tp += pf * dt / 3600
        if i.get(f"Mr{neg}") is not None: self.tn = i[f"Mr{neg}"]
        elif pf < 0: self.tn += -pf * dt / 3600
        mp, mn = self.tp + p.get(f"Mro{pos}", 0), self.tn + p.get(f"Mro{neg}", 0)
        out = {"Pf": pf, f"Mr{pos}": mp, f"Mr{neg}": mn}
        _per(out, pos, self.pp.update(mp, self.ctx.now)); _per(out, neg, self.pn.update(mn, self.ctx.now))
        if "Slvl" in i: out["Slvl"] = i["Slvl"]
        return out
@block("meter-bidirectional")
class MeterBidir(Bidir): POS, NEG = "c", "d"
@block("meter-storage")
class MeterStorage(Bidir):
    """Pf>0 descarga (Mrd), Pf<0 carga (Mrc)."""
    POS, NEG = "d", "c"

@block("fixed-value-meter")
class FixedValueMeter(Block):
    STATE = ("total",)
    def init(self): self.total = 0.0
    def step(self, i, dt):
        if self.rise("R", i.get("R")): self.total = 0.0
        if g(i, "S"): self.total += self.p["Pf"] * dt / 3600
        return {"Mr": self.total + self.p["Mro"]}

@block("pulse-meter")
class PulseMeter(Block):
    """Np pulsos por unidad. F = frecuencia (Hz): flujo = F/Np·3600 unidades/h."""
    STATE = ("pulses",)
    def init(self): self.pulses = 0; self.per = Periods(); self.t = 0.0; self.win = deque(); self.pf = 0.0
    def step(self, i, dt):
        np_ = self.p["Np"] or 1
        if self.rise("R", i.get("R")): self.pulses = 0; self.per = Periods()
        self.t += dt
        if self.rise("P", i.get("P")): self.pulses += 1; self.win.append(self.t)
        while self.win and self.t - self.win[0] > 60: self.win.popleft()
        if i.get("F") is not None: self.pf = i["F"] / np_ * 3600
        else: self.pf = len(self.win) / np_ * 60 if self.t > 0 else 0
        mr = self.pulses / np_ + self.p["Mro"]
        out = {"Pf": self.pf, "Mr": mr}; _per(out, "", self.per.update(mr, self.ctx.now)); return out

class PulseBidir(Block):
    A, B = "c", "d"                # A: sentido positivo
    STATE = ("na", "nb")
    def init(self): self.na = self.nb = 0; self.pa = Periods(); self.pb = Periods(); self.t = 0.0; self.wa = deque(); self.wb = deque()
    def step(self, i, dt):
        p = self.p; self.t += dt
        if self.rise("R", i.get("R")): self.na = self.nb = 0; self.pa = Periods(); self.pb = Periods()
        a, b = self.A, self.B
        if self.rise(f"P{a}", i.get(f"P{a}")): self.na += 1; self.wa.append(self.t)
        if self.rise(f"P{b}", i.get(f"P{b}")): self.nb += 1; self.wb.append(self.t)
        for w in (self.wa, self.wb):
            while w and self.t - w[0] > 60: w.popleft()
        npa, npb = p.get(f"Np{a}") or 1, p.get(f"Np{b}") or 1
        fa = i.get(f"F{a}"); fb = i.get(f"F{b}")
        ra = fa / npa * 3600 if fa is not None else len(self.wa) / npa * 60
        rb = fb / npb * 3600 if fb is not None else len(self.wb) / npb * 60
        ma, mb = self.na / npa + p.get(f"Mro{a}", 0), self.nb / npb + p.get(f"Mro{b}", 0)
        out = {"Pf": ra - rb, f"Mr{a}": ma, f"Mr{b}": mb}
        _per(out, a, self.pa.update(ma, self.ctx.now)); _per(out, b, self.pb.update(mb, self.ctx.now))
        if "Slvl" in i: out["Slvl"] = i["Slvl"]
        return out
@block("pulse-meter-bidirectional")
class PulseMeterBidir(PulseBidir): A, B = "c", "d"
@block("pulse-meter-storage")
class PulseMeterStorage(PulseBidir):
    """Pf positivo = descarga (Pd), negativo = carga (Pc)."""
    A, B = "d", "c"

@block("load-manager")
class LoadManager(Block):
    """rec: si la potencia (Gpwr) supera MaxP desconecta la carga activa de menor prioridad (la de mayor número);
    reconecta de a una cuando Gpwr < MaxP-Hys y pasado Tr s. S1..S12 = demanda de cada carga (1 = quiere estar on).
    AvgP = media 15 min, ApPeak = máximo de la media."""
    def init(self): self.shed = set(); self.t = 0.0; self.win = deque(); self.peak = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        gp = g(i, "Gpwr", g(i, "Gi")); self.t += dt
        self.win.append((self.t, gp))
        while self.win and self.t - self.win[0][0] > 900: self.win.popleft()
        avg = sum(v for _, v in self.win) / len(self.win); self.peak = max(self.peak, avg)
        want = [k for k in range(1, 13) if g(i, f"S{k}")]
        if lock or reset: self.shed = set(); out = {f"L{k}": int(k in want and not lock) for k in range(1, 13)}
        else:
            if gp > p["MaxP"]:
                act = [k for k in want if k not in self.shed]
                if act and (self._prev.get("last", -99) + 10 <= self.t): self.shed.add(max(act)); self._prev["last"] = self.t
            elif gp < p["MaxP"] - p["Hys"] and self.shed and self._prev.get("last", -99) + 30 <= self.t:
                self.shed.discard(min(self.shed)); self._prev["last"] = self.t
            self.shed &= set(want)
            out = {f"L{k}": int(k in want and k not in self.shed) for k in range(1, 13)}
        out.update({"Ap": gp, "AvgP": avg, "MaxPe": p["MaxTp"], "ApPeak": self.peak, "TsU": 0})
        return out

@block("energy-manager", "energy-manager-2")
class EnergyManager(Block):
    """Reparto de excedente. cfg['loads'] = [{"kw": 2.0, "min_on": 60, "min_off": 60}, ...] (orden = prioridad).
    EM2: excedente = -Gpwr (+ batería si Soc>MinSoc) - O; EM1: P es el excedente directamente.
    Una carga se activa si el excedente libre ≥ su kW; se desactiva si el balance cae por debajo de −(histéresis).
    Prio=n fuerza la carga n. Next = siguiente carga candidata (EM2)."""
    def init(self): self.on = {}; self.since = {}; self.t = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt); self.t += dt
        loads = self.cfg.get("loads", [])
        if "P" in i: surplus = g(i, "P") - p["O"]
        else:
            surplus = -g(i, "Gpwr") - p["O"]
            soc = i.get("Soc")
            if i.get("Spwr") is not None and soc is not None and soc >= p.get("MinSoc", 0): surplus += -min(g(i, "Spwr"), 0) * 0   # batería se trata como producción ya medida en Gpwr
        if lock or reset: self.on = {}
        forced = int(g(i, "Prio"))
        used = sum(l["kw"] for k, l in enumerate(loads, 1) if self.on.get(k))
        free = surplus + 0                       # Gpwr ya incluye lo que consumen las cargas activas
        for k, l in enumerate(loads, 1):
            st = self.on.get(k, False); held = self.t - self.since.get(k, -1e9)
            if forced == k or g(i, f"L{k}"): self.on[k] = True; self.since[k] = self.t; continue
            if not st and free >= l["kw"] and held >= l.get("min_off", 0) and not lock:
                self.on[k] = True; self.since[k] = self.t; free -= l["kw"]
            elif st and free < -0.1 * l["kw"] and held >= l.get("min_on", 0):
                # sólo apaga la de menor prioridad activa
                if all(not self.on.get(j) for j in range(k + 1, len(loads) + 1)):
                    self.on[k] = False; self.since[k] = self.t; free += l["kw"]
        out = {f"L{k}": int(self.on.get(k, False)) for k in range(1, 13)}
        nxt = next((k for k, l in enumerate(loads, 1) if not self.on.get(k)), 0)
        out.update({"Next": nxt, "Re": max(free, 0), "MinSoc": p.get("MinSoc", 0)})
        return out

@block("energy-flow-monitor", "energy-monitor")
class EnergyFlowMonitor(Block):
    """Consumo = red(+import) + producción + almacenamiento(+descarga). Integra hoy: Ed, Id, Pd, Cd,
    autoconsumo Scd = min(P, C) acumulado, CO2 evitado = Scd·CO2, rendimiento Yd = Ed·Pre + Scd·Pri."""
    def init(self): self.day = None; self.acc = {}; self.tot = {"P": 0.0, "C": 0.0, "E": 0.0, "I": 0.0}; self.per = {k: Periods() for k in "PCE"}
    def step(self, i, dt):
        p = self.p; n = self.ctx.now
        if self.day != n.date(): self.day = n.date(); self.acc = {"Ed": 0.0, "Id": 0.0, "Pd": 0.0, "Cd": 0.0, "Scd": 0.0}
        gp, pp, sp = g(i, "Gpwr"), max(g(i, "Ppwr"), 0), g(i, "Spwr")
        cp = i["Cpwr"] if i.get("Cpwr") is not None else gp + pp + sp
        h = dt / 3600
        self.acc["Ed"] += max(-gp, 0) * h; self.acc["Id"] += max(gp, 0) * h; self.acc["Pd"] += pp * h; self.acc["Cd"] += max(cp, 0) * h
        self.acc["Scd"] += min(pp, max(cp, 0)) * h
        self.tot["P"] += pp * h; self.tot["C"] += max(cp, 0) * h; self.tot["E"] += max(-gp, 0) * h; self.tot["I"] += max(gp, 0) * h
        a = self.acc
        out = {"Gpwr": gp, "Ppwr": pp, "Cpwr": cp, "Spwr": sp, **a, "Co2d": a["Scd"] * p["CO2"],
               "Yd": a["Ed"] * p["Pre"] + a["Scd"] * p["Pri"], "Rest": cp - pp - sp - gp}
        if self.bid == "energy-monitor":
            pm = {k: self.per[k].update(self.tot[k], n) for k in "PCE"}
            out.update({"Pm": pm["P"]["m"], "Py": pm["P"]["y"], "Ptot": self.tot["P"], "Cm": pm["C"]["m"], "Cy": pm["C"]["y"],
                        "Ctot": self.tot["C"], "Em": pm["E"]["m"], "Ey": pm["E"]["y"], "Etot": self.tot["E"], "Itot": self.tot["I"]})
        return out
