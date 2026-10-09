"""Bloques básicos: lógica, comparadores, temporizadores, pulsos, contadores, matemáticas,
analógicos, reguladores, selectores, pulsadores y datos. Defaults desde el catálogo."""
import math, random, re, ast, operator
from runtime import Block, block, g, clamp

def nums(i, prefix, n=None):
    vals = []
    k = 1
    while True:
        if n is not None and k > n: break
        key = f"{prefix}{k}"
        if key in i and i[key] is not None: vals.append(i[key])
        elif n is None and k > 32: break
        k += 1
        if n is None and k > 32: break
    return vals

# ============================================================== LÓGICA
@block("and")
class And(Block):
    def step(self, i, dt):
        v = nums(i, "I"); return {"O": int(bool(v) and all(v))}
@block("or")
class Or(Block):
    def step(self, i, dt):
        v = nums(i, "I"); return {"O": int(any(v))}
@block("exclusive-or")
class Xor(Block):
    def step(self, i, dt):
        v = nums(i, "I"); return {"O": int(sum(1 for x in v if x) % 2 == 1)}
@block("not")
class Not(Block):
    def step(self, i, dt): return {"O": int(not g(i, "I"))}

# ============================================================== COMPARADORES
def _cmp(name, op, out):
    @block(name)
    class C(Block):
        def step(self, i, dt): return {out: int(op(g(i, "V1"), g(i, "V2")))}
    return C
_cmp("greater", operator.gt, "G"); _cmp("less", operator.lt, "L")
_cmp("greater-or-equal", operator.ge, "Ge"); _cmp("less-or-equal", operator.le, "Le")
_cmp("equal", lambda a, b: abs(a - b) < 1e-9, "E"); _cmp("unequal", lambda a, b: abs(a - b) >= 1e-9, "U")

@block("comparator")
class Comparator(Block):
    """rec: histéresis sobre la diferencia V1-V2 (on ≥ Von, off ≤ Voff)."""
    STATE = ("o",)
    def init(self): self.o = 0
    def step(self, i, dt):
        d = g(i, "V1") - g(i, "V2")
        if d >= self.p["Von"]: self.o = 1
        elif d <= self.p["Voff"]: self.o = 0
        return {"De": self.o}

@block("threshold-switch")
class ThresholdSwitch(Block):
    STATE = ("o",)
    def init(self): self.o = 0; self.pon = 0; self.poff = 0
    def step(self, i, dt):
        v = g(i, "V"); old = self.o
        if v >= self.p["Von"]: self.o = 1
        elif v <= self.p["Voff"]: self.o = 0
        if self.o and not old: self.pon = self.p["Pd"]
        if old and not self.o: self.poff = self.p["Pd"]
        out = {"O": self.o, "On": int(self.pon > 0), "Off": int(self.poff > 0)}
        self.pon = max(0, self.pon - dt); self.poff = max(0, self.poff - dt)
        return out

@block("differential-threshold-switch")
class DiffThreshold(Block):
    """doc: D>0 activa entre T y T+D; D<0 activa al superar T y apaga bajo T-|D|... (ver catálogo)."""
    STATE = ("o",)
    def init(self): self.o = 0; self.pon = 0; self.poff = 0
    def step(self, i, dt):
        v = g(i, "V"); T, D = self.p["T"], self.p["D"]; old = self.o
        if D >= 0: self.o = int(T <= v <= T + D)
        else:
            if v > T: self.o = 1
            elif v < T + D: self.o = 0
        out = {"T": self.o, "Teon": int(self.o and not old), "Teoff": int(old and not self.o)}
        return out

@block("switch")
class Switch(Block):
    STATE = ("o",)
    def init(self): self.o = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        old = self.o
        if lock: self.o = 0
        elif not g(i, "DisPc"):
            if self.rise("Tg", i.get("Tg")): self.o ^= 1
            if self.rise("On", i.get("On")): self.o = 1
        if reset: self.o = 0
        return {"O": self.o, "On": int(self.o and not old), "Off": int(old and not self.o)}

