# PKHosting — Panel web para servidor Minecraft (NeoForge 1.21.1)

Panel ligero en Python (solo stdlib) estilo BisectHosting/Pterodactyl con tema
Liquid Glass negro + morado. Administra el servidor `PrankLindorf` desde
`http://127.0.0.1:8000`.

## Funciones

- Consola en vivo con envío de comandos (stdin directo → RCON → `/proc/<pid>/fd/0`).
- Alias de nivel panel en consola: `start`, `stop`, `restart`, `reload`, `kill`.
- Métricas CPU/RAM con colores por uso, gráficas con pico/promedio, jugadores vía ping.
- Gestor de archivos con CRUD: crear, subir (multipart 300 MB), descargar,
  renombrar, eliminar + editor para archivos ≤ 2 MB.
- IP pública (playit.gg) configurable desde el panel.
- RCON (`127.0.0.1:25575`, password en `~/.config/mc-panel-rcon`, nunca en el repo).

## Servicio

```bash
systemctl --user status mc-panel.service
systemctl --user restart mc-panel.service
journalctl --user -u mc-panel.service -f
```

El servicio usa `KillMode=process` para no matar al servidor al reiniciar el panel.
