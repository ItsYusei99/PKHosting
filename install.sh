#!/usr/bin/env bash
# PKHosting — instalador (Linux + systemd --user)
# Uso interactivo:   ./install.sh
# No interactivo:     ./install.sh --non-interactive  (usa PK_* o valores por defecto)
#   Vars: PK_APP_DIR PK_SERVER_NAME PK_SUBTITLE PK_SERVER_DIR PK_START_CMD
#         PK_WORLD PK_PANEL_PORT PK_MC_PORT PK_RCON_PORT PK_MAX_MEM PK_RCON_PATCH
set -euo pipefail

REPO_URL="https://github.com/ItsYusei99/PKHosting.git"
APP_DIR="${PK_APP_DIR:-$HOME/PKHosting}"
NON_INT=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --non-interactive) NON_INT=1 ;;
    --force) FORCE=1 ;;
    --dir=*) APP_DIR="${a#--dir=}" ;;
  esac
done

say()  { printf '\033[1;35m[PKHosting]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

ask() { # ask VAR "Pregunta" "default"
  local var="$1" prompt="$2" def="$3" val
  if [ "$NON_INT" = 1 ]; then printf -v "$var" '%s' "$def"; return; fi
  read -r -p "$prompt [$def]: " val || true
  printf -v "$var" '%s' "${val:-$def}"
}

# ── 0. Dependencias ──────────────────────────────────────────────
command -v python3 >/dev/null || die "falta python3"
command -v java >/dev/null && say "java: $(java -version 2>&1 | head -n1)" \
  || warn "no se encontró 'java' (lo necesitas para el servidor Minecraft)"
command -v systemctl >/dev/null || die "falta systemctl (se requiere systemd)"

# ── 1. Ubicación de la app ───────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/panel.py" ]; then
  say "usando código local: $SCRIPT_DIR"
  mkdir -p "$APP_DIR"
  cp "$SCRIPT_DIR/panel.py" "$SCRIPT_DIR/backup.py" "$SCRIPT_DIR/pkhosting.service" \
     "$SCRIPT_DIR/pkhosting-backup.service" "$SCRIPT_DIR/pkhosting-backup.timer" "$APP_DIR/"
  chmod +x "$APP_DIR/panel.py" "$APP_DIR/backup.py"
else
  command -v git >/dev/null || die "falta git para clonar $REPO_URL"
  if [ -d "$APP_DIR/panel.py" ] || [ -f "$APP_DIR/panel.py" ]; then
    say "ya existe $APP_DIR, reutilizando"
  else
    say "clonando en $APP_DIR ..."
    git clone "$REPO_URL" "$APP_DIR"
  fi
fi

# ── 2. Preguntas ─────────────────────────────────────────────────
ask SERVER_NAME  "Nombre del servidor"                       "${PK_SERVER_NAME:-MiServidor}"
ask SUBTITLE     "Subtítulo (ej: NeoForge 1.21.1 · Java 21)" "${PK_SUBTITLE:-Minecraft Server}"
ask SERVER_DIR   "Carpeta del servidor Minecraft"            "${PK_SERVER_DIR:-$HOME/minecraft-server}"
ask START_CMD    "Comando de arranque (relativo a esa carpeta)" "${PK_START_CMD:-bash start.sh}"
ask WORLD        "Nombre de la carpeta del mundo"            "${PK_WORLD:-world}"
ask PANEL_PORT   "Puerto del panel"                          "${PK_PANEL_PORT:-8000}"
ask MC_PORT      "Puerto del servidor Minecraft"             "${PK_MC_PORT:-25565}"
ask RCON_PORT    "Puerto RCON"                               "${PK_RCON_PORT:-25575}"
ask MAX_MEM      "RAM máxima del servidor (GB, solo visual)" "${PK_MAX_MEM:-4.0}"

# ── Backups diarios ──────────────────────────────────────────────
BK_DEF=y
if [ "$NON_INT" = 1 ]; then
  BK_ANS="${PK_BACKUP:-y}"
else
  read -r -p "¿Activar backups diarios automáticos? [S/n]: " BK_ANS || true
  BK_ANS="${BK_ANS:-y}"
fi
if [[ "$BK_ANS" =~ ^[sSyY]$ ]]; then
  BK_ON=true
  ask BACKUP_DIR "Carpeta de backups" "${PK_BACKUP_DIR:-$HOME/mc-backups}"
  ask BACKUP_TIME "Hora del backup diario (HH:MM)" "${PK_BACKUP_TIME:-04:00}"
  [[ "$BACKUP_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { warn "hora inválida, uso 04:00"; BACKUP_TIME="04:00"; }
else
  BK_ON=false
  BACKUP_DIR="${PK_BACKUP_DIR:-$HOME/mc-backups}"
  BACKUP_TIME="${PK_BACKUP_TIME:-04:00}"
fi

SERVER_DIR_EXP="${SERVER_DIR/#\~/$HOME}"
[ -f "$SERVER_DIR_EXP/start.sh" ] || [ -f "$SERVER_DIR_EXP/run.sh" ] \
  || warn "$SERVER_DIR_EXP no parece un servidor (sin start.sh/run.sh). Continúo de todos modos."

# ── 3. config.json ───────────────────────────────────────────────
CFG_DIR="$HOME/.config/pkhosting"
CFG_FILE="$CFG_DIR/config.json"
mkdir -p "$CFG_DIR"
if [ -f "$CFG_FILE" ] && [ "$FORCE" = 0 ] && [ "$NON_INT" = 0 ]; then
  read -r -p "Ya existe $CFG_FILE. ¿Sobrescribir? [s/N]: " ow || true
  [[ "$ow" =~ ^[sSyY]$ ]] || { say "conservo config existente"; }
fi
if [ ! -f "$CFG_FILE" ] || [ "$FORCE" = 1 ] || [[ "${ow:-}" =~ ^[sSyY]$ ]]; then
  # start_cmd como array JSON: respeta comillas simples/dobles
  # shellcheck disable=SC2206
  read -r -a CMD_ARR <<< "$START_CMD"
  START_JSON=$(printf '"%s",' "${CMD_ARR[@]}"); START_JSON="[${START_JSON%,}]"
  cat > "$CFG_FILE" <<EOF
{
  "server_name": "$SERVER_NAME",
  "server_subtitle": "$SUBTITLE",
  "server_dir": "$SERVER_DIR",
  "start_cmd": $START_JSON,
  "world_name": "$WORLD",
  "panel_port": $PANEL_PORT,
  "mc_port": $MC_PORT,
  "rcon_port": $RCON_PORT,
  "max_mem_gb": $MAX_MEM,
  "backup_enabled": $BK_ON,
  "backup_dir": "$BACKUP_DIR",
  "backup_time": "$BACKUP_TIME",
  "retention_days": 7,
  "keep_monthly": true
}
EOF
  say "config escrita: $CFG_FILE"
fi
python3 -c "import json; json.load(open('$CFG_FILE'))" || die "config.json inválido"

# ── 4. Password RCON ─────────────────────────────────────────────
RCON_FILE="$CFG_DIR/rcon-password"
if [ ! -f "$RCON_FILE" ]; then
  if command -v python3 >/dev/null; then
    python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$RCON_FILE"
  else
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 > "$RCON_FILE"
  fi
  chmod 600 "$RCON_FILE"
  say "password RCON generado en $RCON_FILE"
else
  say "reutilizo password RCON existente"
fi
RCON_PW="$(cat "$RCON_FILE")"

# ── 5. server.properties (RCON) ──────────────────────────────────
PROP="$SERVER_DIR_EXP/server.properties"
PATCH="${PK_RCON_PATCH:-ask}"
if [ -f "$PROP" ]; then
  if [ "$PATCH" = ask ] && [ "$NON_INT" = 0 ]; then
    read -r -p "¿Configuro RCON en server.properties (hace backup .bak)? [S/n]: " yn || true
    [[ "$yn" =~ ^[nN]$ ]] && PATCH=no || PATCH=yes
  elif [ "$NON_INT" = 1 ] && [ "$PATCH" = ask ]; then
    PATCH=yes
  fi
  if [ "$PATCH" = yes ]; then
    cp "$PROP" "$PROP.bak"
    python3 - "$PROP" "$RCON_PW" "$RCON_PORT" <<'EOF'
import sys
prop, pw, port = sys.argv[1], sys.argv[2], sys.argv[3]
out = []
for line in open(prop).read().splitlines():
    if line.startswith("enable-rcon="): out.append("enable-rcon=true")
    elif line.startswith("rcon.password="): out.append(f"rcon.password={pw}")
    elif line.startswith("rcon.port="): out.append(f"rcon.port={port}")
    else: out.append(line)
open(prop, "w").write("\n".join(out) + "\n")
EOF
    say "RCON activado en server.properties (backup: server.properties.bak)"
    warn "REINICIA el servidor Minecraft para que RCON tome efecto"
  fi
else
  warn "no existe $PROP (se configurará RCON cuando crees el servidor)"
fi

# ── 6. Servicio systemd --user ───────────────────────────────────
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|%h/PKHosting/panel.py|$APP_DIR/panel.py|" "$APP_DIR/pkhosting.service" > "$UNIT_DIR/pkhosting.service"
systemctl --user daemon-reload
systemctl --user enable --now pkhosting.service
if [ "$BK_ON" = true ]; then
  sed -e "s|@APP_DIR@|$APP_DIR|" "$APP_DIR/pkhosting-backup.service" > "$UNIT_DIR/pkhosting-backup.service"
  sed -e "s|@BACKUP_TIME@|$BACKUP_TIME|" "$APP_DIR/pkhosting-backup.timer" > "$UNIT_DIR/pkhosting-backup.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now pkhosting-backup.timer
  say "backups diarios a las $BACKUP_TIME en $BACKUP_DIR (7 días + mensual eterno)"
else
  say "backups automáticos DESACTIVADOS (puedes hacerlos manuales desde el panel)"
fi
sleep 2
systemctl --user is-active pkhosting.service >/dev/null \
  && say "panel ACTIVO → http://127.0.0.1:$PANEL_PORT" \
  || { warn "el servicio no arrancó; revisa: journalctl --user -u pkhosting.service -n 30"; exit 1; }

cat <<EOF

  ── Listo ─────────────────────────────────────────
  Panel:      http://127.0.0.1:$PANEL_PORT
  Config:     $CFG_FILE
  RCON pw:    $RCON_FILE  (también en server.properties)
  Servicio:   systemctl --user {status,restart,stop} pkhosting.service
  Siguiente:  enciende el server con el botón INICIAR o escribe 'start'
               en la consola. Si activaste RCON ahora, reinicia el MC.
  Opcional:   expónlo con playit.gg y pega la IP en Configuración.
EOF