# ============================================================== BIESTABLES
@block("flipflop-sr")
class FlipSR(Block):
    """SR = Set domina."""
    STATE = ("o",); DOM = "S"
    def init(self): self.o = 0
    def step(self, i, dt):
        s, r = bool(g(i, "S")), bool(g(i, "R"))
        if self.rise("Tg", i.get("Tg")): self.o ^= 1
        if s and r: self.o = 1 if self.DOM == "S" else 0
        elif s: self.o = 1
        elif r: self.o = 0
        return {"O": self.o}
@block("flipflop-rs")
class FlipRS(FlipSR): DOM = "R"

# ============================================================== TEMPORIZADORES
class Timed(Block):
    def init(self): self.o = 0; self.t = 0.0; self.running = False; self.sub()
    def sub(self): pass

@block("switch-on-delay")
class SwitchOnDelay(Timed):
    STATE = ("o",)
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        tr = bool(g(i, "Tr"))
        if lock or reset or not tr: self.t = 0; self.o = 0
        else:
            self.t += dt
            if self.t >= self.p["Don"]: self.o = 1
        return {"O": self.o}

@block("saving-switch-on-delay")
class SavingSwitchOnDelay(Timed):
    """rec: el tiempo acumulado se conserva cuando Tr baja (solo Off lo reinicia)."""
    STATE = ("o", "t")
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.t = 0; self.o = 0
        elif g(i, "Tr"):
            self.t += dt
            if self.t >= self.p["Don"]: self.o = 1
        elif self.o: self.o = 0
        return {"O": self.o}

@block("switch-off-delay")
class SwitchOffDelay(Timed):
    STATE = ("o",)
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        tr = bool(g(i, "Tr"))
        if lock or reset: self.o = 0; self.running = False
        elif tr: self.o = 1; self.running = False; self.t = 0
        elif self.o:
            if not self.running: self.running = True; self.t = 0
            self.t += dt
            if self.t >= self.p["Don"]: self.o = 0; self.running = False
        return {"O": self.o}

@block("switch-on-and-off-delay")
class SwitchOnOffDelay(Timed):
    STATE = ("o",)
    def sub(self): self.last = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        tr = int(bool(g(i, "Tr")))
        if lock or reset: self.o = 0; self.t = 0; self.last = tr; return {"O": 0}
        if tr != self.last: self.t = 0; self.last = tr
        self.t += dt
        if tr and not self.o and self.t >= self.p["Don"]: self.o = 1
        if not tr and self.o and self.t >= self.p["Doff"]: self.o = 0
        return {"O": self.o}

@block("monoflop")
class Monoflop(Timed):
    STATE = ("o",)
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.o = 0; self.t = 0
        elif self.rise("Tr", i.get("Tr")): self.o = 1; self.t = 0
        elif self.o:
            self.t += dt
            if self.t >= self.p["D"]: self.o = 0
        return {"O": self.o}

# ============================================================== PULSOS
@block("edge-detection")
class EdgeDetection(Block):
    def init(self): self.tp = self.tn = self.tf = 0.0
    def step(self, i, dt):
        r, f = self.edges("I", i.get("I"))
        pd = self.p["Pd"]
        if r: self.tn = pd; self.tp = pd
        if f: self.tf = pd; self.tp = pd
        out = {"P": int(self.tp > 0), "On": int(self.tn > 0), "Off": int(self.tf > 0)}
        self.tp = max(0, self.tp - dt); self.tn = max(0, self.tn - dt); self.tf = max(0, self.tf - dt)
        return out

@block("pulse-by")
class PulseBy(Block):
    """rec: pulso de duración Pd en cada flanco de subida de T."""
    def init(self): self.t = 0.0
    def step(self, i, dt):
        if self.rise("T", i.get("T")): self.t = self.p["Pd"]
        o = int(self.t > 0); self.t = max(0, self.t - dt); return {"P": o}

@block("pulse-generator")
class PulseGenerator(Block):
    """rec: oscila Don/Doff mientras Off=0; Inv invierte la salida."""
    def init(self): self.t = 0.0
    def step(self, i, dt):
        if g(i, "Off"): self.t = 0; return {"P": 0}
        per = self.p["Don"] + self.p["Doff"]
        self.t = (self.t + dt) % per if per > 0 else 0
        on = int(self.t < self.p["Don"])
        return {"P": (1 - on) if g(i, "Inv") else on}

