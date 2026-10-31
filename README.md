# PKHosting — Panel web para tus servidores Minecraft Java

Panel ligero (**solo Python estándar, sin dependencias**) con tema Liquid Glass
negro + morado. Administra **uno o varios servidores**
(Vanilla, Forge, Fabric, NeoForge) desde el navegador: consola, jugadores,
TPS, archivos, mods, backups, tareas programadas y avisos a Discord.

## Funciones

- **Multi-servidor & Dashboard de Flota:** añade, edita y elimina servidores desde Configuración,
  vista panorámica de toda tu flota con conmutación en 1-click.
- **Acceso con contraseña** (hash PBKDF2 + sesiones, se crea desde
  Configuración), **2FA TOTP** opcional, límite de intentos por IP y **HTTPS** opcional.
  Botón **Cerrar sesión** en la barra lateral.
- **Inicio** con el estado del servidor, jugadores en línea y accesos rápidos; pestañas propias de
  **Consola**, **Jugadores** y **Mods**.
- **Temas** claro/oscuro con 5 colores de acento.
- **Paleta de Comandos Global (`Ctrl+K` / `Cmd+K`):** buscador rápido flotante
  para navegar a cualquier sección, conmutar instancias o ejecutar acciones con atajos de teclado.
- **Consola Pro con Temas & Pantalla Completa:** autocompletado, filtros INFO/WARN/ERROR, búsqueda,
  temas visuales (Cyber Morado, Matrix, Monokai Pro, Tokyo Night, Dracula) y modo pantalla completa.
- **Jugadores & Fichas con Avatares:** renderizado de skins 2D/3D, horas jugadas reales desde estadísticas,
  muertes, asesinatos, km recorridos, coordenadas en vivo y acciones rápidas (curar, alimentar, gamemode, teletransporte).
- **Gestor de Mundos & MCA Region Cleaner:** cálculo de peso por dimensión (Overworld, Nether, End),
  calculadora de coordenadas a archivo `.mca`, borrado de regiones específicas (limpia chunks, POIs y entidades)
  y reinicio completo de dimensiones secundarias (Nether / End).
- **Buscador de Mods Modrinth:** explora e instala mods para NeoForge 1.21.1 en 1-click
  directamente en el servidor sin descargar manualmente.
- **Rendimiento Spark & Monitor de Red:** diagnósticos de salud del servidor (CPU, GC, pools de memoria),
  grabación de muestreo con subida automática a `spark.lucko.me`, medición de latencia TCP (Minecraft, RCON y túnel playit.gg)
  y alerta de rendimiento en cabecera si el TPS cae o la RAM satura.
- **Actualizaciones de mods** (Modrinth, con copia `.prev` para revertir; solo con el servidor detenido),
  **poda de chunks por tiempo de uso**, restauración de chunks desde `.bak` y exportación de regiones.
- **Diagnóstico:** crash-reports, guarda anti-bucle de reinicios, argumentos JVM, `server.properties` y visor de logs.
- **Editor de Archivos Mejorado:** contador de líneas y caracteres, tamaño de tipografía ajustable y tabulación con tecla Tab.
- **Centro de Notificaciones:** registro de actividad en tiempo real con indicador luminoso en cabecera.
- Alias de nivel panel: `start`, `stop`, `restart`, `reload`, `kill`.
- Jugadores: conteo real por ping, tiempo conectado, OP/DeOP/Kick/Ban por fila,
  moderación (OPs, baneados, whitelist on/off) e historial de conexiones.
- **TPS/MSPT** en vivo (vía `tick query`) con colores por estado.
- Métricas CPU/RAM con colores por uso, gráficas en vivo + **historial 24 h**,
  disco usado y red del host.
- Gestor de archivos CRUD + **subir con descompresión .zip**, editor ≤ 2 MB,
  **gestor de mods** (activar/desactivar/eliminar, requiere reinicio).
- **Backups** con retención (7 días + mensual eterno), pre-backup al restaurar,
  descarga, barra de progreso y config editable desde la UI.
