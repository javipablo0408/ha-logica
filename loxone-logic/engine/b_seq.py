"""Secuencias, escenas, reglas, horarios, modos de operación, tiempos, texto y despertador."""
import ast, math, operator, re, datetime as _dt
from runtime import Block, block, g, clamp, sun_position

# ---------------------------------------------------------------- evaluador seguro de condiciones
_B = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
      ast.Mod: operator.mod}
_C = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt, ast.GtE: operator.ge,
      ast.Lt: operator.lt, ast.LtE: operator.le}
def safe_eval(expr, env):
    def w(n):
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.Name): return env.get(n.id, 0) or 0
        if isinstance(n, ast.BinOp): return _B[type(n.op)](w(n.left), w(n.right))
        if isinstance(n, ast.UnaryOp):
            return (not w(n.operand)) if isinstance(n.op, ast.Not) else -w(n.operand)
        if isinstance(n, ast.BoolOp):
            vs = [w(x) for x in n.values]; return all(vs) if isinstance(n.op, ast.And) else any(vs)
        if isinstance(n, ast.Compare):
            l = w(n.left)
            for op, r in zip(n.ops, n.comparators):
                rv = w(r)
                if not _C[type(op)](l, rv): return False
                l = rv
            return True
        raise ValueError("no permitido")
    return w(ast.parse(expr.strip(), mode="eval").body)

# ---------------------------------------------------------------- Stepper / Sequencer
@block("stepper")
class Stepper(Block):
    """rec: V avanza Sts por flanco de S en la dirección Dir; al pasar M vuelve a 0 (sube) / a M (baja)."""
    STATE = ("v",)
    def init(self): self.v = 0
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.v = 0
        elif self.rise("S", i.get("S")):
            p = self.p
            if p["Dir"]:
                self.v -= p["Sts"]
                if self.v < 0: self.v = p["M"]
            else:
                self.v += p["Sts"]
                if self.v > p["M"]: self.v = 0
        return {"V": self.v}

@block("sequencer")
class Sequencer(Block):
    """doc: Tr activa la siguiente salida; P elige una; R -> Dv (0 = todas off); DisPc bloquea Tr y P."""
    STATE = ("sel",)
    def init(self): self.sel = int(self.p["Dv"])
    def step(self, i, dt):
        p = self.p; mx = int(p["Max"])
        if g(i, "R"): self.sel = int(p["Dv"])
        elif not g(i, "DisPc"):
            if self.rise("Tr", i.get("Tr")): self.sel = 1 if self.sel >= mx else self.sel + 1
            if i.get("P") is not None and self._prev.get("Pv") != i["P"]:
                self.sel = int(clamp(i["P"], 0, mx))
            self._prev["Pv"] = i.get("P")
        out = {f"O{k}": int(self.sel == k) for k in range(1, 9)}; out["Sel"] = self.sel
        return out

# ---------------------------------------------------------------- Sequence Controller
@block("sequence-controller")
class SequenceController(Block):
    """rec: el lenguaje de Loxone no es público. Lenguaje propio por línea:
       AQn=<expr con AI1..AI8>, TQ=texto, WAIT s, GOTO n, IF <cond> GOTO n, END.
       cfg['sequences'] = {"1": [líneas], ...}. Intervalo Interval ms entre líneas."""
    def init(self):
        self.seq = None; self.line = 0; self.wait = 0.0; self.acc = 0.0; self.aq = {}; self.tq = ""
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset: self.seq = None
        sel = None
        for k in range(1, 9):
            if self.rise(f"S{k}", i.get(f"S{k}")): sel = k
        if i.get("S") is not None and self._prev.get("Sv") != i["S"]:
            if i["S"] > 0: sel = int(i["S"])
        self._prev["Sv"] = i.get("S")
        if sel: self.seq, self.line, self.wait, self.acc = str(sel), 0, 0.0, 0.0
        env = {f"AI{k}": g(i, f"AI{k}") for k in range(1, 9)}
        if self.seq:
            self.acc += dt
            iv = self.p.get("Interval", 500) / 1000
            while self.seq and self.acc >= iv:
                if self.wait > 0:
                    self.wait -= iv; self.acc -= iv; continue
                lines = self.cfg.get("sequences", {}).get(self.seq, [])
                if self.line >= len(lines): self.seq = None; break
                self.acc -= iv; self._exec(lines[self.line].strip(), env)
        out = dict(self.aq); out.update({"S": int(self.seq or 0), "L": self.line, "TQ": self.tq})
        return out
    def _exec(self, ln, env):
        self.line += 1
        if not ln or ln.startswith("#"): return
        up = ln.upper()
        m = re.fullmatch(r"AQ(\d)\s*=\s*(.+)", ln, re.I)
        if m: self.aq[f"AQ{m.group(1)}"] = safe_eval(m.group(2), env); return
        if up.startswith("TQ"): self.tq = ln.split("=", 1)[1].strip().strip('"'); return
        m = re.fullmatch(r"WAIT\s+([\d.]+)", ln, re.I)
        if m: self.wait = float(m.group(1)); return
        m = re.fullmatch(r"IF\s+(.+?)\s+GOTO\s+(\d+)", ln, re.I)
        if m:
            if safe_eval(m.group(1), env): self.line = int(m.group(2)) - 1
            return
        m = re.fullmatch(r"GOTO\s+(\d+)", ln, re.I)
        if m: self.line = int(m.group(1)) - 1; return
        if up == "END": self.seq = None

