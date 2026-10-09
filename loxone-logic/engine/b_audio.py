"""Audio: máquinas de estado de zona. El adaptador HA traduce Play/Volume/Fav a media_player.*"""
from runtime import Block, block, g, clamp

class ZoneBase(Block):
    STATE = ("vol", "last_vol", "playing", "on", "fav")
    def zinit(self):
        self.on = False; self.playing = False; self.vol = 0.0; self.last_vol = self.p.get("Von", 10) if self.p.get("Von", 10) >= 0 else 10
        self.fav = 1; self.event = None; self.et = 0.0; self.clk = 0.0; self.vclicks = []; self.tts = ""
        self.pulse = {"2C": 0.0, "3C": 0.0}; self.pres = False
    def init(self): self.zinit()
    def _power_on(self, vol=None):
        self.on = True; self.playing = True
        von = self.p.get("Von", 10)
        self.vol = vol if vol is not None else (self.last_vol if von < 0 else von)
    def _power_off(self):
        if self.on: self.last_vol = self.vol
        self.on = False; self.playing = False; self.vol = 0.0
    def _vstep(self, up, i):
        n = self.clk; self.vclicks = [c for c in self.vclicks if n - c[0] <= self.p.get("Tdc", 0.35)]
        key = "up" if up else "dn"
        same = [c for c in self.vclicks if c[1] == key]
        if same:
            if up: self.fav += 1; self.fav = self.fav if self.fav <= self.cfg.get("n_fav", 10) else 1
            else: self._power_off()
            self.vclicks = []; return
        self.vclicks.append((n, key))
        if not self.on:
            if up: self._power_on()
            return
        self.vol = clamp(self.vol + (self.p.get("Vsts", 1) if up else -self.p.get("Vsts", 1)), 0, self.p.get("Vm", 100))
    def common(self, i, dt):
        p = self.p; self.clk += dt
        reset, lock = self.off(i.get("Off"), dt)
        if lock or reset:
            self._power_off(); self.event = None
        dis = bool(g(i, "DisPc"))
        if not lock and not dis:
            if self.rise("V+", i.get("V+")): self._vstep(True, i)
            if self.rise("V-", i.get("V-")): self._vstep(False, i)
            if i.get("V") is not None and self._prev.get("Vv") != i["V"]:
                v = clamp(i["V"], 0, p.get("Vm", 100))
                if not self.on: self._power_on(v)
                else: self.vol = v
            self._prev["Vv"] = i.get("V")
            if self.rise("Play", i.get("Play")): 
                if not self.on: self._power_on()
                self.playing = True
            if self.rise("Pause", i.get("Pause")): self.playing = False
            if self.rise("Tg", i.get("Tg")):
                if not self.on: self._power_on()
                else: self.playing = not self.playing
            if i.get("Fav") is not None and self._prev.get("Fv") != i["Fav"]: self.fav = int(i["Fav"]); 
            self._prev["Fv"] = i.get("Fav")
            if self.rise("Next", i.get("Next")) : self._prev["next"] = self.clk
            if self.rise("Prev", i.get("Prev")): self._prev["prev"] = self.clk
            if isinstance(i.get("TTS"), str) and i["TTS"] and self._prev.get("tts") != i["TTS"]:
                self.tts = i["TTS"]; self._event("tts", 5.0)
            self._prev["tts"] = i.get("TTS")
        pr = bool(g(i, "P")) and not g(i, "DisP")
        r, f = self.edges("P", pr)
        if r and not lock: self._power_on() if not self.on else setattr(self, "playing", True)
        if f and not lock: self._power_off()
        for k, name, secs in (("Alarm", "alarm", 30.0), ("FireAlarm", "fire", 60.0), ("Bell", "bell", 5.0), ("Buzzer", "buzzer", 60.0)):
            if self.rise(k, i.get(k)): self._event(name, secs)
        self._tick_event(dt)
        for k in self.pulse: self.pulse[k] = max(0, self.pulse[k] - dt)
        return lock
    def _event(self, name, secs):
        self.event = name; self.et = secs; self._pre = (self.on, self.vol, self.playing)
        floor = {"alarm": "Va", "fire": "Va", "bell": "Vbell", "buzzer": "Vbuzzer", "tts": "Vtts"}[name]
        self.on = True; self.playing = True; self.vol = max(self.vol, self.p.get(floor, 50))
    def _tick_event(self, dt):
        if self.event:
            self.et -= dt
            if self.et <= 0:
                on, vol, pl = self._pre; self.event = None
                self.on, self.vol, self.playing = on, vol, pl
@block("audio-player")
class AudioPlayer(ZoneBase):
    """Zona de audio. Evento (alarma, timbre, TTS...) sube el volumen a max(actual, mínimo del evento) y restaura al terminar."""
    def step(self, i, dt):
        self.common(i, dt)
        return {"Play": int(self.on and self.playing), "Volume": self.vol, "Stereo LR": int(self.on), "Stereo L": int(self.on), "Stereo R": int(self.on),
                "Fav": self.fav, "Event": self.event or "", "TTS": self.tts if self.event == "tts" else ""}
@block("audio-player-fixed-group")
class AudioPlayerFixedGroup(AudioPlayer): pass

@block("audio-central")
class AudioCentral(Block):
    """Fan-out de órdenes (TgZ, Zon, Zoff, V+, V-, V, Play, Pause, Stop, Next, Prev, Alarm...) y Na = zonas activas (entradas Z1..Z32)."""
    CMD = ("TgZ Zon Zoff V+ V- V S+ Play AIs Pause Stop Shuffle Repeat Sleep TTS Off Next Prev T5 DisPc DisP Alarm FireAlarm Bell Buzzer Rtd Cs").split()
    def step(self, i, dt):
        out = {k: i[k] for k in self.CMD if i.get(k) is not None}
        out["Na"] = sum(1 for k in range(1, 33) if g(i, f"Z{k}")); return out