- **Tareas programadas** (comandos, anuncios, guardado, reinicio opt-in).
- **Discord:** avisos de encendido/apagado, TPS bajo, backup fallido, joins/leaves.
- IP pública (playit.gg) configurable desde el panel. PWA instalable.

## Requisitos

- Linux con **systemd** (sesión de usuario).
- **Python 3.12+** (`python3 --version`; los backups usan `tarfile` con `filter="data"`).
- **Java** acorde a tu versión de Minecraft (`java -version`).
- Un servidor Minecraft funcional con script de arranque (ej. `start.sh`).
- Opcional: `openssl` (HTTPS autofirmado).

## Instalación rápida

```bash
git clone https://github.com/ItsYusei99/PKHosting.git ~/PKHosting
cd ~/PKHosting
./install.sh
```

El instalador pregunta lo esencial y genera el resto:

- `~/.config/pkhosting/config.json` — configuración.
- Contraseña del panel (la muestra **una sola vez**, se guarda en hash).
- `~/.config/pkhosting/rcon-password` — password RCON (600).
- Activa RCON en tu `server.properties` (con backup `.bak`).
- HTTPS autofirmado opcional.
- Crea y enciende `pkhosting.service` (`KillMode=process`: el MC sobrevive
  a reinicios del panel) y el timer de backups si los activas.

Abre **http://127.0.0.1:8000** (o tu puerto) e inicia sesión.

No interactivo (variables `PK_*`):

```bash
./install.sh --non-interactive
PK_SERVER_DIR=~/mi-server PK_MC_PORT=25565 PK_PANEL_PASSWORD="cambia-esto" ./install.sh --non-interactive --force
```

## Estructura esperada del servidor

```
~/minecraft-server/
├── start.sh              # ej: java -Xmx4G -jar server.jar nogui
├── server.properties
├── logs/latest.log
├── mods/                 # opcional
└── world/                # world_name configurable
```

## RCON (obligatorio para consola y apagado elegante)

```properties
enable-rcon=true
rcon.password=TU_PASSWORD_DE_~/.config/pkhosting/rcon-password
rcon.port=25575
```

> ⚠️ Reinicia el Minecraft después de tocar `server.properties`.

## `config.json`

```json
{
  "server_name": "MiServidor",
  "server_subtitle": "NeoForge 1.21.1 · Java 21",
  "server_dir": "~/minecraft-server",
  "start_cmd": ["bash", "start.sh"],
  "world_name": "world",
  "panel_port": 8000,
  "mc_port": 25565,
  "rcon_port": 25575,
  "max_mem_gb": 4.0,
  "bind": "127.0.0.1",
  "backup_enabled": false,
  "backup_dir": "~/mc-backups",
  "backup_time": "04:00",
  "retention_days": 7,
  "keep_monthly": true
}
```

Tras editarlo: `systemctl --user restart pkhosting.service`.

### Varios servidores

```json
{
  "panel_port": 8000,
  "servers": [
    {"id": "survival", "server_name": "Survival", "server_dir": "~/srv-survival",
     "mc_port": 25565, "rcon_port": 25575, "max_mem_gb": 4.0, ...},
    {"id": "creativo", "server_name": "Creativo", "server_dir": "~/srv-creativo",
     "mc_port": 25566, "rcon_port": 25576, "max_mem_gb": 4.0, ...}
  ]
}
```

Cada servidor necesita su `server.properties` con su RCON (guarda cada password
en `~/.config/pkhosting/servers/<id>/rcon-password`). Sin bloque `servers`,
funciona como antes con un solo servidor.

## Backups

`pkhosting-AAAAMMDD-HHMMSS.tar.gz` (mundo + `server.properties` + bans/ops +
`user_jvm_args.txt`) congelando el guardado; si el servidor está encendido y no se puede congelar, el
backup se aborta. Se escribe como `.partial` y solo se publica tras verificarlo. Retención: borra lo de
más de `retention_days`, excepto el **último de cada mes** (12 al año), y siempre conserva las 3 copias
más recientes. Un lock evita que el panel y el timer corran a la vez.