# ---------------------------------------------------------------- Scene / Automatic Rule
@block("scene")
class Scene(Block):
    """cfg['actions'] = [{"port": "A1", "value": 50}, ...]; al activar (Act rising) las salidas
    adoptan esos valores y se mantienen hasta la siguiente escena. Cablear A1..An a los bloques destino."""
    STATE = ("vals",)
    def init(self): self.vals = {}
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if not lock and self.rise("Act", i.get("Act")):
            for a in self.cfg.get("actions", []): self.vals[a["port"]] = a["value"]
        return dict(self.vals)

@block("automatic-rule")
class AutomaticRule(Block):
    """rec: cfg['rules'] = [{"if": "I1 > 5 and I2", "then": {"Q1": 1}, "else": {"Q1": 0}}] con I1..I8."""
    def step(self, i, dt):
        env = {f"I{k}": g(i, f"I{k}") for k in range(1, 9)}; env.update({k: v for k, v in i.items() if v is not None})
        out = {}
        for r in self.cfg.get("rules", []):
            try: ok = bool(safe_eval(r["if"], env))
            except Exception: continue
            out.update(r.get("then", {}) if ok else r.get("else", {}))
        return out

# ---------------------------------------------------------------- Operating modes / times
@block("operating-times-periphery")
class OperatingTimes(Block):
    """cfg['entries'] = [{"mode":"vacaciones","from":"2026-12-24","to":"2027-01-06"}].
       Actualiza ctx.modes; salida 'Active' = nº de modos activos."""
    def step(self, i, dt):
        today = self.ctx.now.date().isoformat(); active = set()
        for e in self.cfg.get("entries", []):
            if e["from"] <= today <= e["to"]: active.add(e["mode"])
        wd = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"][self.ctx.now.weekday()]
        active.add(wd)
        for m in {e["mode"] for e in self.cfg.get("entries", [])} | set(self.cfg.get("weekdays", [])):
            (self.ctx.modes.add if m in active else self.ctx.modes.discard)(m)
        for w in ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]:
            (self.ctx.modes.add if w == wd else self.ctx.modes.discard)(w)
        return {"Active": len(active)}

@block("operating-modes")
class OperatingModes(Block):
    """cfg['mode']: nombre. Entrada I fuerza el modo; salida Q = modo activo (por entrada o por tiempos)."""
    def step(self, i, dt):
        m = self.cfg.get("mode", "modo")
        if i.get("I") is not None:
            (self.ctx.modes.add if i["I"] else self.ctx.modes.discard)(m)
        return {"Q": int(m in self.ctx.modes)}

@block("times")
class Times(Block):
    def init(self): self.last = None; self.start = True
    def step(self, i, dt):
        n = self.ctx.now; el, az = self.ctx.sun
        l = self.last or n
        out = {"Day": n.day, "Hour": n.hour, "Minutes": n.minute, "Seconds": n.second, "Month": n.month, "Year": n.year,
               "MinutesPastMidnight": n.hour * 60 + n.minute, "SunElevation": el, "SunDirection": az,
               "Daylight": int(el > -0.833), "Daylight30": int(el > 0.0), "Night": int(el < -6),
               "UnixTimestamp": n.timestamp(), "DaysSince2009": (n.date() - _dt.date(2009, 1, 1)).days,
               "StartPulse": int(self.start),
               "PulseSecondChange": int(n.second != l.second), "PulseMinuteChange": int(n.minute != l.minute),
               "PulseHourChange": int(n.hour != l.hour), "PulseDayChange": int(n.day != l.day),
               "PulseMonthChange": int(n.month != l.month), "PulseYearChange": int(n.year != l.year)}
        # amanecer/atardecer como cruces del horizonte entre ciclos
        le = self.last_el if hasattr(self, "last_el") else el
        out["PulseSunrise"] = int(le <= -0.833 < el); out["PulseSunset"] = int(le > -0.833 >= el)
        out["PulseDawn"] = int(le <= -6 < el); out["PulseDusk"] = int(le > -6 >= el)
        self.last_el = el; self.last = n; self.start = False
        return out