@block("pulse-at")
class PulseAt(Block):
    """rec: pulso Don cuando la hora llega a cfg['time'] (minutos desde medianoche)."""
    def init(self): self.t = 0.0; self.fired = None
    def step(self, i, dt):
        now = self.ctx.now; mins = now.hour * 60 + now.minute
        if not g(i, "Off") and mins == self.cfg.get("time", 0) and self.fired != (now.date(), mins):
            self.fired = (now.date(), mins); self.t = self.p["Don"]
        o = int(self.t > 0); self.t = max(0, self.t - dt); return {"O": o}

@block("delayed-pulse")
class DelayedPulse(Block):
    def init(self): self.phase = 0; self.t = 0.0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.phase = 0; return {"P": 0}
        if self.rise("P", i.get("P")): self.phase, self.t = 1, 0
        out = 0
        if self.phase == 1:
            self.t += dt
            if self.t >= self.p["Dd"]: self.phase, self.t = 2, 0
        if self.phase == 2:
            out = 1; self.t += dt
            if self.t >= self.p["Dp"]: self.phase = 0
        return {"P": out}

@block("edge-triggered-wiping-relay")
class WipingRelay(Block):
    def init(self): self.cyc = 0; self.t = 0.0; self.on = True
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.cyc = 0; return {"P": 0}
        if self.rise("Tr", i.get("Tr")) and self.cyc == 0: self.cyc = int(self.p["C"]); self.t = 0; self.on = True
        if self.cyc == 0: return {"P": 0}
        out = int(self.on); self.t += dt
        lim = self.p["Don"] if self.on else self.p["Doff"]
        if self.t >= lim:
            self.t = 0
            if self.on: self.on = False
            else: self.on = True; self.cyc -= 1
        return {"P": out}

@block("pulse-width-modulation")
class PWM(Block):
    def init(self): self.t = 0.0
    def step(self, i, dt):
        if g(i, "Off"): self.t = 0; return {"PWM": 0}
        P = self.p["P"]; self.t = (self.t + dt) % P if P > 0 else 0
        duty = clamp(g(i, "V"), 0, 10) / 10
        return {"PWM": int(self.t < duty * P)}

# ============================================================== CONTADORES
@block("counter")
class Counter(Block):
    STATE = ("v",)
    def init(self): self.v = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.v = 0
        elif self.rise("C", i.get("C")): self.v += 1
        return {"V": self.v, "Lr": int(self.v >= self.p["L"])}

@block("up-down-counter")
class UpDownCounter(Block):
    STATE = ("v", "o")
    def init(self): self.v = self.p["Sv"]; self.o = 0
    def step(self, i, dt):
        if self.rise("R", i.get("R")): self.v = self.p["Sv"]
        if self.rise("C", i.get("C")): self.v += -1 if g(i, "Dir") else 1
        if self.v >= self.p["Von"]: self.o = 1
        elif self.v <= self.p["Voff"]: self.o = 0
        return {"V": self.v, "O": self.o}

@block("maintenance-counter")
class RuntimeCounter(Block):
    STATE = ("total", "since")
    def init(self): self.total = 0.0; self.since = 0.0; self.lst = 0.0
    def step(self, i, dt):
        if g(i, "En"): self.total += dt; self.since += dt; self.lst = self.ctx.now.timestamp()
        if self.rise("Rmc", i.get("Rmc")): self.since = 0
        div = [1, 60, 3600, 86400][int(self.p["Tu"])]
        mi = self.p["Mi"]
        return {"Me": int(mi > 0 and self.since >= mi), "To": self.total / div, "Lst": self.lst,
                "Rtm": max(0, (mi - self.since) / div) if mi > 0 else 0}

# ============================================================== MATEMÁTICAS
@block("add-2-way", "add-4-way")
class Add(Block):
    def step(self, i, dt): return {"O": sum(nums(i, "V"))}
@block("subtract")
class Sub(Block):
    def step(self, i, dt): return {"O": g(i, "V1") - g(i, "V2")}
@block("multiply")
class Mul(Block):
    """doc: una entrada desconectada o a 0 se ignora."""
    def step(self, i, dt):
        v = [x for x in nums(i, "V") if x != 0]
        r = 1.0
        for x in v: r *= x
        return {"O": r if v else 0}