@block("music-server-zone")
class MusicServerZone(ZoneBase):
    """Zona de Music Server. Añade Zon/Zoff/TgZ, Song±, Mute, Mo (movimiento → listas), MT apagado automático,
    Sleep (Ts s), Repeat/Shuffle."""
    def step(self, i, dt):
        p = self.p
        if not hasattr(self, "mute"): self.mute = False; self.sleep = None; self.mt = None; self.rep = 0; self.shuf = 0; self.song = 0
        self.clk += dt
        if self.rise("TgZ", i.get("TgZ")): self._power_off() if self.on else self._power_on()
        if self.rise("Zon", i.get("Zon")) and not self.on: self._power_on()
        if self.rise("Zoff", i.get("Zoff")): self._power_off()
        if self.rise("R", i.get("R")): self._power_off()
        if self.rise("Mute", i.get("Mute")): self.mute = not self.mute
        if self.rise("Song+", i.get("Song+")): self.song += 1
        if self.rise("Song-", i.get("Song-")): self.song = max(0, self.song - 1)
        if self.rise("Shuffle", i.get("Shuffle")): self.shuf ^= 1
        if i.get("Repeat") is not None: self.rep = int(i["Repeat"])
        if self.rise("Stop", i.get("Stop")): self.playing = False
        if i.get("AIv") is not None and self._prev.get("av") != i["AIv"]:
            if not self.on: self._power_on(i["AIv"])
            else: self.vol = clamp(i["AIv"], 0, p["Vm"])
        self._prev["av"] = i.get("AIv")
        mr, mf = self.edges("Mo", g(i, "Mo") and not g(i, "DisMo"))
        if mr and not self.on: self._power_on(); self.mt = None
        if mf and self.on: self.mt = p["TH"] or None
        if g(i, "Mo"): self.mt = None
        if self.mt is not None:
            self.mt -= dt
            if self.mt <= 0: self._power_off(); self.mt = None
        if self.rise("Sleep", i.get("Sleep")): self.sleep = p["Ts"]
        if self.sleep is not None:
            self.sleep -= dt
            if self.sleep <= 0: self._power_off(); self.sleep = None
        sub = {k: v for k, v in i.items() if k not in ("Zon", "Zoff", "TgZ", "R", "Mute", "AIv")}
        sub["V+"] = i.get("V+"); sub["V-"] = i.get("V-")
        self.clk -= dt         # common vuelve a sumar
        self.common(sub, dt)
        return {"Qa": int(self.on), "AQv": 0 if self.mute else self.vol, "AQs": self.fav, "AQr": self.sleep or 0,
                "Play": int(self.on and self.playing), "Song": self.song, "Shuffle": self.shuf, "Repeat": self.rep}

@block("media-controller")
class MediaController(Block):
    """Mando universal. cfg['modes'] = {"1": {"name": "TV", "on": {"O1": 1}, "off": {"O1": 0}}, ...}.
    M1-8/Mode activan el modo (ejecutan sus acciones 'on'); Poff/Ptg apagan (acciones 'off' del modo activo)."""
    STATE = ("mode", "power")
    def init(self): self.mode = 0; self.power = 0; self.vol = 0.0; self.ch = 0; self.outs = {}
    def _act(self, d):
        for k, v in d.items(): self.outs[k] = v
    def step(self, i, dt):
        p = self.p; reset, lock = self.off(i.get("Off"), dt)
        modes = self.cfg.get("modes", {})
        if lock or reset:
            if self.power and str(self.mode) in modes: self._act(modes[str(self.mode)].get("off", {}))
            self.mode = 0; self.power = 0
        elif not g(i, "DisPc"):
            sel = None
            for k in range(1, 9):
                if self.rise(f"M{k}", i.get(f"M{k}")): sel = k
            if i.get("Mode") is not None and self._prev.get("mv") != i["Mode"]: sel = int(i["Mode"])
            self._prev["mv"] = i.get("Mode")
            if sel and str(sel) in modes:
                if self.power and str(self.mode) in modes and self.mode != sel: self._act(modes[str(self.mode)].get("off", {}))
                self.mode = sel; self.power = 1; self._act(modes[str(sel)].get("on", {}))
            if self.rise("Pon", i.get("Pon")) and self.mode and not self.power: self.power = 1; self._act(modes[str(self.mode)].get("on", {}))
            if (self.rise("Poff", i.get("Poff")) or (self.rise("Ptg", i.get("Ptg")) and self.power)) and self.power:
                self.power = 0; self._act(modes.get(str(self.mode), {}).get("off", {}))
            elif self.rise("Ptg", i.get("Ptg")) and not self.power and self.mode:
                self.power = 1; self._act(modes[str(self.mode)].get("on", {}))
            if self.rise("V+", i.get("V+")): self.vol = clamp(self.vol + 5, 0, 100)
            if self.rise("V-", i.get("V-")): self.vol = clamp(self.vol - 5, 0, 100)
            if i.get("V") is not None: self.vol = clamp(i["V"], 0, 100)
            if self.rise("Ch+", i.get("Ch+")): self.ch += 1
            if self.rise("Ch-", i.get("Ch-")): self.ch = max(0, self.ch - 1)
            if i.get("Ch") is not None: self.ch = int(i["Ch"])
        out = dict(self.outs); out.update({"M": self.mode, "P": self.power, "Volume": self.vol, "Channel": self.ch}); return out