# ---------------------------------------------------------------- Schedule
def _hm(s): h, m = s.split(":"); return int(h) * 60 + int(m)

@block("schedule")
class Schedule(Block):
    """cfg['entries'] = [{"days":[0..6] (0=lunes), "start":"06:00", "end":"22:00", "value":1, "mode":None}]
       cfg['require_activation']: la salida solo se activa con pulso en Act dentro de la ventana (máx. Don s)."""
    STATE = ("o",)
    def init(self): self.o = 0; self.active_req = False; self.t = 0.0; self.last_on = 0
    def _inside(self):
        n = self.ctx.now; m = n.hour * 60 + n.minute; best = None
        for e in self.cfg.get("entries", []):
            if e.get("mode") and e["mode"] not in self.ctx.modes: continue
            if n.weekday() not in e.get("days", range(7)): continue
            s, f = _hm(e["start"]), _hm(e["end"])
            ok = (s <= m < f) if s <= f else (m >= s or m < f)
            if ok: best = e.get("value", 1)
        return best
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        v = None if lock else self._inside()
        if self.p.get("Am") == 1: v = self.p.get("Mm", 0) or None
        if self.cfg.get("require_activation"):
            if self.rise("Act", i.get("Act")) and v is not None: self.active_req = True; self.t = 0
            if v is None: self.active_req = False
            if self.active_req:
                self.t += dt
                if self.p["Don"] > 0 and self.t >= self.p["Don"]: self.active_req = False
            else: v = None
        new = v if v is not None else 0
        out = {"O": new, "On": int(new and not self.o), "Off": int(self.o and not new),
               "Om": sorted(self.ctx.modes)[0] if self.ctx.modes else ""}
        self.o = new
        return out

# ---------------------------------------------------------------- Text generator
@block("text-generator")
class TextGenerator(Block):
    def init(self): self.txt = ""; self.pending = None; self.t = 0.0
    def step(self, i, dt):
        r, f = self.edges("Tr", i.get("Tr"))
        env = {f"v{k}": i.get(f"V{k}") for k in range(1, 9)}
        if r: self.pending = self.p.get("Td", 0) / 1000; self.t = 0
        if f: self.txt = ""; self.pending = None
        if self.pending is not None:
            self.t += dt
            if self.t >= self.pending:
                self.pending = None
                self.txt = re.sub(r"<(v\d)>", lambda m: str(env.get(m.group(1), "")), self.cfg.get("template", ""))
        return {"Txt": self.txt, "Txt1": self.txt}

# ---------------------------------------------------------------- Alarm clock
@block("alarm-clock")
class AlarmClock(Block):
    """rec: Set fija la hora (min desde medianoche); Tg/S activa/desactiva; al llegar la hora suena
    Buzzer hasta Ca (confirmar) o MaxA min; Sd = snooze en s (DisA lo impide)."""
    STATE = ("enabled", "time")
    def init(self):
        self.enabled = bool(self.cfg.get("enabled", False)); self.time = self.cfg.get("time", 420)
        self.ring = False; self.t = 0.0; self.snooze = None; self.fired = None
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        if i.get("Set") is not None: self.time = int(i["Set"])
        if self.rise("Tg", i.get("Tg")): self.enabled = not self.enabled
        if i.get("S") is not None and self.rise("Sa", i.get("S")): self.snooze = self.p["Sd"] if self.ring else None; self.ring = False if self.snooze else self.ring
        if self.rise("Ca", i.get("Ca")): self.ring = False; self.snooze = None
        n = self.ctx.now; m = n.hour * 60 + n.minute; start = False
        if self.enabled and not lock and m == self.time and self.fired != (n.date(), m):
            self.fired = (n.date(), m); self.ring = True; self.t = 0; start = True
        if self.snooze is not None:
            self.snooze -= dt
            if self.snooze <= 0: self.snooze = None; self.ring = True; self.t = 0
        if self.ring:
            self.t += dt
            if self.t >= self.p["MaxA"] * 60: self.ring = False
        return {"A": int(self.enabled), "Buzzer": int(self.ring), "Aon": int(start), "Tna": self.time}
