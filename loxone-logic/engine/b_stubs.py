"""Bloques de hardware / integración / sistema / comunicaciones.
STUB=True: no hay algoritmo (es hardware o servicio externo); en HA se resuelven con la integración
correspondiente. Cada stub registra su actividad en ctx.events (el puente HA puede convertirla en
notify.*, persistent_notification, logbook, etc.) y mantiene las salidas del catálogo a 0."""
from runtime import Block, block, catalog, g

def _emit(b, kind, **data):
    ev = getattr(b.ctx, "events", None)
    if ev is None: ev = b.ctx.events = []
    ev.append({"block": b.iid, "type": b.bid, "kind": kind, **data})

class Stub(Block):
    STUB = True
    def _zero(self):
        spec = catalog().get(self.bid, {})
        return {o["abbr"]: 0 for o in spec.get("outputs", [])}
    def step(self, i, dt): return self._zero()

@block("status-monitor", "tracker", "lighting-groups", "virtual-status", "eib-shading", "eib-dimmer", "eib-push-button",
       "power-supply-backup-block", "wall-display-10-block", "ir-control", "virtual-inputs-outputs", "tesla-powerwall",
       "mqtt", "ocpp-server-connector", "husqvarna", "fronius", "gardena", "home-connect", "mielehome",
       "honeywell-security", "system-status", "statistics")
class Passthrough(Stub):
    """Sin lógica propia; los valores llegan/salen por la periferia (entidades HA)."""

@block("push-notification", "mail-generator", "call-generator")
class Notifier(Stub):
    """Flanco de subida en Tr (o en la entrada) -> evento de notificación."""
    def step(self, i, dt):
        trig = i.get("Tr", i.get("(entrada)", i.get("I")))
        if self.rise("tr", trig):
            _emit(self, "notify", params=dict(self.p), values={k: v for k, v in i.items() if k.startswith("V")})
        return self._zero()

@block("logger")
class Logger(Stub):
    """Registra cada entrada que cambia como evento de log."""
    def step(self, i, dt):
        for k, v in i.items():
            if self._prev.get("v" + k) != v: _emit(self, "log", port=k, value=v)
            self._prev["v" + k] = v
        return {}

@block("status")
class StatusBlock(Stub):
    """Txt/Val: entrada activa de menor índice (I1..I8) y su texto (config.texts)."""
    def step(self, i, dt):
        for n in range(1, 9):
            v = g(i, f"I{n}")
            if v:
                return {"Txt": (self.cfg.get("texts") or {}).get(str(n), f"I{n}"), "Val": v, "API": {"input": n}}
        return {"Txt": "", "Val": 0, "API": None}

@block("analogue-correction")
class AnalogueCorrection(Stub):
    """doc: escalado lineal 2 puntos (IV1->DV1, IV2->DV2). Se aplica en la capa de adaptación;
    como bloque, convierte la entrada I."""
    STUB = False
    DEFAULTS = {"IV1": 0, "DV1": 0, "IV2": 1, "DV2": 1}
    def step(self, i, dt):
        p = self.p; v = g(i, "I"); d = p["IV2"] - p["IV1"]
        return {"O": v if d == 0 else p["DV1"] + (v - p["IV1"]) * (p["DV2"] - p["DV1"]) / d}

@block("ping-function-block")
class Ping(Stub):
    """Online lo fija el puente HA (ping/binary_sensor); aquí se propaga la entrada 'Online' externa."""
    def step(self, i, dt):
        reset, lock = self.off(i.get("Off"), dt)
        return {"Online": 0 if lock else int(bool(i.get("Online", self._prev.get("on", 0))))}