@block("divide")
class Div(Block):
    """rec: divisor 0 -> 0 (comportamiento no documentado)."""
    def step(self, i, dt):
        b = g(i, "V2"); return {"O": g(i, "V1") / b if b else 0}
@block("modulo")
class Mod(Block):
    """rec: Int = parte entera del cociente, Dec = resto V1 mod V2."""
    def step(self, i, dt):
        a, b = g(i, "V1"), g(i, "V2")
        if not b: return {"Int": 0, "Dec": 0}
        return {"Int": int(a // b), "Dec": math.fmod(a, b)}
@block("average")
class Avg(Block):
    def step(self, i, dt):
        v = nums(i, "V", 4); return {"Avg": sum(v) / len(v) if v else 0}
@block("moving-average")
class MovAvg(Block):
    STATE = ("buf",)
    def init(self): self.buf = []; self.t = 0.0; self.avg = 0
    def step(self, i, dt):
        v = g(i, "V")
        if g(i, "R"): self.buf = []; return {"Avg": v}
        self.t += dt
        if self.t >= self.p["C"] or not self.buf:
            self.t = 0; self.buf.append(v); self.buf = self.buf[-int(self.p["N"]):]
        return {"Avg": sum(self.buf) / len(self.buf)}
@block("minmax")
class MinMax(Block):
    def step(self, i, dt):
        v = nums(i, "V", 4)
        if g(i, "Off") or not v: return {"Min": 0, "Max": 0}
        return {"Min": min(v), "Max": max(v)}
@block("minmax-since-reset")
class MinMaxSince(Block):
    STATE = ("mn", "mx")
    def init(self): self.mn = None; self.mx = None
    def step(self, i, dt):
        v = g(i, "V")
        if self.rise("R", i.get("R")) or self.mn is None: self.mn = self.mx = v
        self.mn = min(self.mn, v); self.mx = max(self.mx, v)
        return {"Min": self.mn, "Max": self.mx}
@block("integer")
class Integer(Block):
    """rec: trunca a entero."""
    def step(self, i, dt): return {"O": int(g(i, "I", g(i, "V")))}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FN = {"ABS": abs, "SQRT": math.sqrt, "LN": math.log, "LOG": math.log10, "EXP": math.exp, "SIN": math.sin,
       "COS": math.cos, "TAN": math.tan, "ARCSIN": math.asin, "ARCCOS": math.acos, "ARCTAN": math.atan,
       "SINH": math.sinh, "COSH": math.cosh, "TANH": math.tanh, "RAD": math.radians, "DEG": math.degrees,
       "SIGN": lambda x: (x > 0) - (x < 0), "INT": int, "MIN": min, "MAX": max}
_CMP = {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

def _split_args(s):
    out, d, cur = [], 0, ""
    for ch in s:
        if ch == "(": d += 1
        if ch == ")": d -= 1
        if ch == ";" and d == 0: out.append(cur); cur = ""
        else: cur += ch
    out.append(cur); return out

def eval_formula(f, vars_):
    """Evalúa fórmulas Loxone: I1-I4, + - * / ^, funciones (IF(c;a;b), MIN(a;b)...), decimal con coma."""
    f = f.replace(",", ".").replace("^", "**")
    def ev(s):
        s = s.strip()
        m = re.fullmatch(r"IF\((.*)\)", s, re.S)
        if m:
            c, a, b = _split_args(m.group(1)); return ev(a) if cond(c) else ev(b)
        m = re.fullmatch(r"(MIN|MAX)\((.*)\)", s, re.S)
        if m:
            a = [ev(x) for x in _split_args(m.group(2))]; return _FN[m.group(1)](*a)
        node = ast.parse(re.sub(r"\b(PI)\b", "PI()", s), mode="eval").body
        return walk(node)
    def cond(c):
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if op in c:
                l, r = c.split(op, 1); return _CMP[op](ev(l), ev(r))
        return bool(ev(c))
    def walk(n):
        if isinstance(n, ast.Constant): return float(n.value)
        if isinstance(n, ast.Name): return float(vars_[n.id.upper()])
        if isinstance(n, ast.BinOp): return _OPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp): return _OPS[type(n.op)](walk(n.operand))
        if isinstance(n, ast.Call):
            name = n.func.id.upper()
            if name == "PI": return math.pi
            return float(_FN[name](*[walk(a) for a in n.args]))
        raise ValueError("no permitido")
    return ev(f)

@block("formula")
class Formula(Block):
    def step(self, i, dt):
        v = {f"I{k}": g(i, f"I{k}") for k in range(1, 5)}
        try:
            r = eval_formula(self.cfg.get("formula", "I1"), v)
            if math.isnan(r) or math.isinf(r): raise ValueError
            return {"R": r, "E": 0}
        except Exception:
            return {"R": 0, "E": 1}

# ============================================================== ANALÓGICOS
@block("analogue-memory")
class AnalogueMemory(Block):
    STATE = ("v",)
    def init(self): self.v = 0
    def step(self, i, dt):
        if self.rise("Off", i.get("Off")): self.v = 0
        elif self.rise("Set", i.get("Set")): self.v = g(i, "V")
        return {"V": self.v}
@block("analogue-multiplexer-2-way")
class Mux2(Block):
    def step(self, i, dt):
        if g(i, "Off"): return {"V": 0}
        return {"V": g(i, "V2") if self.p["Sel"] else g(i, "V1")}
@block("analogue-multiplexer-4-way")
class Mux4(Block):
    def step(self, i, dt):
        if g(i, "Off"): return {"V": 0}
        return {"V": g(i, f"V{int(clamp(self.p['Sel'], 1, 4))}")}
@block("scaler")
class Scaler(Block):
    def step(self, i, dt):
        p = self.p; v = g(i, "V")
        if p["V2"] == p["V1"]: return {"Sv": p["Sv1"]}
        return {"Sv": p["Sv1"] + (v - p["V1"]) * (p["Sv2"] - p["Sv1"]) / (p["V2"] - p["V1"])}
@block("analogue-min-max-limiter")
class Limiter(Block):
    def step(self, i, dt): return {"V": clamp(g(i, "V"), self.p["Min"], self.p["Max"])}
@block("analogue-watchdog")
class AnalogueWatchdog(Block):
    STATE = ("te",)
    def init(self): self.te = 0
    def step(self, i, dt):
        if g(i, "Off"): self.te = 0
        else:
            v = g(i, "V")
            if v >= self.p["TU"] or v <= self.p["TL"]: self.te = 1
            else: self.te = 0
        return {"Te": self.te}
@block("value-validator")
class ValueValidator(Block):
    """rec: mantiene el último valor válido; E si fuera de rango o sin valor válido en Tmc s."""
    def init(self): self.last = self.p["D"]; self.since = 0.0
    def step(self, i, dt):
        v = i.get("V"); p = self.p
        if v is not None and p["Min"] <= v <= p["Max"]: self.last = v; self.since = 0
        else: self.since += dt
        err = int(self.since > p["Tmc"] or (v is not None and not p["Min"] <= v <= p["Max"]))
        return {"V": self.last, "E": err}

@block("dewpoint-calculator")
class Dewpoint(Block):
    """Fórmula de Magnus."""
    def step(self, i, dt):
        t, h = clamp(g(i, "ϑ", g(i, "T")), -65, 60), clamp(g(i, "H"), 1, 100)
        a, b = 17.62, 243.12
        gam = math.log(h / 100) + a * t / (b + t)
        return {"ϑd": b * gam / (a - gam) + self.p["O"]}

# ============================================================== REGULADORES
@block("pi-controller", "pid-controller")
class PID(Block):
    """rec: PI/PID posicional discreto con banda muerta Th y anti-windup por límites."""
    STATE = ("integ", "co")
    def init(self): self.integ = 0.0; self.co = 0.0; self.t = 1e9; self.prev_e = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.integ = 0; self.co = 0; return {"CO": 0}
        if not g(i, "Auto", 1): self.co = clamp(p["Mv"], p["Min"], p["Max"]); return {"CO": self.co}
        self.t += dt
        if self.t >= p["St"]:
            e = p["SP"] - g(i, "PV"); h = self.t; self.t = 0
            if abs(e) >= p["Th"] or self.co != 0:
                self.integ = clamp(self.integ + p.get("Ki", 0) * e * h, p["Min"], p["Max"])
                d = p.get("Kd", 0) * (e - self.prev_e) / h if "Kd" in p else 0
                self.co = clamp(p["Kp"] * e + self.integ + d, p["Min"], p["Max"])
            self.prev_e = e
        return {"CO": self.co}

@block("2-position-controller")
class TwoPos(Block):
    STATE = ("o",)
    def init(self): self.o = 0
    def step(self, i, dt):
        if g(i, "Off"): self.o = 0; return {"O": 0}
        p = self.p; v = g(i, "PV")
        if v < p["SP"] - p["Hys"] / 2: self.o = 1
        elif v > p["SP"] + p["Hys"] / 2: self.o = 0
        return {"O": (1 - self.o) if p["Inv"] else self.o}

@block("3-position-controller")
class ThreePos(Block):
    """rec: O1 si PV<SP1, O2 si PV>SP2, ambas apagadas entre medias."""
    def step(self, i, dt):
        if g(i, "Off"): return {"O1": 0, "O2": 0}
        v = g(i, "PV"); return {"O1": int(v < self.p["SP1"]), "O2": int(v > self.p["SP2"])}

@block("ramp-controller")
class Ramp(Block):
    """doc: rampa entre dos niveles con paso Sts cada 100 ms; S elige L1/L2; St detiene."""
    STATE = ("v",)
    def init(self): self.v = self.p["Sv"]; self.acc = 0.0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.v = self.p["Sv"]; return {"V": self.v}
        if g(i, "St"): return {"V": self.v}
        target = self.p["L2"] if g(i, "S") else self.p["L1"]
        self.acc += dt
        while self.acc >= 0.1:
            self.acc -= 0.1; step = self.p["Sts"]
            if abs(target - self.v) <= step: self.v = target
            else: self.v += step if target > self.v else -step
        return {"V": self.v}

# ============================================================== SELECTORES
@block("radio-buttons", "radio-buttons-16x")
class RadioButtons(Block):
    STATE = ("n",)
    def init(self): self.n = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        mx = int(self.p["Max"]) if "Max" in self.p else 8
        if lock or reset: self.n = 0
        elif not g(i, "DisPc"):
            for k in range(1, mx + 1):
                if self.rise(f"I{k}", i.get(f"I{k}")): self.n = 0 if self.n == k and False else k
            if self.rise("+", i.get("+")): self.n = self._next(self.n, 1, mx)
            if self.rise("-", i.get("-")): self.n = self._next(self.n, -1, mx)
        if i.get("Sel") is not None: self.n = int(clamp(i["Sel"], 0, mx))
        out = {f"O{k}": int(self.n == k) for k in range(1, mx + 1)}; out["N"] = self.n
        return out
    def _next(self, n, d, mx):
        lo = 1 if self.p.get("Sk0") else 0
        n += d
        if n > mx: n = lo
        if n < lo: n = mx
        return n

@block("selection-switch-onoff")
class SelectionOnOff(Block):
    STATE = ("o",)
    def init(self): self.o = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.o = 0
        elif not g(i, "DisPc"):
            if self.rise("Son", i.get("Son")): self.o = 1
            if self.rise("Soff", i.get("Soff")): self.o = 0
        return {"O": self.o}

@block("selection-switch-plus-minus", "selection-switch-plus")
class SelectionPM(Block):
    STATE = ("o",)
    def init(self): self.o = self.p["Vdef"]; self.t = 0.0; self.held = 0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.o = p["Vdef"]; return {"O": self.o}
        if i.get("V") is not None and self.rise("V", True) is not None: self.o = clamp(i["V"], p["Vmin"], p["Vmax"])
        if i.get("Val") is not None: self.o = i["Val"]
        if g(i, "DisPc"): return {"O": self.o}
        for key, d in (("+", 1), ("-", -1)):
            on = bool(g(i, key))
            if self.rise(key, on) : self.o += d * p["Sts"]; self.t = 0
            elif on:
                self.t += dt
                if self.t >= p["Rr"]: self.t = 0; self.o += d * p["Sts"]
            if self.bid == "selection-switch-plus":
                if self.o > p["Vmax"]: self.o = p["Vmin"]
            else: self.o = clamp(self.o, p["Vmin"], p["Vmax"])
        return {"O": self.o}

# ============================================================== PULSADORES
@block("push-switch")
class PushSwitch(Block):
    def init(self): self.t = 0.0; self.pon = self.poff = 0.0
    def step(self, i, dt):
        if g(i, "DisPc"): return {"O": 0, "Off": 0, "On": 0}
        r, f = self.edges("Tr", i.get("Tr")); d = self.p["Don"] or 0.02
        if r: self.pon = d; self.t = d
        if f: self.poff = d; self.t = d
        if g(i, "On"): self.t = d
        if g(i, "Off"): self.t = 0
        out = {"O": int(self.t > 0), "On": int(self.pon > 0), "Off": int(self.poff > 0)}
        self.t = max(0, self.t - dt); self.pon = max(0, self.pon - dt); self.poff = max(0, self.poff - dt)
        return out

@block("retractive-switch")
class RetractiveSwitch(Block):
    """rec: T sin documentar -> 'T' en params (s), por defecto 10."""
    STATE = ("q",)
    def init(self): self.q = 0; self.t = 0.0
    def step(self, i, dt):
        if g(i, "Dis"): return {"Q": self.q}
        T = self.p.get("T") or 10.0
        if g(i, "R"): self.q = 0; self.t = 0
        elif self.rise("Tr", i.get("Tr")): self.q = 1; self.t = 0
        elif self.q:
            self.t += dt
            if self.t >= T: self.q = 0
        return {"Q": self.q}

@block("stairwell-light-switch")
class Stairwell(Block):
    """doc: Tr enciende Don s; aviso: se apaga Dw s a Tw s del final. rec: re-disparo reinicia."""
    STATE = ("o",)
    def init(self): self.o = 0; self.t = 0.0
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.o = 0; self.t = 0; return {"O": 0}
        if g(i, "DisPc"): return {"O": self.o}
        if self.rise("Tr", i.get("Tr")) or self.rise("On", i.get("On")): self.o = 1; self.t = 0
        out = self.o
        if self.o:
            self.t += dt
            warn_start = p["Don"] - p["Tw"]
            if p["Tw"] > 0 and warn_start <= self.t < warn_start + p["Dw"]: out = 0
            if self.t >= p["Don"]: self.o = 0; out = 0
        return {"O": out}

@block("long-click")
class LongClick(Block):
    STATE = ()
    def init(self): self.t0 = 0.0; self.down = False; self.pulse = [0.0] * 4; self.v = 0
    def step(self, i, dt):
        p = self.p
        if g(i, "R"): self.pulse = [0.0] * 4; self.v = 0
        r, f = self.edges("Tr", i.get("Tr"))
        if r: self.down = True; self.t0 = 0.0
        if self.down: self.t0 += dt
        if f and self.down:
            self.down = False; k = min(int(self.t0 // p["TI"]), 3)
            self.pulse[k] = p["D"]; self.v = p[f"V{k+1}"]
        out = {f"O{k+1}": int(self.pulse[k] > 0) for k in range(4)}; out["V"] = self.v
        self.pulse = [max(0, x - dt) for x in self.pulse]
        return out

@block("double-click")
class DoubleClick(Block):
    """rec: M (intervalo máx. entre clics) 0.5 s y T (duración del pulso) 0.1 s por defecto."""
    def init(self): self.last = None; self.t = 0.0; self.q = 0.0
    def step(self, i, dt):
        M = self.p.get("M") or 0.5; T = self.p.get("T") or 0.1
        self.t += dt
        if g(i, "R"): self.q = 0
        if self.rise("Tr", i.get("Tr")):
            if self.last is not None and self.t - self.last <= M: self.q = T; self.last = None
            else: self.last = self.t
        if self.last is not None and self.t - self.last > M: self.last = None
        o = int(self.q > 0); self.q = max(0, self.q - dt); return {"Q": o}

@block("multifunction-switch")
class ComfortSwitch(Block):
    """doc: pulso corto enciende Don; otro pulso apaga antes; pulsación larga (≥Tlc) = permanente;
    aviso de apagado Tw/Dw como la luz de escalera."""
    STATE = ("o",)
    def init(self): self.o = 0; self.t = 0.0; self.perm = False; self.hold = 0.0; self.down = False
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.o = 0; self.perm = False; return {"O": 0}
        if g(i, "DisPc"): return {"O": self.o}
        r, f = self.edges("Tg", i.get("Tg"))
        if r: self.down = True; self.hold = 0
        if self.down: self.hold += dt
        if f and self.down:
            self.down = False
            if self.hold >= p["Tlc"]: self.o = 1; self.perm = True
            elif self.o: self.o = 0; self.perm = False
            else: self.o = 1; self.t = 0
        if self.rise("On", i.get("On")): self.o = 1; self.t = 0
        out = self.o
        if self.o and not self.perm:
            self.t += dt; ws = p["Don"] - p["Tw"]
            if p["Tw"] > 0 and ws <= self.t < ws + p["Dw"]: out = 0
            if self.t >= p["Don"]: self.o = 0; out = 0
        return {"O": out}

# ============================================================== DATOS
@block("shift-register")
class ShiftRegister(Block):
    STATE = ("reg",)
    def init(self): self.reg = [0] * int(self.p["Rb"])
    def step(self, i, dt):
        if self.rise("Tr", i.get("Tr")):
            d = int(bool(g(i, "D")))
            if g(i, "Dir"): self.reg = self.reg[1:] + [d]
            else: self.reg = [d] + self.reg[:-1]
        return {"O": self.reg[-1] if not g(i, "Dir") else self.reg[0]}
@block("binary-encoder")
class BinEnc(Block):
    def step(self, i, dt):
        return {"V": sum(1 << k for k in range(32) if g(i, f"Bit {k}", g(i, f"B{k}")))}
@block("binary-decoder")
class BinDec(Block):
    def step(self, i, dt):
        v = int(clamp(g(i, "V"), 0, 4294967295)); return {f"Bit {k}": (v >> k) & 1 for k in range(32)}
@block("memory-flags")
class MemoryFlag(Block):
    """doc: valor de memoria con Delay en ciclos. Un ciclo de retardo = salida con valor del ciclo anterior."""
    def init(self): self.buf = []
    def step(self, i, dt):
        d = int(self.p.get("Delay") or 0); self.buf.append(g(i, "I")); self.buf = self.buf[-(d + 1):]
        return {"Q": self.buf[0] if len(self.buf) > d else self.buf[0]}
@block("command-recognition")
class CommandRecognition(Block):
    """rec: subconjunto del patrón Loxone: texto literal entre 'i...i' avanza; 'v' extrae número."""
    STATE = ("lv",)
    def init(self): self.lv = 0
    def step(self, i, dt):
        t = i.get("T")
        if isinstance(t, str) and t:
            pat = self.cfg.get("pattern", "v"); pos = 0; val = None; k = 0
            while k < len(pat):
                c = pat[k]
                if c == "i":
                    e = pat.index("i", k + 1); lit = pat[k + 1:e]; j = t.find(lit, pos)
                    if j < 0: val = None; break
                    pos = j + len(lit); k = e + 1; continue
                if c == "v":
                    m = re.compile(r"-?\d+(?:[.,]\d+)?" if self.cfg.get("signed") else r"\d+(?:[.,]\d+)?").search(t, pos)
                    if m: val = float(m.group().replace(",", ".")); pos = m.end()
                k += 1
            if val is not None: self.lv = val
        return {"Lv": self.lv}

# ============================================================== ALEATORIO
@block("random-controller")
class RandomController(Block):
    def init(self): self.rng = random.Random(self.cfg.get("seed")); self.o = 0; self.t = 0.0; self.nxt = None
    def step(self, i, dt):
        if not g(i, "En"): self.o = 0; self.nxt = None; return {"Ran": 0}
        if self.nxt is None: self.nxt = self.rng.uniform(0, self.p["Soff"] if self.o else self.p["Son"]); self.t = 0
        self.t += dt
        if self.t >= self.nxt: self.o ^= 1; self.nxt = None
        return {"Ran": self.o}
@block("random-number-generator")
class RandomNumber(Block):
    STATE = ("v",)
    def init(self): self.rng = random.Random(self.cfg.get("seed")); self.v = 0
    def step(self, i, dt):
        if not g(i, "DisPc") and self.rise("C", i.get("C")): self.v = self.rng.uniform(self.p["Min"], self.p["Max"])
        return {"Ran": self.v}
