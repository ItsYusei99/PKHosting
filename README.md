# PKHosting — Panel web para tu servidor Minecraft Java

Panel ligero (**solo Python estándar, sin dependencias**) estilo BisectHosting con
tema Liquid Glass negro + morado. Enciende, apaga, manda comandos y revisa
métricas y archivos de tu servidor desde el navegador.

Funciona con **Vanilla, Forge, Fabric y NeoForge** (cualquier servidor Java que
se arranque con un script y guarde logs en `logs/`).

## Funciones

- Consola en vivo con envío de comandos (stdin → **RCON** → `/proc/<pid>/fd/0`).
- Alias de nivel panel en la consola: `start`, `stop`, `restart`, `reload`, `kill`.
- Métricas CPU/RAM que cambian de color según uso, gráficas con pico/promedio.
- Gestor de archivos: crear, **subir** (multipart, 300 MB), descargar, renombrar,
  eliminar y **editar** archivos de texto (`.json`, `.properties`, `.toml`…).
- IP pública (playit.gg) configurable desde el panel.
- Detección del proceso Java por carpeta (sobrevive a reinicios del panel).

## Requisitos

- Linux con **systemd** (sesión de usuario).
- **Python 3.8+** (`python3 --version`).
- **Java** acorde a tu versión de Minecraft (`java -version`).
- Un servidor Minecraft funcional con un script de arranque (ej. `start.sh`).

## Instalación rápida

```bash
git clone https://github.com/ItsYusei99/PKHosting.git ~/PKHosting
cd ~/PKHosting
./install.sh
```

El instalador te pregunta: nombre del servidor, carpeta del servidor, comando de
arranque, puertos (panel/Minecraft/RCON), RAM máxima (solo visual) y genera
todo lo demás:

- `~/.config/pkhosting/config.json` — configuración del panel.
- `~/.config/pkhosting/rcon-password` — password RCON (permisos 600).
- Activa RCON en tu `server.properties` (con backup `.bak`).
- Crea y enciende el servicio `pkhosting.service` (usuario, con `KillMode=process`
  para no matar al Minecraft al reiniciar el panel).

Al terminar abre **http://127.0.0.1:8000** (o el puerto que elegiste).

Instalación no interactiva (valores por defecto o variables `PK_*`):

```bash
./install.sh --non-interactive
PK_SERVER_DIR=~/mi-server PK_MC_PORT=25565 ./install.sh --non-interactive --force
```

## Cómo configurar tu servidor

### 1. Estructura esperada

```
~/minecraft-server/
├── start.sh              # tu script de arranque (ej: java -Xmx4G -jar server.jar nogui)
├── server.properties
├── logs/
│   └── latest.log
└── world/                # nombre configurable (world_name)
```

El panel lanza `start_cmd` con esa carpeta como directorio de trabajo y lee
`logs/latest.log` (+ `logs/panel-child.log` para lo que él mismo arranca).

### 2. RCON (obligatorio para consola y apagado elegante)

El instalador lo configura solo, pero manualmente son 3 líneas en
`server.properties` (debe coincidir con `config.json`):

```properties
enable-rcon=true
rcon.password=TU_PASSWORD_DE_~/.config/pkhosting/rcon-password
rcon.port=25575
```

> ⚠️ Reinicia el servidor Minecraft después de tocar `server.properties`.

### 3. `config.json`

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
  "max_mem_gb": 4.0
}
```

Tras editarlo: `systemctl --user restart pkhosting.service`.
Hay un ejemplo comentado en `config.example.json`.

### 4. Backups automáticos (opcional)

El instalador pregunta si los quieres, dónde y a qué hora. Guarda
`pkhosting-AAAAMMDD-HHMMSS.tar.gz` (mundo + `server.properties` + jsons de
bans/ops + `user_jvm_args.txt`) congelando el guardado (`save-off`/`save-all`
y tolerando archivos temporales del mundo vivo).

- **Retención:** borra lo de más de 7 días, excepto el **último de cada mes**,
  que se guarda para siempre (12 al año). `retention_days` y `keep_monthly`
  en `config.json`.
- **Manual:** pestaña **Backups** → *Backup ahora*; cada fila permite
  **Restaurar** (detiene el server, hace pre-backup de seguridad y rearranca)
  o **Eliminar**. Los mensuales llevan etiqueta MENSUAL.
- El timer es `pkhosting-backup.timer` (`systemctl --user status pkhosting-backup.timer`).
  Sin systemd timer también sirve: `backup.py --run/--prune/--list`.

### 5. Exponerlo a internet (opcional, playit.gg)

1. Instala el agente de [playit.gg](https://playit.gg) y crea un túnel TCP
   que apunte a `127.0.0.1:<tu-mc_port>`.
2. En el panel ve a **Configuración → IP pública** y pega la dirección
   (ej. `algo.playit.gg:12345`). El chip de la barra lateral la mostrará
   con botón para copiar.

## Uso diario

| Acción | Dónde |
|---|---|
| Encender / apagar / reiniciar | Botones INICIAR, REINICIAR, RELOAD, DETENER, FORZAR APAGADO |
| Comandos MC + alias (`start`, `reload`…) | Consola (Enter para enviar, ↑/↓ historial) |
| Ver CPU/RAM/jugadores | Tarjetas + pestaña Métricas |
| Subir mods, editar configs | Pestaña Archivos (flechas del navegador funcionan) |
| Cambiar IP pública | Pestaña Configuración |

```bash
systemctl --user status pkhosting.service
systemctl --user restart pkhosting.service
journalctl --user -u pkhosting.service -f
```

## Problemas comunes

| Síntoma | Causa y solución |
|---|---|
| `puerto ocupado` al iniciar | Otro proceso usa `mc_port`. Detén la otra instancia. |
| Comandos sin respuesta | RCON no activo o password desincronizado: revisa `server.properties`, reinicia el MC y compara con `~/.config/pkhosting/rcon-password`. |
| Panel OFFLINE tras reiniciarlo | Normal si el MC no corría; usa INICIAR o escribe `start`. El MC sobrevive a reinicios del panel. |
| `RCON auth fallida` | El password de `server.properties` ≠ el del archivo `rcon-password`. Iguala y reinicia el MC. |
| Página no carga | `systemctl --user status pkhosting.service` y revisa el puerto en `config.json`. |
| La RAM supera el `-Xmx` (ej. 9 GB con Xmx 8 GB) | Normal: `-Xmx` limita solo el *heap*. El panel mide RSS total = heap + *metaspace* (clases de los mods) + stacks de hilos + buffers directos + JVM. Con ~200 mods, ~1 GB extra es lo esperado. Solo preocúpate si el sistema se queda sin RAM libre. |

## Desinstalación

```bash
systemctl --user disable --now pkhosting.service
rm ~/.config/systemd/user/pkhosting.service
rm -rf ~/PKHosting ~/.config/pkhosting
```

(Tu carpeta del servidor Minecraft no se toca.)

## Estructura del repo

```
panel.py              # todo el panel (backend + frontend)
install.sh            # instalador interactivo / no interactivo
pkhosting.service     # plantilla de unidad systemd --user
config.example.json   # ejemplo de configuración
README.md             # esta guía
```