Al **restaurar**, el mundo actual se aparta antes de extraer (no se mezclan chunks nuevos y viejos) y se
hace un backup previo. Todo editable en la pestaña Backups. Timer: `pkhosting-backup.timer` (respeta
`backup_enabled` de cada servidor). CLI: `backup.py --run [--force] / --prune / --list / --restore ARCHIVO`.

## Tareas programadas

Pestaña **Tareas**: comandos, anuncios (`say`), guardado y **reinicio**.
El reinicio automático viene **desactivado**: solo corre si creas y activas una
tarea de ese tipo (puedes poner aviso previo en minutos).

## Discord

Configuración → pega el webhook, marca eventos (online/offline, TPS bajo,
backup fallido, joins/leaves) y usa Probar.

## Exponerlo a internet

1. Túnel TCP con [playit.gg](https://playit.gg) a `127.0.0.1:<mc_port>`.
2. Pega la IP en Configuración → IP pública.
3. Para acceso remoto al panel: activa HTTPS en la instalación o usa
   `ssh -L 8000:localhost:8000 tu-servidor`.

## Uso diario

| Acción | Dónde |
|---|---|
| Encender / apagar / reiniciar | Botones de energía o alias en consola |
| Moderar jugadores | Jugadores |
| Ver TPS, 24 h, disco, red | Métricas |
| Archivos y mods | Archivos (descomprime .zip al subir) · Mods |
| Backup manual / restaurar | Backups |
| Cambiar contraseña | Configuración |

```bash
systemctl --user status pkhosting.service
systemctl --user restart pkhosting.service
journalctl --user -u pkhosting.service -f
```

## Problemas comunes

| Síntoma | Solución |
|---|---|
| `puerto ocupado` al iniciar | Otro proceso usa `mc_port`. |
| Comandos sin respuesta | RCON mal configurado (ver sección RCON). |
| `RCON auth fallida` | Password de `server.properties` ≠ archivo `rcon-password`. |
| Olvidé la contraseña del panel | `python3 ~/PKHosting/panel.py --reset-password` (también desactiva el 2FA). |
| La RAM supera el `-Xmx` | Normal: `-Xmx` limita solo el heap; el panel mide RSS total. |
| Contador de jugadores en 0 | El ping necesita `enable-status=true` (va por defecto). |

## Desinstalación

```bash
systemctl --user disable --now pkhosting.service pkhosting-backup.timer
rm ~/.config/systemd/user/pkhosting*.service ~/.config/systemd/user/pkhosting-backup.timer
rm -rf ~/PKHosting ~/.config/pkhosting
```

## Estructura del repo

```
panel.py                   # punto de entrada: login, servidor HTTP y arranque
pkhosting/                 # núcleo, dividido por responsabilidad
  core.py                  #   config, autenticación, clase Server, RCON, procesos
  ops.py                   #   métricas, jugadores, arranque/parada, backups, Discord, tareas
  content.py               #   consola, plantillas estáticas, mods y Modrinth
  world.py                 #   NBT/MCA, mapa de terreno, poda y restauración de chunks
  tools.py                 #   fichas de jugador, ping, Spark, flota, subida de archivos
diagnostics.py             # crash-reports, JVM, server.properties y logs
backup.py                  # motor de backups (también CLI)
static/                    # interfaz: index.html, app.css y scripts por sección (*.js)
tests/                     # pruebas unitarias y de humo HTTP
install.sh                 # instalador interactivo / no interactivo
pkhosting.service          # plantilla systemd --user
pkhosting-backup.service   # unidad oneshot de backup
pkhosting-backup.timer     # plantilla del timer diario
config.example.json        # ejemplo de configuración
docs/architecture/         # mapa interactivo del sistema (pkhosting.html)
```

## Desarrollo

```bash
python3 -m unittest discover -s tests -v   # unitarias + humo HTTP (arranca el panel con un HOME temporal)
ruff check .                               # lint (reglas en ruff.toml)
node --check static/*.js                   # sintaxis del frontend
```

El CI (`.github/workflows/ci.yml`) ejecuta lo mismo con Python 3.12 y 3.13 y revisa `install.sh` con shellcheck.
