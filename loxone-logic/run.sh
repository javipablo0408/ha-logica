#!/bin/sh
P=/data/proyecto.json
[ -f "$P" ] || cp /opt/loxone/engine/ejemplo_proyecto.json "$P"
LIVE=$(python3 -c "import json;print('--live' if json.load(open('/data/options.json')).get('arrancar_activo') else '')")
echo "Proyecto: $P  ${LIVE:-(simulación)}"
exec python3 -u /opt/loxone/engine/ha_bridge.py "$P" --port 8099 $LIVE
