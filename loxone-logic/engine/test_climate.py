import climate as c

def test_curva_ejemplos_itc():           # ejemplos documentados (ext 0 °C, S .5, N 0)
    h = c.HeatingCurve(S=0.5, O=0, minFt=5, maxFt=70)
    assert abs(h.step({"Tt":20,"Ct":0})["Ft"] - 30.9) < 0.5
    assert abs(h.step({"Tt":22,"Ct":0})["Ft"] - 33.8) < 0.5

def test_curva_limites_iv():
    h = c.HeatingCurve(S=2.5, maxFt=65)
    assert h.step({"Tt":22,"Ct":-15})["Iv"] == 1

def test_g_frio_ejemplo():                # G=2, dev 1.5 sobre 20 → 17.0
    f = c.FlowTemperatureController(G=2)
    r = f.step({"rooms":[{"tt":20,"tc":21.5,"demand":50,"area":20}],"to":30,"heating":False})
    assert r["AQt"] == 20 and r["AQf"] >= f.Min

def test_aqr_aql():
    f = c.FlowTemperatureController()
    r = f.step({"rooms":[{"tt":21,"tc":20,"demand":60,"area":10},{"tt":22,"tc":22,"demand":0,"area":30}],"to":0})
    assert r["AQr"] == 10 and abs(r["AQl"] - 15) < 1e-9

def test_qp_umbral():
    f = c.FlowTemperatureController(Str=35)
    assert f.step({"rooms":[{"tt":21,"tc":20,"demand":40,"area":1}],"to":0})["Qp"] == 1
    assert f.step({"rooms":[{"tt":21,"tc":20,"demand":30,"area":1}],"to":0})["Qp"] == 0

def test_consigna_por_defecto_y_eco():
    r = c.RoomController()
    assert r.targets("comfort") == 23.5
    assert r.targets("eco_min") == 19.5 and r.targets("eco_max") == 27.5

def test_aprendizaje_mediana_y_defecto():
    r = c.RoomController()
    assert r.rate(True) == 600 and r.rate(False) == 120
    r.heat_rate = [100, 300, 200]
    assert r.rate(True) == 200

def test_pwm_intervalo():
    r = c.RoomController()
    assert r.pwm_interval(1) == 10 and r.pwm_interval(0.1) == 60

def test_histeresis_shd():
    r = c.RoomController()
    assert r.shading(26, 0, True, 26) == 1
    assert r.shading(25.7, 1, True, 26) == 1 and r.shading(25.5, 1, True, 26) == 0

def test_ventana_corta_demanda():
    r = c.RoomController()
    assert r.step({"tc":18,"tt":21,"window":1}, 60)["demand"] == 0

def test_climate_modos_y_limites():
    k = c.ClimateController(mode=1)
    assert k.step({"demand_heat":50,"demand_cool":0,"to":5}, 1)["H"] == 1
    assert k.step({"demand_heat":50,"demand_cool":0,"to":20}, 1)["H"] == 0   # exterior > ϑLimH
    k = c.ClimateController(mode=3)
    assert k.step({"demand_heat":90,"demand_cool":0,"to":5}, 1) == {"H":0,"C":0}
