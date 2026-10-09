import datetime as dt
from runtime import REGISTRY, Ctx
import b_basic, b_seq, b_comfort, b_energy, b_audio  # noqa

def mk(bid, params=None, cfg=None, ctx=None): return REGISTRY[bid](bid, params, cfg, ctx)
def run(b, seq, dt=1.0): return [b.step(i, dt) for i in seq]
def hold(i, n): return [i] * n

def test_meter_integrates_and_periods():
    ctx = Ctx(now=dt.datetime(2026, 10, 5, 23, 59, 0))
    m = mk("meter", ctx=ctx)
    for _ in range(3600):
        ctx.now += dt.timedelta(seconds=1); o = m.step({"Pf": 2.0}, 1.0)   # 2 kW durante 1 h cruzando medianoche
    assert abs(o["Mr"] - 2.0) < 0.01
    assert abs(o["Rld"] - 2 * 60 / 3600) < 0.01 and abs(o["Rd"] - (2 - 2 * 60 / 3600)) < 0.01

def test_meter_uses_reading_and_offset():
    m = mk("meter", {"Mro": 10}); o = m.step({"Mr": 5.0}, 1.0); assert o["Mr"] == 15.0

def test_meter_bidirectional_and_storage():
    m = mk("meter-bidirectional")
    run(m, hold({"Pf": 3.6}, 1000)); o = run(m, hold({"Pf": -3.6}, 500))[-1]
    assert abs(o["Mrc"] - 1.0) < 0.01 and abs(o["Mrd"] - 0.5) < 0.01
    s = mk("meter-storage"); o = run(s, hold({"Pf": -3.6}, 1000))[-1]; assert abs(o["Mrc"] - 1.0) < 0.01 and o["Mrd"] == 0

def test_pulse_meter():
    m = mk("pulse-meter", {"Np": 1000}); out = None
    for _ in range(2000): m.step({"P": 1}, 0.01); out = m.step({"P": 0}, 0.01)
    assert abs(out["Mr"] - 2.0) < 1e-9
    assert abs(mk("pulse-meter", {"Np": 1000}).step({"F": 1.0}, 1)["Pf"] - 3.6) < 1e-9

def test_pulse_meter_storage_sign():
    m = mk("pulse-meter-storage", {"Npd": 1000, "Npc": 1000})
    for _ in range(10): m.step({"Pc": 1}, .01); o = m.step({"Pc": 0}, .01)
    assert o["Mrc"] == 0.01 and o["Mrd"] == 0

def test_load_manager_sheds_and_restores():
    lm = mk("load-manager", {"MaxP": 10, "Hys": 1})
    base = {f"S{k}": 1 for k in range(1, 4)}
    o = run(lm, hold({**base, "Gpwr": 12}, 3))[-1]; assert o["L3"] == 0 and o["L1"] == 1
    o = run(lm, hold({**base, "Gpwr": 8}, 40))[-1]; assert o["L3"] == 1                       # restablece
    assert max(x["ApPeak"] for x in run(lm, hold({**base, "Gpwr": 15}, 5))) > 8

def test_energy_manager_surplus():
    em = mk("energy-manager-2", cfg={"loads": [{"kw": 2.0}, {"kw": 1.0}]})
    o = em.step({"Gpwr": -3.5}, 1); assert o["L1"] == 1 and o["L2"] == 1
    o = em.step({"Gpwr": 1.0}, 1); assert o["L2"] == 0 and o["L1"] == 1                     # apaga la de menor prioridad
    o = em.step({"Gpwr": -0.5, "Prio": 2}, 1); assert o["L2"] == 1                           # forzada

def test_flow_monitor():
    ctx = Ctx(now=dt.datetime(2026, 10, 5, 12, 0)); f = mk("energy-flow-monitor", ctx=ctx)
    o = run(f, hold({"Gpwr": -1.0, "Ppwr": 4.0, "Spwr": 0.0}, 3600))[-1]   # consume 3, exporta 1
    assert abs(o["Cpwr"] - 3.0) < 1e-9 and abs(o["Pd"] - 4.0) < 0.01 and abs(o["Ed"] - 1.0) < 0.01
    assert abs(o["Scd"] - 3.0) < 0.01 and abs(o["Co2d"] - 3 * 0.42) < 0.01 and abs(o["Yd"] - (1 * 0.2 + 3 * 0.2)) < 0.01

def test_audio_player_volume_and_events():
    a = mk("audio-player", {"Von": 10, "Vsts": 5}); a.step({"V+": 1}, .1); a.step({"V+": 0}, 1.0)
    o = a.step({"V+": 1}, .1); assert o["Volume"] == 15 and o["Play"] == 1
    a.step({"V+": 0}, 1.0); o = a.step({"Bell": 1}, .1); assert o["Volume"] == 40
    o = run(a, hold({"Bell": 0}, 6), dt=1.0)[-1]; assert o["Volume"] == 15

def test_audio_double_click_off_and_fav():
    a = mk("audio-player", {"Vsts": 5}); a._power_on(20)
    a.step({"V-": 1}, .1); a.step({"V-": 0}, .1); o = a.step({"V-": 1}, .1); assert o["Play"] == 0
    a2 = mk("audio-player"); a2._power_on(20); a2.step({"V+": 1}, .1); a2.step({"V+": 0}, .1); o = a2.step({"V+": 1}, .1)
    assert o["Fav"] == 2

def test_audio_presence_and_pause_toggle():
    a = mk("audio-player"); assert a.step({"P": 1}, .1)["Play"] == 1 and a.step({"P": 0}, .1)["Play"] == 0
    a.step({"Tg": 1}, .1); a.step({"Tg": 0}, .1); o = a.step({"Tg": 1}, .1); assert o["Play"] == 0

def test_music_server_zone_motion_auto_off():
    z = mk("music-server-zone", {"TH": 5}); o = z.step({"Mo": 1}, .1); assert o["Qa"] == 1
    run(z, hold({"Mo": 0}, 7), dt=1.0); assert z.step({"Mo": 0}, 1)["Qa"] == 0

def test_media_controller_modes():
    mc = mk("media-controller", cfg={"modes": {"1": {"on": {"O1": 1}, "off": {"O1": 0}}, "2": {"on": {"O2": 1}, "off": {"O2": 0}}}})
    o = mc.step({"M1": 1}, .1); assert o["M"] == 1 and o["O1"] == 1
    mc.step({"M1": 0}, .1); o = mc.step({"M2": 1}, .1); assert o["O1"] == 0 and o["O2"] == 1
    mc.step({"M2": 0}, .1); o = mc.step({"Poff": 1}, .1); assert o["P"] == 0 and o["O2"] == 0
