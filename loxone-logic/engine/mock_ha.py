"""HA simulado para probar el puente/editor sin Home Assistant. python3 mock_ha.py  (puerto 8766)"""
import asyncio, json, websockets
STATES = [{"entity_id": e, "state": s, "attributes": {"friendly_name": n}} for e, s, n in [
 ("binary_sensor.presencia_pasillo","off","Presencia pasillo"),("light.pasillo","off","Luz pasillo"),
 ("light.salon","on","Luz salón"),("cover.persiana_salon","open","Persiana salón"),
 ("sensor.temp_salon","21.5","Temperatura salón"),("switch.enchufe","off","Enchufe")]]
STATES[1]["attributes"].update({"supported_color_modes":["color_temp","xy"],"effect_list":["blink","breathe"],"brightness":254,"color_temp_kelvin":3000,"rgb_color":[255,200,120]})
async def h(ws):
    await ws.send(json.dumps({"type":"auth_required"})); await ws.recv(); await ws.send(json.dumps({"type":"auth_ok"}))
    last = 0
    async for raw in ws:
        m = json.loads(raw)
        if m["id"] <= last:   # como HA real: los ids deben crecer
            await ws.send(json.dumps({"id":m["id"],"type":"result","success":False,"error":{"code":"id_reuse","message":"Identifier values have to increase."}})); continue
        last = m["id"]
        if m["type"]=="get_states": await ws.send(json.dumps({"id":m["id"],"type":"result","success":True,"result":STATES}))
        elif m["type"]=="config/area_registry/list": await ws.send(json.dumps({"id":m["id"],"type":"result","success":True,"result":[{"area_id":"salon","name":"Salón"},{"area_id":"pasillo","name":"Pasillo"}]}))
        elif m["type"]=="config/device_registry/list": await ws.send(json.dumps({"id":m["id"],"type":"result","success":True,"result":[{"id":"d1","name":"Luz del pasillo (Shelly)","area_id":"pasillo"},{"id":"d2","name":"Sensor presencia","name_by_user":"Presencia pasillo","area_id":"pasillo"}]}))
        elif m["type"]=="config/entity_registry/list": await ws.send(json.dumps({"id":m["id"],"type":"result","success":True,"result":[
            {"entity_id":"light.salon","area_id":"salon"},{"entity_id":"cover.persiana_salon","area_id":"salon"},{"entity_id":"sensor.temp_salon","area_id":"salon"},
            {"entity_id":"light.pasillo","device_id":"d1"},{"entity_id":"binary_sensor.presencia_pasillo","device_id":"d2"}]}))
        elif m["type"]=="get_config": await ws.send(json.dumps({"id":m["id"],"type":"result","success":True,"result":{"latitude":40.2,"longitude":-3.7,"time_zone":"Europe/Madrid"}}))
        elif m["type"]=="subscribe_events":
            async def later():
                await asyncio.sleep(6)
                await ws.send(json.dumps({"type":"event","event":{"data":{"entity_id":"binary_sensor.presencia_pasillo","new_state":{"entity_id":"binary_sensor.presencia_pasillo","state":"on","attributes":{}}}}}))
            asyncio.create_task(later())
        elif m["type"]=="call_service": print("CALL", m["domain"], m["service"], m["service_data"], flush=True)
async def main():
    async with websockets.serve(h,"127.0.0.1",8766): await asyncio.Future()
asyncio.run(main())
