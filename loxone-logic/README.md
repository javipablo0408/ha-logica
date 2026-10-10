# Lógica

Bloques de lógica para Home Assistant, con editor visual y una app que se ordena sola por plantas y habitaciones (áreas de HA).

Modelo: los **bloques coordinan** y los **nodos de dispositivo** son los aparatos reales.
- **Controlador de luz** (bloque): decide encendido, color, brillo, temperatura y escenas. Su salida «Luz» se cablea a uno o varios nodos **Luz**.
- **Luz** (nodo): la lámpara real (Zigbee, Z-Wave, ESPHome…). Se adapta a lo que admita.
- **Mando** (nodo): pulsaciones 1, 2 y larga, que se cablean a las entradas de los bloques.

Actualizar: Ajustes → Complementos → Tienda → ⋮ → Buscar actualizaciones → Actualizar → Reiniciar → F5.
