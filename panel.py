#!/usr/bin/env python3
"""
PKHosting — panel web instalable para servidores Minecraft Java.
- Estado en tiempo real (RUNNING / OFFLINE / STARTING / STOPPING)
- Métricas en vivo con gráficos de CPU y RAM
- Consola dedicada con alias de nivel panel (start/stop/restart/reload/kill)
- Explorador de archivos con CRUD + subida multipart + editor
- RCON persistente (sobrevive a reinicios del panel)
- Configuración en ~/.config/pkhosting/config.json (ver config.example.json)
"""

PK_VERSION = "1.0.0"

import collections
import datetime
import html
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

CONFIG_DIR = os.path.expanduser("~/.config/pkhosting")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "server_name": "MiServidor",
    "server_subtitle": "Minecraft Server",
    "server_dir": "~/minecraft-server",
    "start_cmd": ["bash", "start.sh"],
    "world_name": "world",
    "panel_port": 8000,
    "mc_port": 25565,
    "rcon_port": 25575,
    "max_mem_gb": 4.0,
    "backup_enabled": False,
    "backup_dir": "~/mc-backups",
    "backup_time": "04:00",
    "retention_days": 7,
    "keep_monthly": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        for k in cfg:
            if k in user:
                cfg[k] = user[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[pkhosting] config inválida ({e}), usando valores por defecto)", flush=True)
    cfg["server_dir"] = os.path.expanduser(cfg["server_dir"])
    return cfg


CFG = load_config()

SERVER_DIR = CFG["server_dir"]
START_CMD = CFG["start_cmd"]
LOG_FILE = os.path.join(SERVER_DIR, "logs", "latest.log")
CHILD_LOG = os.path.join(SERVER_DIR, "logs", "panel-child.log")
PORT = int(CFG["panel_port"])
MC_PORT = int(CFG["mc_port"])
RCON_HOST = "127.0.0.1"
RCON_PORT = int(CFG["rcon_port"])
MAX_MEM_GB = float(CFG["max_mem_gb"])
SERVER_NAME = CFG["server_name"]
WORLD_NAME = CFG["world_name"]


def _first_existing(*paths):
    for p in paths:
        try:
            if p and os.path.isfile(p):
                return p
        except Exception:
            continue
    return paths[0]


RCON_PASS_FILE = _first_existing(
    os.path.join(CONFIG_DIR, "rcon-password"),
    os.path.expanduser("~/.config/mc-panel-rcon"),  # legado
)


def get_rcon_password():
    try:
        with open(RCON_PASS_FILE) as f:
            pw = f.read().strip()
            return pw or None
    except Exception:
        return None


def rcon_send(cmd, timeout=5):
    """Cliente RCON minimo (stdlib). Devuelve (ok, respuesta)."""
    import socket as _sock
    import struct as _struct
    pw = get_rcon_password()
    if not pw:
        return False, "RCON sin password configurado"
    try:
        s = _sock.create_connection((RCON_HOST, RCON_PORT), timeout=timeout)
        s.settimeout(timeout)
        _id = 1

        def pkt(pid, ptype, payload):
            body = _struct.pack("<ii", pid, ptype) + payload.encode("utf-8") + b"\x00\x00"
            return _struct.pack("<i", len(body)) + body

        def read_pkt():
            hdr = b""
            while len(hdr) < 4:
                ch = s.recv(4 - len(hdr))
                if not ch:
                    raise ConnectionError("RCON cerrado")
                hdr += ch
            (ln,) = _struct.unpack("<i", hdr)
            data = b""
            while len(data) < ln:
                ch = s.recv(ln - len(data))
                if not ch:
                    raise ConnectionError("RCON incompleto")
                data += ch
            rid, rtype = _struct.unpack("<ii", data[:8])
            return rid, rtype, data[8:-2].decode("utf-8", "replace")

        s.sendall(pkt(_id, 3, pw))
        rid, rtype, _ = read_pkt()
        if rid == -1:
            s.close()
            return False, "RCON auth fallida (password desincronizado?)"
        s.sendall(pkt(_id + 1, 2, cmd))
        _, _, resp = read_pkt()
        s.close()
        return True, resp
    except Exception as e:
        return False, f"RCON error: {e}"


def write_stdin_fallback(pid, text):
    """Escribe a /proc/<pid>/fd/0 sin bloquear (O_NONBLOCK)."""
    import os as _os
    import fcntl as _fcntl
    path = f"/proc/{pid}/fd/0"
    fd = None
    try:
        fd = _os.open(path, _os.O_WRONLY | _os.O_NONBLOCK)
        _os.write(fd, (text + "\n").encode("utf-8"))
        return True, "enviado"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            if fd is not None:
                _os.close(fd)
        except Exception:
            pass

PUBLIC_IP_FILE = _first_existing(
    os.path.join(CONFIG_DIR, "public-ip"),
    os.path.expanduser("~/.config/mc-panel-public-ip"),  # legado
)


def get_public_ip():
    """IP pública (playit.gg) configurada para mostrar en el panel."""
    for path in (PUBLIC_IP_FILE,
                 os.path.join(CONFIG_DIR, "public-ip"),
                 os.path.expanduser("~/.config/mc-panel-public-ip")):
        try:
            with open(path) as f:
                ip = f.read().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return f"localhost:{MC_PORT}"


def set_public_ip(ip):
    ip = (ip or "").strip()
    if not ip:
        return False, "IP vacía"
    if len(ip) > 120 or any(c in ip for c in ("\n", "\r")):
        return False, "IP inválida"
    os.makedirs(os.path.dirname(PUBLIC_IP_FILE), exist_ok=True)
    with open(PUBLIC_IP_FILE, "w") as f:
        f.write(ip + "\n")
    try:
        os.chmod(PUBLIC_IP_FILE, 0o600)
    except Exception:
        pass
    return True, f"IP pública actualizada: {ip}"


def get_playit_status():
    """Best-effort: estado del agente playit sin requerir root."""
    import shutil as _sh
    import subprocess as _sp
    info = {"installed": False, "service_active": False, "reachable": False, "note": ""}
    if _sh.which("playit"):
        info["installed"] = True
    try:
        r = _sp.run(["systemctl", "is-active", "playit.service"],
                    capture_output=True, text=True, timeout=5)
        info["service_active"] = (r.stdout.strip() == "active")
    except Exception:
        pass
    for sock in ("/run/playit/playitd.sock",):
        try:
            with open(sock, "rb"):
                pass
            info["reachable"] = True
        except Exception:
            pass
    if info["installed"] and info["service_active"] and not info["reachable"]:
        info["note"] = "Agente activo pero socket sin permiso (agrega tu usuario al grupo playit o pega la IP pública manualmente)."
    return info


state_lock = threading.Lock()
proc = None
stop_requested = False

# Buffer de historial de métricas para gráficos (últimos 40 puntos = ~1 minuto)
metrics_history = collections.deque(maxlen=40)
cpu_prev_sample = None

def find_running_mc_pid():
    """PID del java del servidor: coincide cwd con SERVER_DIR (genérico para
    Vanilla/Forge/Fabric/NeoForge). Excluye lanzadores de cliente."""
    root = os.path.realpath(SERVER_DIR)
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "ignore")
            if not cmd:
                continue
            low = cmd.lower()
            if "theseus" in low or "forgeclient" in low or "modrinthapp" in low:
                continue
            try:
                exe = os.path.basename(os.readlink(f"/proc/{pid_str}/exe"))
            except Exception:
                exe = ""
            if not exe.startswith("java"):
                continue
            try:
                cwd = os.path.realpath(f"/proc/{pid_str}/cwd")
            except Exception:
                continue
            if cwd == root:
                return int(pid_str)
        except Exception:
            continue
    # Fallback: heurística NeoForge solo si el cwd no es legible o coincide
    # (evita secuestrar OTRO servidor con distinto directorio en multi-instancia).
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "ignore")
            low = cmd.lower()
            if "theseus" in low or "forgeclient" in low or "modrinthapp" in low:
                continue
            if "unix_args.txt" in cmd and "libraries" in cmd:
                try:
                    cwd = os.path.realpath(f"/proc/{pid_str}/cwd")
                except Exception:
                    cwd = None
                if cwd is None or cwd == root:
                    return int(pid_str)
        except Exception:
            continue
    return None

def is_port_listening(port):
    """Verifica si un puerto está en estado LISTEN directamente en /proc/net."""
    hex_port = f":{port:04X}"
    for net_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(net_file) as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[1].endswith(hex_port) and parts[3] == "0A":
                        return True
        except Exception:
            pass
    return False

def get_server_status(pid):
    global stop_requested
    if pid is None:
        stop_requested = False
        return "OFFLINE"
    if stop_requested:
        return "STOPPING"
    if is_port_listening(MC_PORT):
        return "RUNNING"
    return "STARTING"

def calculate_cpu(pid):
    global cpu_prev_sample
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        utime = int(parts[13])
        stime = int(parts[14])
        now = time.time()

        if cpu_prev_sample is not None and cpu_prev_sample["pid"] == pid:
            du = (utime + stime) - (cpu_prev_sample["utime"] + cpu_prev_sample["stime"])
            dt = now - cpu_prev_sample["time"]
            cpu_prev_sample = {"pid": pid, "utime": utime, "stime": stime, "time": now}
            if dt > 0:
                clk_tck = os.sysconf(os.sysconf_names.get('SC_CLK_TCK', 'SC_CLK_TCK')) or 100
                pct = (du / float(clk_tck)) / dt * 100.0
                return max(0.0, round(pct, 1))
        else:
            cpu_prev_sample = {"pid": pid, "utime": utime, "stime": stime, "time": now}
            return 0.0
    except Exception:
        return 0.0

def get_memory_mb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0

def get_process_uptime(pid):
    if pid is None:
        return "-"
    try:
        st = os.stat(f"/proc/{pid}")
        elapsed = int(time.time() - st.st_mtime)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        return f"{h}h {m:02d}m {s:02d}s"
    except Exception:
        return "-"

def get_world_size_gb():
    try:
        world_path = os.path.join(SERVER_DIR, WORLD_NAME)
        total = sum(os.path.getsize(os.path.join(dp, f))
                    for dp, _, fns in os.walk(world_path)
                    for f in fns)
        return round(total / (1024**3), 2)
    except Exception:
        return 0.0

def query_mc_players():
    s = None
    try:
        s = socket.create_connection(("127.0.0.1", MC_PORT), timeout=2)
        def varint(n):
            n &= 0xFFFFFFFF  # soporta protocolo -1 (cualquier versión)
            out = b""
            while True:
                b = n & 0x7F
                n >>= 7
                out += struct.pack("B", b | (0x80 if n else 0))
                if not n:
                    return out
        host = b"localhost"
        hs = varint(0) + varint(-1) + varint(len(host)) + host + struct.pack(">H", MC_PORT) + varint(1)
        s.sendall(varint(len(hs)) + hs)
        s.sendall(varint(1) + varint(0))
        def rvi():
            # Devuelve (valor, bytes consumidos)
            n = sh = size = 0
            while True:
                chunk = s.recv(1)
                if not chunk:
                    raise ConnectionError()
                size += 1
                b = chunk[0]
                n |= (b & 0x7F) << sh
                if not (b & 0x80):
                    return n, size
                sh += 7
        plen, _ = rvi()       # largo total (incluye el packet ID)
        _, pid_len = rvi()    # packet ID (status = 0, 1 byte)
        need = plen - pid_len  # lo que falta es solo el JSON
        data = b""
        while len(data) < need:
            chunk = s.recv(min(4096, need - len(data)))
            if not chunk:
                raise ConnectionError("respuesta truncada")
            data += chunk
        info = json.loads(data.decode("utf-8", "ignore"))
        players = info.get("players", {})
        names = [p.get("name", "?") for p in players.get("sample", []) or []]
        return players.get("online", 0), players.get("max", 20), names
    except Exception:
        return 0, 20, []
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def player_action(action, name, reason=""):
    """op / deop / kick / ban con validación. Devuelve (ok, msg)."""
    action = (action or "").lower()
    name = (name or "").strip()
    reason = (reason or "").strip().replace("\n", " ")[:100]
    if action not in ("op", "deop", "kick", "ban"):
        return False, "acción inválida"
    if not PLAYER_NAME_RE.match(name):
        return False, "nombre de jugador inválido (3-16 letras, números o _)"
    if find_running_mc_pid() is None:
        return False, "El servidor está apagado"
    if action in ("kick", "ban") and reason:
        cmd = f"{action} {name} {reason}"
    else:
        cmd = f"{action} {name}"
    ok, msg = send_command_action(cmd)
    if ok:
        labels = {"op": "OP otorgado a", "deop": "OP retirado a",
                  "kick": "Expulsado", "ban": "Baneado"}
        return True, f"{labels[action]} {name}"
    return False, msg

def metrics_worker():
    """Hilo de fondo que muestrea métricas cada 1.5s de forma continua."""
    while True:
        try:
            pid = find_running_mc_pid()
            status = get_server_status(pid)
            cpu = calculate_cpu(pid) if pid else 0.0
            mem = get_memory_mb(pid) if pid else 0
            now_str = time.strftime("%H:%M:%S")
            metrics_history.append({
                "time": now_str,
                "cpu": cpu,
                "mem": mem,
                "status": status
            })
        except Exception:
            pass
        time.sleep(1.5)

threading.Thread(target=metrics_worker, daemon=True).start()

def start_server_action():
    global proc, stop_requested
    with state_lock:
        current_pid = find_running_mc_pid()
        if current_pid is not None:
            return False, f"El servidor ya está en ejecución (PID {current_pid})"
        try:
            logf = open(CHILD_LOG, "ab", buffering=0)
            proc = subprocess.Popen(
                START_CMD, cwd=SERVER_DIR,
                stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                close_fds=True
            )
            stop_requested = False
            return True, f"Servidor iniciando (PID {proc.pid})..."
        except Exception as e:
            proc = None
            return False, f"Error al iniciar: {e}"

def stop_server_action(force=False):
    global proc, stop_requested
    with state_lock:
        pid = find_running_mc_pid()
        if pid is None:
            stop_requested = False
            return False, "El servidor ya está apagado"
        stop_requested = True
        try:
            if force:
                # Intentar stop elegante por RCON primero, luego SIGKILL
                ok, _ = rcon_send("stop", timeout=3)
                time.sleep(2)
                if find_running_mc_pid() is None:
                    stop_requested = False
                    return True, "Servidor detenido (RCON stop)"
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stop_requested = False
                return True, "Servidor terminado forzosamente (SIGKILL)"
            # Apagado elegante en cascada: stdin -> RCON -> SIGTERM
            if proc is not None and proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(b"stop\n")
                    proc.stdin.flush()
                    return True, "Enviada orden de apagado segura (stdin, guardando mundo)..."
                except Exception:
                    pass
            ok, resp = rcon_send("stop", timeout=4)
            if ok:
                return True, "Enviada orden de apagado segura (RCON, guardando mundo)..."
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                stop_requested = False
                return False, "El servidor ya está apagado"
            return True, f"Enviada señal SIGTERM (RCON no disponible: {resp})..."
        except Exception as e:
            return False, f"Error al detener: {e}"

def restart_server_action():
    ok, msg = stop_server_action(force=False)
    if not ok and "apagado" not in msg.lower():
        return ok, msg
    def _delayed():
        time.sleep(5)
        start_server_action()
    threading.Thread(target=_delayed, daemon=True).start()
    return True, "Reiniciando servidor..."


def send_command_action(cmd):
    global proc
    cmd = (cmd or "").strip()
    if not cmd:
        return False, "Comando vacío"
    low = cmd.lower().lstrip("/")
    # Comandos de nivel panel (no existen como comando MC o se prefieren gestionados)
    if low in ("start", "panel:start"):
        pid0 = find_running_mc_pid()
        if pid0 is not None:
            return False, f"El servidor ya está en ejecución (PID {pid0})"
        return start_server_action()
    if low in ("stop", "panel:stop"):
        return stop_server_action(force=False)
    if low in ("restart", "panel:restart"):
        return restart_server_action()
    if low in ("kill", "panel:kill"):
        return stop_server_action(force=True)
    # reload: si el server está apagado no tiene sentido reenviarlo al MC
    if low in ("reload", "minecraft:reload"):
        if find_running_mc_pid() is None:
            return False, "El servidor está apagado (usa 'start' para encenderlo)"
        # si está online se deja pasar al MC (recarga datapacks)
    pid = find_running_mc_pid()
    if pid is None:
        return False, "El servidor está apagado (usa 'start' para encenderlo)"
    # Nivel 1: stdin directo si el panel lanzó el proceso
    if proc is not None and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write((cmd + "\n").encode("utf-8"))
            proc.stdin.flush()
            return True, f"Comando enviado: {cmd}"
        except Exception:
            pass  # caer a RCON
    # Nivel 2: RCON (sobrevive a reinicios del panel)
    ok, resp = rcon_send(cmd)
    if ok:
        short = (resp or "").strip().splitlines()
        extra = (": " + short[0][:160]) if short and short[0].strip() else ""
        return True, f"Comando enviado (RCON): {cmd}{extra}"
    rcon_err = resp
    # Nivel 3: escritura no bloqueante a /proc/<pid>/fd/0
    ok2, msg2 = write_stdin_fallback(pid, cmd)
    if ok2:
        return True, f"Comando enviado (stdin): {cmd}"
    return False, f"No se pudo enviar ({rcon_err} / {msg2})"

def get_server_stats_data():
    pid = find_running_mc_pid()
    status = get_server_status(pid)
    uptime = get_process_uptime(pid)
    world_gb = get_world_size_gb()
    online, max_p, names = query_mc_players() if status == "RUNNING" else (0, 20, [])
    
    latest_metric = list(metrics_history)[-1] if metrics_history else {"cpu": 0.0, "mem": 0}
    
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()[:3]
    except Exception:
        load = ["0.00", "0.00", "0.00"]

    return {
        "status": status,
        "pid": pid,
        "uptime": uptime,
        "cpu": latest_metric["cpu"],
        "mem_mb": latest_metric["mem"],
        "mem_gb": round(latest_metric["mem"] / 1024.0, 2),
        "max_mem_gb": MAX_MEM_GB,
        "world_gb": world_gb,
        "online_players": online,
        "max_players": max_p,
        "player_names": names,
        "system_load": ", ".join(load),
        "port": MC_PORT,
        "public_ip": get_public_ip(),
        "playit": get_playit_status(),
        "history": list(metrics_history)
    }


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import backup as bk
    HAVE_BACKUP = True
except Exception as e:
    bk = None
    HAVE_BACKUP = False
    print(f"[pkhosting] backup.py no disponible: {e}", flush=True)

backup_lock = threading.Lock()
backup_state = {"running": False, "job": None, "msg": "", "updated": 0,
                "pct": 0, "stage": ""}


def _bk_progress(pct, stage):
    with backup_lock:
        backup_state.update(pct=max(0, min(100, int(pct))), stage=stage,
                            updated=time.time())


def _bk_log(m, *args, **kwargs):
    # Acepta flush=True porque backup.py llama log(msg, flush=True) como print
    with backup_lock:
        backup_state.update(msg=m, updated=time.time())


def _backup_cfg():
    c = load_config()
    return {
        "enabled": bool(c.get("backup_enabled", False)),
        "dir": os.path.expanduser(c.get("backup_dir", "~/mc-backups")),
        "retention_days": int(c.get("retention_days", 7)),
        "keep_monthly": bool(c.get("keep_monthly", True)),
        "time": str(c.get("backup_time", "04:00")),
    }


def _bk_dirs():
    c = _backup_cfg()
    return SERVER_DIR, WORLD_NAME, c["dir"], c["retention_days"], c["keep_monthly"]


def _bk_send(cmd):
    ok, msg = send_command_action(cmd)
    return ok, msg


def _backup_worker(mode, name=None):
    with backup_lock:
        backup_state.update(running=True, job=mode, msg="iniciando...",
                            pct=0, stage="iniciando", updated=time.time())
    try:
        sdir, world, bdir, ret, keepm = _bk_dirs()
        if mode == "run":
            ok, msg = bk.run_backup(sdir, world, bdir, send_fn=_bk_send,
                                    retention_days=ret, keep_monthly=keepm,
                                    log=_bk_log, progress=_bk_progress)
        elif mode == "restore":
            ok, msg = bk.restore_backup(
                sdir, bdir, name,
                stop_fn=lambda: stop_server_action(force=False),
                start_fn=lambda: start_server_action(),
                is_running_fn=lambda: find_running_mc_pid() is not None,
                pre_backup=True, retention_days=ret, keep_monthly=keepm,
                log=_bk_log, progress=_bk_progress)
        else:
            ok, msg = False, "trabajo desconocido"
        with backup_lock:
            backup_state.update(running=False, msg=("OK " if ok else "FAIL ") + msg,
                                pct=100 if ok else backup_state.get("pct", 0),
                                stage="completado" if ok else "error",
                                updated=time.time())
    except Exception as e:
        with backup_lock:
            backup_state.update(running=False, msg=f"FAIL {e}",
                                stage="error", updated=time.time())


def backups_status():
    import shutil as _sh
    c = _backup_cfg()
    info = {"ok": HAVE_BACKUP, "enabled": c["enabled"], "dir": c["dir"],
            "time": c["time"], "retention_days": c["retention_days"],
            "keep_monthly": c["keep_monthly"],
            "job": dict(backup_state), "items": []}
    if not HAVE_BACKUP:
        info["msg"] = "backup.py no disponible"
        return info
    try:
        if c["enabled"]:
            os.makedirs(c["dir"], exist_ok=True)
        ok, err = bk.check_writable(c["dir"]) if os.path.isdir(c["dir"]) else (False, "carpeta inexistente")
        info["writable"] = ok
        info["writable_msg"] = "" if ok else err
    except Exception as e:
        info["writable"] = False
        info["writable_msg"] = str(e)
    try:
        info["free_gb"] = round(_sh.disk_usage(c["dir"]).free / 1e9, 1) if os.path.isdir(c["dir"]) else 0
    except Exception:
        info["free_gb"] = 0
    try:
        items = bk.list_backups(c["dir"])
        for i in items:
            i["size"] = (f"{i['size_b']/1e9:.2f} GB" if i["size_b"] > 1e9
                         else f"{i['size_b']/1e6:.0f} MB")
            i["date"] = datetime.datetime.fromtimestamp(i["mtime"]).strftime("%d/%m/%Y %H:%M")
        info["items"] = items
    except Exception as e:
        info["msg"] = str(e)
    return info


SAFE_FS_ROOT = os.path.realpath(SERVER_DIR)
MAX_FILE_READ = 2 * 1024 * 1024  # 2 MB

_dirsize_cache = {}
_dirsize_ttl = 30


def dir_size_b(path):
    """Tamaño recursivo de un directorio (scandir, sin symlinks) con caché TTL."""
    now = time.time()
    hit = _dirsize_cache.get(path)
    if hit and now - hit[0] < _dirsize_ttl:
        return hit[1]
    total = 0
    try:
        stack = [path]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            else:
                                total += e.stat(follow_symlinks=False).st_size
                        except Exception:
                            continue
            except Exception:
                continue
    except Exception:
        pass
    _dirsize_cache[path] = (now, total)
    # Limitar crecimiento del caché
    if len(_dirsize_cache) > 200:
        _dirsize_cache.clear()
    return total


def safe_fs_path(rel):
    rel = (rel or "").lstrip("/")
    target = os.path.realpath(os.path.join(SAFE_FS_ROOT, rel))
    if target != SAFE_FS_ROOT and not target.startswith(SAFE_FS_ROOT + os.sep):
        return None
    return target


def fs_list(rel=""):
    target = safe_fs_path(rel)
    if target is None or not os.path.isdir(target):
        return None, "Ruta inválida"
    entries = []
    try:
        for name in sorted(os.listdir(target), key=str.lower):
            if name.startswith(".") and rel == "":
                continue
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
                is_d = os.path.isdir(full)
                sz = dir_size_b(full) if is_d else st.st_size
                if sz > 1024 * 1024:
                    size = f"{sz // (1024*1024)} MB"
                elif sz > 1024:
                    size = f"{sz // 1024} KB"
                elif not is_d:
                    size = f"{sz} B"
                else:
                    size = "-"
                entries.append({
                    "name": name,
                    "is_dir": is_d,
                    "size": size,
                    "size_b": sz if not is_d else 0,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
        return {"path": rel.strip("/"), "entries": entries}, None
    except Exception as e:
        return None, str(e)

def read_console_lines(limit=250):
    for fpath in (CHILD_LOG, LOG_FILE):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                if lines:
                    return lines[-limit:]
            except Exception:
                continue
    return []

# ══════════════════════════════════════════════════════════════════
# PLANTILLA HTML/CSS/JS (Estilo BisectHosting / Pterodactyl)
# ══════════════════════════════════════════════════════════════════

HTML_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PKHosting Panel — PrankLindorf</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg-main: #050508;
  --bg-sidebar: #0a0a13;
  --bg-card: #101016;
  --bg-card-hover: #171722;
  --bg-terminal: #06060b;
  --border-color: #2b2440;
  --border-subtle: #1c1729;

  --text-main: #f5f3ff;
  --text-muted: #a89fc7;
  --text-dim: #6d6490;

  --accent-cyan: #a855f7;
  --accent-blue: #8b5cf6;
  --accent-soft: #c084fc;
  --color-online: #34d399;
  --color-starting: #fbbf24;
  --color-offline: #f87171;
  --color-stopping: #fb923c;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg-main);
  color: var(--text-main);
  font-family: 'Inter', system-ui, sans-serif;
  display: flex;
  min-height: 100vh;
  overflow-x: hidden;
}

/* SIDEBAR */
aside {
  width: 260px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.sidebar-header {
  padding: 22px 20px;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-icon {
  width: 38px;
  height: 38px;
  background: linear-gradient(135deg, #7c3aed, #a855f7);
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  box-shadow: 0 4px 12px rgba(6, 182, 212, 0.3);
}
.brand-name {
  font-size: 17px;
  font-weight: 800;
  letter-spacing: -0.5px;
  color: #fff;
}
.brand-name span { color: var(--accent-cyan); }
.brand-sub {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}

.server-selector {
  padding: 16px 20px;
  background: rgba(0,0,0,0.2);
  border-bottom: 1px solid var(--border-color);
}
.server-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.7px;
  color: var(--text-dim);
  font-weight: 700;
  margin-bottom: 4px;
}
.server-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-main);
  display: flex;
  align-items: center;
  gap: 8px;
}
.mini-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-offline);
}

nav {
  padding: 18px 12px;
  flex: 1;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 11px 14px;
  color: var(--text-muted);
  text-decoration: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 4px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.nav-item:hover {
  background: var(--bg-card);
  color: var(--text-main);
}
.nav-item.active {
  background: rgba(6, 182, 212, 0.12);
  color: var(--accent-cyan);
  font-weight: 600;
  border-left: 3px solid var(--accent-cyan);
}
.nav-icon { font-size: 18px; width: 22px; text-align: center; }

.sidebar-footer {
  padding: 16px;
  border-top: 1px solid var(--border-color);
  background: rgba(0,0,0,0.15);
}
.ip-chip {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 8px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--accent-cyan);
  cursor: pointer;
  transition: border-color 0.2s;
}
.ip-chip:hover { border-color: var(--accent-cyan); }
.copy-badge {
  font-size: 10px;
  text-transform: uppercase;
  background: rgba(6, 182, 212, 0.2);
  padding: 2px 6px;
  border-radius: 4px;
}

/* MAIN CONTENT */
main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  max-height: 100vh;
}

/* TOP HEADER */
header {
  background: var(--bg-sidebar);
  border-bottom: 1px solid var(--border-color);
  padding: 16px 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  position: sticky;
  top: 0;
  z-index: 20;
}
.header-info h1 {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -0.5px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.header-subtitle {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 3px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 14px;
}

/* STATUS BADGE */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 30px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  background: rgba(239, 68, 68, 0.15);
  color: var(--color-offline);
  border: 1px solid rgba(239, 68, 68, 0.3);
}
.status-pill.RUNNING {
  background: rgba(16, 185, 129, 0.15);
  color: var(--color-online);
  border-color: rgba(16, 185, 129, 0.3);
}
.status-pill.STARTING {
  background: rgba(245, 158, 11, 0.15);
  color: var(--color-starting);
  border-color: rgba(245, 158, 11, 0.3);
}
.status-pill.STOPPING {
  background: rgba(249, 115, 22, 0.15);
  color: var(--color-stopping);
  border-color: rgba(249, 115, 22, 0.3);
}
.status-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 8px currentColor;
}

/* POWER BUTTONS (Pterodactyl style) */
.power-btn-group {
  display: flex;
  gap: 6px;
  background: var(--bg-main);
  padding: 4px;
  border-radius: 10px;
  border: 1px solid var(--border-color);
}
.pbtn {
  border: none;
  outline: none;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  padding: 8px 14px;
  border-radius: 7px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s ease;
  color: #fff;
}
.pbtn-start { background: #15803d; }
.pbtn-start:hover:not(:disabled) { background: #16a34a; }
.pbtn-restart { background: #1e3a8a; }
.pbtn-restart:hover:not(:disabled) { background: #2563eb; }
.pbtn-stop { background: #b91c1c; }
.pbtn-stop:hover:not(:disabled) { background: #dc2626; }
.pbtn-kill { background: #450a0a; border: 1px solid #7f1d1d; }
.pbtn-kill:hover:not(:disabled) { background: #7f1d1d; }

.pbtn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
  filter: grayscale(0.5);
}

/* CONTENT CONTAINER */
.page-container {
  padding: 24px 28px;
  flex: 1;
}

/* 4 STATS CARDS */
.stats-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}
.scard {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 16px 18px;
  position: relative;
  overflow: hidden;
  transition: transform 0.15s;
}
.scard-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--text-dim);
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.scard-val {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.5px;
  margin: 8px 0 6px;
  font-variant-numeric: tabular-nums;
  color: var(--text-main);
}
.scard-sub {
  font-size: 12px;
  color: var(--text-muted);
}
.scard-bar {
  height: 4px;
  background: rgba(255,255,255,0.06);
  border-radius: 2px;
  margin-top: 10px;
  overflow: hidden;
}
.scard-bar-fill {
  height: 100%;
  background: var(--accent-cyan);
  width: 0%;
  transition: width 0.3s ease;
}

/* TABS */
.tab-content { display: none; }
.tab-content.active { display: block; }

/* CONSOLE VIEW */
.terminal-wrapper {
  background: var(--bg-terminal);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  height: 520px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.5);
}
.terminal-topbar {
  padding: 10px 16px;
  background: rgba(255,255,255,0.02);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-muted);
}
.terminal-topbar-tools {
  display: flex;
  gap: 8px;
}
.term-tool-btn {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  color: var(--text-muted);
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 5px;
  cursor: pointer;
}
.term-tool-btn:hover { color: var(--text-main); border-color: var(--text-dim); }

.terminal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12.5px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: #cbd5e1;
}
.terminal-input-bar {
  padding: 12px 14px;
  background: var(--bg-card);
  border-top: 1px solid var(--border-color);
  display: flex;
  gap: 10px;
  align-items: center;
}
.cmd-prompt {
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-cyan);
  font-weight: 700;
}
.cmd-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  color: #fff;
}
.cmd-input::placeholder { color: var(--text-dim); }
.cmd-btn {
  background: var(--accent-cyan);
  color: #0c0f17;
  border: none;
  font-weight: 700;
  padding: 6px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.cmd-btn:hover { background: #c084fc; }

/* METRICS VIEW */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 24px;
}
.chart-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 20px;
}
.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.chart-title { font-size: 15px; font-weight: 700; }
.chart-badge {
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-cyan);
}
canvas {
  width: 100% !important;
  height: 200px !important;
  background: rgba(0,0,0,0.25);
  border-radius: 8px;
}

.system-details-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 20px;
}
.details-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.details-table tr {
  border-bottom: 1px solid var(--border-subtle);
}
.details-table td {
  padding: 10px 6px;
}
.details-table td:first-child {
  color: var(--text-muted);
  width: 200px;
}
.details-table td:last-child {
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-main);
  text-align: right;
}

/* FILE EXPLORER VIEW */
.file-list {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  overflow: hidden;
}
.file-row {
  display: flex;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 13px;
  gap: 12px;
  cursor: pointer;
  transition: background 0.15s;
}
.file-row:hover { background: var(--bg-card-hover); }
.file-name { flex: 1; font-family: 'JetBrains Mono', monospace; }
.file-size { color: var(--text-dim); font-size: 12px; }
.file-icon { display: inline-flex; flex-shrink: 0; }
.file-icon svg { width: 20px; height: 20px; }
.file-icon.is-folder { color: #c084fc; filter: drop-shadow(0 0 6px rgba(168,85,247,0.45)); }
.file-icon.is-file { color: #7d8aa5; }
.file-actions { display: flex; gap: 6px; flex-shrink: 0; }
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 8px;
  background: transparent; border: 1px solid transparent;
  color: var(--text-muted); cursor: pointer; transition: all 0.15s;
}
.icon-btn:hover { background: var(--bg-card-hover); color: var(--text-main); border-color: var(--border-color); }
.icon-btn.danger:hover { color: #f87171; border-color: #7f1d1d; }
.icon-btn svg { width: 16px; height: 16px; }

/* OUTLINE ICONS (stroke style) */
.nav-icon { width: 22px; text-align: center; display: inline-flex; justify-content: center; }
.nav-icon svg { width: 19px; height: 19px; }
.brand-icon svg { width: 22px; height: 22px; }
.pbtn svg { width: 14px; height: 14px; }
.scard-ico { display: inline-flex; color: var(--text-muted); }
.scard-ico svg { width: 16px; height: 16px; }
svg.ico { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.cmd-btn { display: inline-flex; align-items: center; gap: 6px; }
.cmd-btn svg { width: 14px; height: 14px; }
.fs-toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }
.fs-crumb { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-muted); }
.fs-crumb a { color: var(--accent-cyan); cursor: pointer; text-decoration: none; }

/* TOAST MESSAGE */
#toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #1e293b;
  color: #fff;
  padding: 12px 20px;
  border-radius: 8px;
  font-size: 13px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5);
  border-left: 4px solid var(--accent-cyan);
  opacity: 0;
  transform: translateY(10px);
  transition: all 0.25s ease;
  pointer-events: none;
  z-index: 100;
}
#toast.show { opacity: 1; transform: translateY(0); }

/* LOG HIGHLIGHTS */
.log-info { color: #94a3b8; }
.log-warn { color: #facc15; }
.log-err { color: #f87171; font-weight: 600; }
.log-done { color: #4ade80; font-weight: 700; }
</style>
<style>
/* ═══ LIQUID GLASS · negro + morado · principios Apple: claridad, deferencia, profundidad ═══ */
:root {
  --glass-bg: linear-gradient(155deg, rgba(255,255,255,0.09), rgba(255,255,255,0.02) 55%, rgba(168,85,247,0.06));
  --glass-border: rgba(255,255,255,0.12);
  --glass-hi: inset 0 1px 0 rgba(255,255,255,0.16);
  --glass-shadow: 0 12px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(0,0,0,0.4), 0 0 32px rgba(139,92,246,0.07);
  --r-lg: 20px;
  --r-md: 14px;
  --ease-apple: cubic-bezier(0.32, 0.72, 0, 1);
}
* { -webkit-tap-highlight-color: transparent; }
::selection { background: rgba(168,85,247,0.45); color: #fff; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', system-ui, sans-serif;
  background:
    radial-gradient(900px 480px at 12% -8%, rgba(124,58,237,0.22), transparent 65%),
    radial-gradient(760px 520px at 88% 4%, rgba(168,85,247,0.16), transparent 60%),
    radial-gradient(700px 700px at 50% 110%, rgba(76,29,149,0.20), transparent 65%),
    #050508;
  background-attachment: fixed;
  letter-spacing: -0.01em;
}
main { position: relative; z-index: 1; }

/* Superficies de vidrio */
aside {
  background: linear-gradient(180deg, rgba(20,16,34,0.72), rgba(8,8,14,0.78));
  -webkit-backdrop-filter: blur(24px) saturate(160%);
  backdrop-filter: blur(24px) saturate(160%);
  border-right: 1px solid var(--glass-border);
}
header,
.scard,
.terminal-wrapper,
.chart-card,
.system-details-card,
.file-list,
.ip-chip,
.power-btn-group {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  backdrop-filter: blur(22px) saturate(160%);
  border: 1px solid var(--glass-border);
  box-shadow: var(--glass-hi), var(--glass-shadow);
}
header, .scard, .terminal-wrapper, .chart-card, .system-details-card, .file-list { border-radius: var(--r-lg); }
header { padding: 14px 20px; }
.scard { transition: transform 0.35s var(--ease-apple), box-shadow 0.35s var(--ease-apple); }
.scard:hover { transform: translateY(-2px) scale(1.01); box-shadow: var(--glass-hi), 0 18px 50px rgba(0,0,0,0.6), 0 0 40px rgba(139,92,246,0.14); }
.scard-val { letter-spacing: -0.02em; }

/* Marca */
.brand-icon {
  background: linear-gradient(135deg, #7c3aed, #a855f7 60%, #d8b4fe);
  border-radius: 12px;
  box-shadow: 0 6px 20px rgba(168,85,247,0.45), inset 0 1px 0 rgba(255,255,255,0.4);
  color: #fff;
}
.brand-name span {
  background: linear-gradient(90deg, #c084fc, #a855f7);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

/* Navegación */
.nav-item { border-radius: var(--r-md); transition: all 0.3s var(--ease-apple); border-left: 3px solid transparent; }
.nav-item:hover { background: rgba(168,85,247,0.10); color: var(--text-main); }
.nav-item.active {
  background: linear-gradient(120deg, rgba(168,85,247,0.22), rgba(139,92,246,0.10));
  color: #d8b4fe;
  font-weight: 600;
  border-left: 3px solid #a855f7;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
}

/* Pastilla de energía estilo control flotante */
.power-btn-group {
  border-radius: 999px;
  padding: 5px;
  gap: 4px;
}
.pbtn { border-radius: 999px; transition: all 0.3s var(--ease-apple); border: 1px solid transparent; }
.pbtn-start {
  background: linear-gradient(135deg, #8b5cf6, #6d28d9);
  box-shadow: 0 4px 18px rgba(139,92,246,0.5), inset 0 1px 0 rgba(255,255,255,0.35);
}
.pbtn-start:hover:not(:disabled) { background: linear-gradient(135deg, #a78bfa, #7c3aed); box-shadow: 0 6px 24px rgba(139,92,246,0.65), inset 0 1px 0 rgba(255,255,255,0.4); }
.pbtn-restart { background: rgba(168,85,247,0.14); border-color: rgba(168,85,247,0.35); }
.pbtn-restart:hover:not(:disabled) { background: rgba(168,85,247,0.28); }
.pbtn-stop { background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.12); }
.pbtn-stop:hover:not(:disabled) { background: rgba(255,255,255,0.12); }
.pbtn-kill { background: rgba(248,113,113,0.10); border: 1px solid rgba(248,113,113,0.35); }
.pbtn-kill:hover:not(:disabled) { background: rgba(248,113,113,0.22); }

/* Botones y campos */
.cmd-btn {
  background: linear-gradient(135deg, #a855f7, #7c3aed);
  color: #fff;
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(168,85,247,0.4), inset 0 1px 0 rgba(255,255,255,0.3);
  transition: all 0.3s var(--ease-apple);
}
.cmd-btn:hover { background: linear-gradient(135deg, #c084fc, #8b5cf6); box-shadow: 0 6px 22px rgba(168,85,247,0.55), inset 0 1px 0 rgba(255,255,255,0.35); }
.term-tool-btn { border-radius: 10px; transition: all 0.25s var(--ease-apple); }
.term-tool-btn:hover { border-color: rgba(168,85,247,0.5); color: #d8b4fe; }
.icon-btn:hover { border-color: rgba(168,85,247,0.5); color: #d8b4fe; }
.cmd-input:focus, textarea:focus, select:focus, input[type="text"]:focus {
  outline: none;
  border-color: rgba(168,85,247,0.6) !important;
  box-shadow: 0 0 0 3px rgba(168,85,247,0.22) !important;
}

/* Terminal y tablas */
.terminal-wrapper { border-radius: var(--r-lg); overflow: hidden; }
.terminal-body::-webkit-scrollbar, .file-list::-webkit-scrollbar { width: 8px; }
.terminal-body::-webkit-scrollbar-thumb, .file-list::-webkit-scrollbar-thumb { background: rgba(168,85,247,0.35); border-radius: 4px; }
.scard-bar-fill { background: linear-gradient(90deg, #7c3aed, #c084fc); }
.cmd-prompt { color: var(--accent-soft); }
.ip-chip { border-radius: var(--r-md); color: #d8b4fe; }
.ip-chip:hover { border-color: rgba(168,85,247,0.55); }
.copy-badge { background: rgba(168,85,247,0.22); color: #e9d5ff; }
.file-row { transition: background 0.25s var(--ease-apple); }
.file-row:hover { background: rgba(168,85,247,0.08); }
#toast {
  border-left: 4px solid #a855f7;
  border-radius: var(--r-md);
  background: rgba(20,14,34,0.85);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  backdrop-filter: blur(20px) saturate(160%);
  box-shadow: 0 12px 40px rgba(0,0,0,0.6), 0 0 24px rgba(139,92,246,0.15);
}
.status-pill { -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px); }

.scard-val {
  background: linear-gradient(180deg, #ffffff 20%, #c4b5fd 90%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  filter: drop-shadow(0 0 14px rgba(168,85,247,0.35));
}
.scard-bar { position: relative; }
.scard-bar-fill { position: relative; overflow: hidden; }
.scard-bar-fill::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(100deg, transparent 20%, rgba(255,255,255,0.55) 50%, transparent 80%);
  transform: translateX(-100%);
  animation: bar-shimmer 2.8s ease-in-out infinite;
}
@keyframes bar-shimmer {
  0% { transform: translateX(-100%); }
  55%, 100% { transform: translateX(100%); }
}
.chart-badge.sub {
  font-size: 11px;
  color: var(--text-muted);
  background: rgba(168,85,247,0.12);
  border: 1px solid rgba(168,85,247,0.3);
  padding: 3px 10px;
  border-radius: 999px;
  font-variant-numeric: tabular-nums;
}
canvas { filter: drop-shadow(0 0 10px rgba(139,92,246,0.25)); }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
</style>
</head>
<body>

<!-- SIDEBAR -->
<aside>
  <div class="sidebar-header">
    <div class="brand-icon"><svg class="ico" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg></div>
    <div>
      <div class="brand-name">PK<span>Hosting</span></div>
      <div class="brand-sub">Game Manager</div>
    </div>
  </div>

  <div class="server-selector">
    <div class="server-label">Servidor Activo</div>
    <div class="server-title">
      <span class="mini-dot" id="sideDot"></span>
      <span>PrankLindorf</span>
    </div>
  </div>

  <nav>
    <a class="nav-item active" onclick="switchTab('console')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg></span> Consola
    </a>
    <a class="nav-item" onclick="switchTab('metrics')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></span> Métricas del Sistema
    </a>
    <a class="nav-item" onclick="switchTab('files')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></span> Archivos
    <a class="nav-item" onclick="switchTab('backups')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><rect x="1" y="3" width="22" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><line x1="10" y1="12" x2="14" y2="12"/></svg></span> Backups
    </a>
    </a>
    <a class="nav-item" onclick="switchTab('settings')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg></span> Configuración
    </a>
  </nav>

  <div class="sidebar-footer">
    <div class="ip-chip" onclick="copyIp()">
      <span id="ipText">localhost:25566</span>
      <span class="copy-badge" id="copyBadge">Copiar</span>
    </div>
  </div>
</aside>

<!-- MAIN -->
<main>
  <header>
    <div class="header-info">
      <h1>
        <span>PrankLindorf</span>
        <span class="status-pill OFFLINE" id="statusBadge">
          <span class="status-pulse"></span>
          <span id="statusText">OFFLINE</span>
        </span>
      </h1>
      <div class="header-subtitle">NeoForge 1.21.1 · Java 21 · Puerto 25566</div>
    </div>

    <div class="header-actions">
      <div class="power-btn-group">
        <button class="pbtn pbtn-start" id="btnStart" onclick="serverAction('start')">
          <svg class="ico" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg> Iniciar
        </button>
        <button class="pbtn pbtn-restart" id="btnRestart" onclick="serverAction('restart')">
          <svg class="ico" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Reiniciar
        </button>
        <button class="pbtn pbtn-restart" id="btnReload" onclick="serverAction('reload')" title="Recarga datapacks/mods (comando reload del servidor)">
          <svg class="ico" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg> Reload
        </button>
        <button class="pbtn pbtn-stop" id="btnStop" onclick="serverAction('stop')">
          <svg class="ico" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/></svg> Detener
        </button>
        <button class="pbtn pbtn-kill" id="btnKill" onclick="serverAction('kill')">
          <svg class="ico" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Matar
        </button>
      </div>
    </div>
  </header>

  <div class="page-container">
    <!-- STATS CARDS -->
    <div class="stats-cards">
      <div class="scard">
        <div class="scard-label">
          <span>Uptime</span>
          <span class="scard-ico"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></span>
        </div>
        <div class="scard-val" id="cardUptime">-</div>
        <div class="scard-sub" id="cardUptimeSub">Servidor apagado</div>
      </div>

      <div class="scard">
        <div class="scard-label">
          <span>Uso de CPU</span>
          <span class="scard-ico"><svg class="ico" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></span>
        </div>
        <div class="scard-val" id="cardCpu">0.0%</div>
        <div class="scard-sub">16 núcleos disponibles</div>
        <div class="scard-bar"><div class="scard-bar-fill" id="barCpu"></div></div>
      </div>

      <div class="scard">
        <div class="scard-label">
          <span>Memoria RAM</span>
          <span class="scard-ico"><svg class="ico" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg></span>
        </div>
        <div class="scard-val" id="cardMem">0 MB</div>
        <div class="scard-sub" id="cardMemSub">Límite: 8.00 GB</div>
        <div class="scard-bar"><div class="scard-bar-fill" id="barMem"></div></div>
      </div>

      <div class="scard">
        <div class="scard-label">
          <span>Jugadores</span>
          <span class="scard-ico"><svg class="ico" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></span>
        </div>
        <div class="scard-val" id="cardPlayers">0 / 20</div>
        <div class="scard-sub" id="cardPlayersSub">Mundo: 6.6 GB</div>
      </div>
    </div>

    <!-- TAB 1: CONSOLA -->
    <div id="tab-console" class="tab-content active">
      <div class="terminal-wrapper">
        <div class="terminal-topbar">
          <div>Consola del Servidor (Registro en Vivo)</div>
          <div class="terminal-topbar-tools">
            <button class="term-tool-btn" onclick="toggleAutoScroll()" id="btnAutoScroll">Auto-scroll: ON</button>
            <button class="term-tool-btn" onclick="copyLogs()">Copiar registro</button>
            <button class="term-tool-btn" onclick="clearTerminal()">Limpiar</button>
          </div>
        </div>
        <div class="terminal-body" id="termBody">Conectando a la consola del servidor...</div>
        <div class="terminal-input-bar">
          <span class="cmd-prompt">&gt;</span>
          <input type="text" class="cmd-input" id="cmdInput" placeholder="Comandos MC (/say, /op, /list...) + panel: start · stop · restart · reload · kill" onkeydown="handleCmdKey(event)">
          <button class="cmd-btn" onclick="submitCmd()">Enviar</button>
        </div>
      </div>
      <div class="system-details-card" style="margin-top:12px">
        <h3 style="margin-bottom:4px; font-size:15px;">Gestión de jugadores</h3>
        <div id="onlineChips" style="display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; font-size:12px; color:var(--text-muted)">Sin jugadores en línea</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input type="text" id="playerName" maxlength="16" placeholder="Nombre del jugador" style="flex:1; min-width:160px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-family:'JetBrains Mono',monospace; font-size:12.5px">
          <input type="text" id="playerReason" maxlength="100" placeholder="Motivo (kick/ban, opcional)" style="flex:1; min-width:160px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-family:'JetBrains Mono',monospace; font-size:12.5px">
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px">
          <button class="cmd-btn" onclick="playerAction('op')">OP</button>
          <button class="term-tool-btn" onclick="playerAction('deop')">DeOP</button>
          <button class="term-tool-btn" onclick="playerAction('kick')">Kick</button>
          <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d" onclick="playerAction('ban')">Ban</button>
        </div>
      </div>
    </div>

    <!-- TAB 2: METRICAS -->
    <div id="tab-metrics" class="tab-content">
      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><svg class="ico" viewBox="0 0 24 24" style="width:16px;height:16px;vertical-align:-3px"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> Uso de CPU del Proceso</div>
            <div style="display:flex;gap:8px;align-items:center"><div class="chart-badge" id="chartCpuBadge">0.0%</div><div class="chart-badge sub" id="chartCpuPeak">pico — · prom —</div></div>
          </div>
          <canvas id="canvasCpu"></canvas>
        </div>

        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><svg class="ico" viewBox="0 0 24 24" style="width:16px;height:16px;vertical-align:-3px"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg> Memoria RAM Asignada</div>
            <div style="display:flex;gap:8px;align-items:center"><div class="chart-badge" id="chartMemBadge">0 MB</div><div class="chart-badge sub" id="chartMemPeak">pico — · prom —</div></div>
          </div>
          <canvas id="canvasMem"></canvas>
        </div>
      </div>

      <div class="system-details-card">
        <h3 style="margin-bottom:14px; font-size:16px;">Información del Servidor y Entorno</h3>
        <table class="details-table">
          <tr><td>Estado del Proceso</td><td id="detStatus">OFFLINE</td></tr>
          <tr><td>Identificador de Proceso (PID)</td><td id="detPid">-</td></tr>
          <tr><td>Carga del Sistema (1m, 5m, 15m)</td><td id="detLoad">-</td></tr>
          <tr><td>Tamaño del Mundo (PrankLindorf)</td><td id="detWorld">-</td></tr>
          <tr><td>Puerto Minecraft</td><td>25566 (TCP / UDP)</td></tr>
          <tr><td>Entorno Java</td><td>OpenJDK 21 (64-Bit Server VM)</td></tr>
          <tr><td>Directorio</td><td>~/PrankLindorf-NeoForge</td></tr>
        </table>
      </div>
    </div>

    <!-- TAB 3: ARCHIVOS -->
    <div id="tab-files" class="tab-content">
      <div class="fs-toolbar">
        <button class="cmd-btn" onclick="fsCreate('file')"><svg class="ico" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/></svg> Nuevo archivo</button>
        <button class="cmd-btn" onclick="fsCreate('dir')"><svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg> Nueva carpeta</button>
        <label class="cmd-btn" style="cursor:pointer"><svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Subir<input type="file" id="fsUploadInput" style="display:none" multiple onchange="fsUpload()"></label>
        <span style="flex:1"></span>
        <span class="fs-crumb"><a onclick="fsNavUp()">↑ Subir</a>&nbsp;<span id="fsBreadcrumb">/</span></span>
        <button class="term-tool-btn" onclick="loadFiles()">Recargar</button>
      </div>
      <div class="fs-toolbar">
        <label style="font-size:12px; color:var(--text-muted)">Ordenar:</label>
        <select id="fsSortKey" onchange="fsSortChanged()" style="background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:7px 10px; color:#e2e8f0; font-size:12px">
          <option value="name">Nombre</option>
          <option value="mtime">Fecha de modificación</option>
          <option value="size">Tamaño</option>
        </select>
        <button class="term-tool-btn" id="fsSortDir" onclick="fsToggleDir()" title="Dirección">↓</button>
        <label style="font-size:12px; color:var(--text-muted); display:inline-flex; align-items:center; gap:6px; cursor:pointer"><input type="checkbox" id="fsGroup" checked onchange="loadFiles()"> Carpetas primero</label>
      </div>
      <div class="file-list" id="filesContainer">
        <div class="file-row"><span>Cargando archivos del servidor...</span></div>
      </div>
      <div id="fsEditModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:200; align-items:center; justify-content:center; padding:20px">
        <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:12px; width:min(860px,100%); max-height:90vh; display:flex; flex-direction:column; overflow:hidden">
          <div style="display:flex; align-items:center; gap:10px; padding:12px 16px; border-bottom:1px solid var(--border-color)">
            <span style="font-family:'JetBrains Mono',monospace; font-size:12.5px; color:#93c5fd; flex:1; overflow:hidden; text-overflow:ellipsis" id="fsEditPath">—</span>
            <button class="term-tool-btn" onclick="fsDownloadCurrent()">Descargar</button>
            <button class="term-tool-btn" onclick="fsCloseEdit()">Cerrar ✕</button>
          </div>
          <textarea id="fsEditor" style="flex:1; min-height:320px; background:var(--bg-terminal); color:#cbd5e1; border:0; padding:14px; font-family:'JetBrains Mono',monospace; font-size:12.5px; resize:vertical" placeholder="Cargando..."></textarea>
          <div style="display:flex; gap:10px; padding:12px 16px; border-top:1px solid var(--border-color)">
            <button class="cmd-btn" onclick="fsSaveEdit()">Guardar cambios</button>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB: BACKUPS -->
    <div id="tab-backups" class="tab-content">
      <div class="system-details-card" style="margin-bottom:12px">
        <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center">
          <button class="cmd-btn" onclick="backupNow()"><svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Backup ahora</button>
          <button class="term-tool-btn" onclick="loadBackups()">Recargar</button>
          <span id="bkStatus" style="font-size:12.5px; color:var(--text-muted)">Cargando estado...</span>
        </div>
        <div id="bkJob" style="font-size:12.5px; color:#c084fc; margin-top:8px; min-height:18px"></div>
        <div id="bkProgWrap" style="display:none; margin-top:8px">
          <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--text-muted); margin-bottom:4px">
            <span id="bkStage">…</span><span id="bkPct">0%</span>
          </div>
          <div class="scard-bar" style="height:8px"><div class="scard-bar-fill" id="bkBar" style="width:0%"></div></div>
        </div>
      </div>
      <div class="file-list" id="bkList">
        <div class="file-row"><span>Cargando backups...</span></div>
      </div>
      <div style="font-size:11.5px; color:var(--text-dim); margin-top:8px">Se conservan los últimos 7 días; el último de cada mes se guarda para siempre (etiqueta MENSUAL).</div>
    </div>

    <!-- TAB 4: CONFIGURACION -->
    <div id="tab-settings" class="tab-content">
      <div class="system-details-card" style="margin-bottom:12px">
        <h3 style="margin-bottom:8px">IP pública (playit.gg)</h3>
        <div style="font-size:12.5px; color:var(--text-muted); margin-bottom:8px" id="playitStatus">Detectando túnel playit...</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input id="publicIpInput" placeholder="ej: tu-servidor.playit.gg:12345" style="flex:1; min-width:220px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; color:#e2e8f0; font-family:'JetBrains Mono',monospace; font-size:12.5px">
          <button class="cmd-btn" onclick="savePublicIp()">Guardar IP</button>
        </div>
      </div>
      <div class="system-details-card">
        <h3 style="margin-bottom:12px">Configuración del Servidor (server.properties)</h3>
        <textarea id="propsText" style="width:100%; height:320px; background:var(--bg-terminal); color:#cbd5e1; border:1px solid var(--border-color); border-radius:8px; padding:12px; font-family:'JetBrains Mono', monospace; font-size:12px"></textarea>
        <div style="margin-top:10px; display:flex; gap:10px">
          <button class="cmd-btn" onclick="saveProps()">Guardar server.properties</button>
          <button class="term-tool-btn" onclick="loadProps()">Recargar</button>
        </div>
      </div>
    </div>

  </div>
</main>

<div id="toast"></div>

<script>
let autoScroll = true;
let historyBuffer = [];
let cmdHistory = [];
let cmdIndex = -1;

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}

let currentPublicIp = 'localhost:25566';
function copyIp() {
  navigator.clipboard.writeText(currentPublicIp);
  document.getElementById('copyBadge').textContent = '¡Copiado!';
  setTimeout(() => document.getElementById('copyBadge').textContent = 'Copiar', 1800);
  showToast('Dirección del servidor copiada: ' + currentPublicIp);
}
async function refreshPublicIp() {
  try {
    const res = await fetch('/api/tunnels');
    const d = await res.json();
    if (d.public_ip) {
      currentPublicIp = d.public_ip;
      document.getElementById('ipText').textContent = d.public_ip;
    }
    const ps = document.getElementById('playitStatus');
    if (ps && d.playit) {
      const p = d.playit;
      ps.textContent = p.installed
        ? (p.service_active ? 'Agente playit: servicio activo. ' + (p.note || 'Pega arriba la dirección pública del túnel TCP (puerto 25566).') : 'Agente playit instalado pero servicio inactivo.')
        : 'playit no detectado en el sistema.';
    }
    const inp = document.getElementById('publicIpInput');
    if (inp && d.public_ip && !inp.value) inp.value = d.public_ip;
  } catch (e) {}
}
async function savePublicIp() {
  const v = document.getElementById('publicIpInput').value.trim();
  try {
    const res = await fetch('/api/public-ip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ public_ip: v }) });
    const data = await res.json();
    showToast(data.msg || 'OK');
    refreshPublicIp();
  } catch (e) { showToast('Error al guardar IP'); }
}

function switchTab(name) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  
  const targetNav = Array.from(document.querySelectorAll('.nav-item')).find(el => el.textContent.toLowerCase().includes(name === 'console' ? 'consola' : name === 'metrics' ? 'métrica' : name === 'files' ? 'archivo' : name === 'backups' ? 'backup' : 'config'));
  if (targetNav) targetNav.classList.add('active');

  const tab = document.getElementById('tab-' + name);
  if (tab) tab.classList.add('active');

  if (name === 'metrics') renderCharts();
  if (name === 'files') loadFiles();
  if (name === 'backups') loadBackups();
  if (name === 'settings') { loadProps(); refreshPublicIp(); }
}

async function serverAction(act) {
  showToast('Enviando acción: ' + act.toUpperCase() + '...');
  try {
    const res = await fetch('/api/' + act, { method: 'POST' });
    const data = await res.json();
    showToast(data.msg || 'OK');
    setTimeout(refreshStats, 1200);
  } catch (e) {
    showToast('Error de conexión: ' + e);
  }
}

async function submitCmd() {
  const input = document.getElementById('cmdInput');
  const val = input.value.trim();
  if (!val) return;
  cmdHistory.push(val);
  cmdIndex = cmdHistory.length;
  input.value = '';
  try {
    const res = await fetch('/api/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: val })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'Comando enviado' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar comando');
  }
}

function handleCmdKey(e) {
  if (e.key === 'Enter') {
    submitCmd();
  } else if (e.key === 'ArrowUp') {
    if (cmdIndex > 0) {
      cmdIndex--;
      document.getElementById('cmdInput').value = cmdHistory[cmdIndex] || '';
    }
  } else if (e.key === 'ArrowDown') {
    if (cmdIndex < cmdHistory.length - 1) {
      cmdIndex++;
      document.getElementById('cmdInput').value = cmdHistory[cmdIndex] || '';
    } else {
      cmdIndex = cmdHistory.length;
      document.getElementById('cmdInput').value = '';
    }
  }
}

async function playerAction(act) {
  const nameEl = document.getElementById('playerName');
  const name = nameEl.value.trim();
  if (!name) { showToast('Escribe el nombre del jugador'); nameEl.focus(); return; }
  const reason = document.getElementById('playerReason').value.trim();
  if ((act === 'kick' || act === 'ban') && !confirm(`¿${act === 'ban' ? 'BANEAR' : 'Expulsar'} a "${name}"?${reason ? '\\nMotivo: ' + reason : ''}`)) return;
  try {
    const res = await fetch('/api/player', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: act, name, reason })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'OK' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar acción');
  }
}

function clearTerminal() {
  document.getElementById('termBody').innerHTML = '';
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  document.getElementById('btnAutoScroll').textContent = 'Auto-scroll: ' + (autoScroll ? 'ON' : 'OFF');
}

function copyLogs() {
  const t = document.getElementById('termBody').innerText;
  navigator.clipboard.writeText(t);
  showToast('Registro de consola copiado al portapapeles');
}

function highlightLogLine(line) {
  const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (esc.includes('Done (')) return `<span class="log-done">${esc}</span>`;
  if (esc.includes('ERROR') || esc.includes('FATAL') || esc.includes('Exception')) return `<span class="log-err">${esc}</span>`;
  if (esc.includes('WARN')) return `<span class="log-warn">${esc}</span>`;
  return `<span class="log-info">${esc}</span>`;
}

async function refreshConsole() {
  try {
    const res = await fetch('/api/console');
    const data = await res.json();
    const body = document.getElementById('termBody');
    if (data.lines && data.lines.length) {
      body.innerHTML = data.lines.map(highlightLogLine).join('<br>');
      if (autoScroll) body.scrollTop = body.scrollHeight;
    }
  } catch (e) {}
}

async function refreshStats() {
  try {
    const res = await fetch('/api/stats');
    const d = await res.json();
    
    // Status Badge
    const badge = document.getElementById('statusBadge');
    const stText = document.getElementById('statusText');
    const sideDot = document.getElementById('sideDot');
    badge.className = 'status-pill ' + d.status;
    stText.textContent = d.status;
    
    const colorMap = {
      'RUNNING': 'var(--color-online)',
      'STARTING': 'var(--color-starting)',
      'STOPPING': 'var(--color-stopping)',
      'OFFLINE': 'var(--color-offline)'
    };
    sideDot.style.background = colorMap[d.status] || 'var(--color-offline)';

    // Buttons Disable
    document.getElementById('btnStart').disabled = (d.status !== 'OFFLINE');
    document.getElementById('btnStop').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnRestart').disabled = (d.status === 'OFFLINE');
    document.getElementById('btnReload').disabled = (d.status !== 'RUNNING');
    document.getElementById('btnKill').disabled = (d.status === 'OFFLINE');
    if (d.public_ip) {
      currentPublicIp = d.public_ip;
      const ipEl = document.getElementById('ipText');
      if (ipEl) ipEl.textContent = d.public_ip;
    }

    // Cards
    document.getElementById('cardUptime').textContent = d.uptime;
    document.getElementById('cardUptimeSub').textContent = d.status === 'RUNNING' ? 'En línea y respondiendo' : (d.status === 'STARTING' ? 'Cargando mods y mundo...' : 'Servidor detenido');
    
    // Color por nivel de uso: verde < umbral1, ámbar < umbral2, rojo arriba
    const cpuPct = Math.max(0, d.cpu || 0);
    const memPct = d.max_mem_gb ? (d.mem_gb / d.max_mem_gb) * 100 : 0;
    const cpuColor = usageColor(cpuPct, 50, 80);
    const memColor = usageColor(memPct, 60, 85);
    cpuStrokeColor = cpuColor; memStrokeColor = memColor;

    const cardCpu = document.getElementById('cardCpu');
    cardCpu.textContent = d.cpu.toFixed(1) + '%';
    paintUsage(cardCpu, document.getElementById('barCpu'), Math.min(100, cpuPct), cpuColor);

    const cardMem = document.getElementById('cardMem');
    cardMem.textContent = (d.mem_mb >= 1024 ? d.mem_gb + ' GB' : d.mem_mb + ' MB');
    paintUsage(cardMem, document.getElementById('barMem'), Math.min(100, memPct), memColor);
    document.getElementById('cardMemSub').textContent = `Límite: ${d.max_mem_gb} GB (${d.mem_mb} MB)`;

    document.getElementById('cardPlayers').textContent = `${d.online_players} / ${d.max_players}`;
    document.getElementById('cardPlayersSub').textContent = d.player_names && d.player_names.length ? d.player_names.join(', ') : `Mundo: ${d.world_gb} GB`;
    const chips = document.getElementById('onlineChips');
    if (chips) {
      if (d.player_names && d.player_names.length) {
        chips.innerHTML = d.player_names.map(n =>
          `<button class="term-tool-btn" title="Usar este jugador" onclick="document.getElementById('playerName').value='${n.replace(/'/g, "")}'">${n}</button>`
        ).join('');
      } else {
        chips.textContent = d.status === 'RUNNING' ? 'Sin jugadores en línea' : 'Servidor apagado';
      }
    }

    // Metrics tab badges
    paintBadge(document.getElementById('chartCpuBadge'), d.cpu.toFixed(1) + '%', cpuColor);
    paintBadge(document.getElementById('chartMemBadge'), d.mem_gb + ' GB', memColor);

    document.getElementById('detStatus').textContent = d.status;
    document.getElementById('detPid').textContent = d.pid || '-';
    document.getElementById('detLoad').textContent = d.system_load;
    document.getElementById('detWorld').textContent = d.world_gb + ' GB';

    historyBuffer = d.history || [];
    if (document.getElementById('tab-metrics').classList.contains('active')) {
      renderCharts();
    }
  } catch (e) {}
}

function drawSmoothChart(canvasId, values, maxVal, colorStroke, colorFill, unit) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth;
  const h = c.clientHeight;
  c.width = w * dpr;
  c.height = h * dpr;
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, w, h);
  if (!values || values.length < 2) {
    ctx.fillStyle = '#64748b';
    ctx.font = '12px Inter';
    ctx.fillText('Esperando datos de telemetría...', 20, h / 2);
    return;
  }

  // Gridlines
  ctx.strokeStyle = 'rgba(255,255,255,0.05)';
  ctx.lineWidth = 1;
  for (let y = 0; y <= 4; y++) {
    const yPos = (h / 4) * y;
    ctx.beginPath();
    ctx.moveTo(0, yPos);
    ctx.lineTo(w, yPos);
    ctx.stroke();
  }

  const step = w / (values.length - 1);
  ctx.beginPath();
  ctx.moveTo(0, h - (values[0] / maxVal) * h);

  for (let i = 1; i < values.length; i++) {
    const prevX = (i - 1) * step;
    const prevY = h - (values[i - 1] / maxVal) * h;
    const currX = i * step;
    const currY = h - (values[i] / maxVal) * h;
    const midX = (prevX + currX) / 2;
    ctx.bezierCurveTo(midX, prevY, midX, currY, currX, currY);
  }

  // Stroke con resplandor
  ctx.save();
  ctx.strokeStyle = colorStroke;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.shadowColor = colorStroke;
  ctx.shadowBlur = 12;
  ctx.stroke();
  ctx.restore();

  // Fill
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, colorFill);
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad;
  ctx.fill();
}

function chartPeakAvg(vals, digits) {
  if (!vals.length) return 'pico — · prom —';
  const mx = Math.max(...vals);
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  return `pico ${mx.toFixed(digits)} · prom ${avg.toFixed(digits)}`;
}

let cpuStrokeColor = '#a855f7', memStrokeColor = '#7c3aed';
function usageColor(pct, warnAt, critAt) {
  if (pct >= critAt) return '#f87171';
  if (pct >= warnAt) return '#fbbf24';
  return '#34d399';
}
function paintUsage(valEl, barEl, pct, color) {
  // Solo el TEXTO de color: backgroundImage no resetea background-clip:text
  // (el shorthand `background` sí lo hacía y pintaba toda la caja como barra).
  valEl.style.backgroundImage = `linear-gradient(180deg, #ffffff 15%, ${color} 95%)`;
  valEl.style.webkitBackgroundClip = 'text';
  valEl.style.backgroundClip = 'text';
  valEl.style.filter = `drop-shadow(0 0 14px ${color}59)`;
  barEl.style.width = pct + '%';
  barEl.style.background = `linear-gradient(90deg, ${color}99, ${color})`;
}
function paintBadge(el, text, color) {
  if (!el) return;
  el.textContent = text;
  el.style.color = color;
  el.style.borderColor = color + '66';
}
function renderCharts() {
  if (!historyBuffer || historyBuffer.length < 2) return;
  const cpuVals = historyBuffer.map(h => h.cpu || 0);
  const memVals = historyBuffer.map(h => (h.mem || 0) / 1024.0);
  drawSmoothChart('canvasCpu', cpuVals, 100.0, cpuStrokeColor, cpuStrokeColor + '40', '%');
  drawSmoothChart('canvasMem', memVals, 8.0, memStrokeColor, memStrokeColor + '4D', 'GB');
  const cp = document.getElementById('chartCpuPeak');
  if (cp) cp.textContent = chartPeakAvg(cpuVals, 1) + '%';
  const mp = document.getElementById('chartMemPeak');
  if (mp) mp.textContent = chartPeakAvg(memVals, 2) + ' GB';
}

let fsPath = '';
let fsSort = { key: 'name', dir: 1, group: true };
function fsJoin(base, name) { return (base ? base + '/' : '') + name; }
function escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
const SVG_FOLDER = '<svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
const SVG_FILE = '<svg class="ico" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>';
const SVG_DL = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const SVG_PENCIL = '<svg class="ico" viewBox="0 0 24 24"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>';
const SVG_EDIT = '<svg class="ico" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const SVG_TRASH = '<svg class="ico" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
function fsSortChanged() { fsSort.key = document.getElementById('fsSortKey').value; loadFiles(false); }
function fsToggleDir() { fsSort.dir *= -1; document.getElementById('fsSortDir').textContent = fsSort.dir === 1 ? '↓' : '↑'; loadFiles(false); }
function fsApplySort(files) {
  fsSort.group = document.getElementById('fsGroup').checked;
  const k = fsSort.key, d = fsSort.dir;
  return files.slice().sort((a, b) => {
    if (fsSort.group && a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let va, vb;
    if (k === 'mtime') { va = a.mtime || 0; vb = b.mtime || 0; }
    else if (k === 'size') { va = a.size_b || 0; vb = b.size_b || 0; }
    else { va = (a.name || '').toLowerCase(); vb = (b.name || '').toLowerCase(); }
    if (va < vb) return -1 * d;
    if (va > vb) return 1 * d;
    return 0;
  });
}
function fsRenderCrumb(cur) {
  const el = document.getElementById('fsBreadcrumb');
  const parts = (cur || '').split('/').filter(Boolean);
  let html = `<a data-p="" onclick="fsGoto(this.dataset.p);return false" href="#">/</a>`;
  let acc = '';
  for (const seg of parts) {
    acc = acc ? acc + '/' + seg : seg;
    html += ` <span style="color:var(--text-dim)">›</span> <a data-p="${escHtml(acc)}" onclick="fsGoto(this.dataset.p);return false" href="#">${escHtml(seg)}</a>`;
  }
  el.innerHTML = html;
}
function fsGoto(p) { fsNav(p, true); }
async function loadFiles(push = true) {
  const container = document.getElementById('filesContainer');
  try {
    const res = await fetch('/api/files?path=' + encodeURIComponent(fsPath));
    const data = await res.json();
    const files = fsApplySort(Array.isArray(data) ? data : (data.entries || []));
    const cur = (data && typeof data === 'object' && data.path !== undefined) ? data.path : fsPath;
    fsRenderCrumb(cur);
    if (!files.length) { container.innerHTML = '<div class="file-row"><span class="file-name">Carpeta vacía</span></div>'; }
    else {
      container.innerHTML = files.map(f => {
        const full = escHtml(fsJoin(fsPath, f.name));
        const link = f.is_dir
          ? `<a href="#" data-path="${full}" onclick="fsEnter(this.dataset.path);return false" style="color:#93c5fd">${escHtml(f.name)}/</a>`
          : escHtml(f.name);
        const editable = !f.is_dir && (f.size_b || 0) <= 2 * 1024 * 1024;
        const dt = f.mtime ? new Date(f.mtime * 1000).toLocaleString() : '';
        return `<div class="file-row">
          <span class="file-icon ${f.is_dir ? 'is-folder' : 'is-file'}" title="${f.is_dir ? 'Carpeta' : 'Archivo'}">${f.is_dir ? SVG_FOLDER : SVG_FILE}</span>
          <span class="file-name">${link}</span>
          <span class="file-size" title="${escHtml(dt)}">${escHtml(f.size || '')}</span>
          <span class="file-actions">
            ${f.is_dir ? '' : `<button class="icon-btn" title="Descargar" data-path="${full}" onclick="fsDownload(this.dataset.path)">${SVG_DL}</button>`}
            ${editable ? `<button class="icon-btn" title="Editar" data-path="${full}" onclick="fsEdit(this.dataset.path)">${SVG_EDIT}</button>` : ''}
            <button class="icon-btn" title="Renombrar" data-path="${full}" onclick="fsRename(this.dataset.path)">${SVG_PENCIL}</button>
            <button class="icon-btn danger" title="Eliminar" data-path="${full}" onclick="fsDelete(this.dataset.path)">${SVG_TRASH}</button>
          </span>
        </div>`;
      }).join('');
    }
  } catch (e) {
    container.innerHTML = '<div class="file-row">Error al cargar archivos</div>';
  }
  if (push) { try { history.pushState({ fsPath }, '', '#archivos/' + fsPath); } catch (err) {} }
}
function fsNav(p, push = true) { fsPath = p; loadFiles(push); }
function fsEnter(p) { fsNav(p, true); }
function fsNavUp() { fsNav(fsPath.split('/').slice(0, -1).join(''), true); }
window.addEventListener('popstate', (e) => {
  const p = (e.state && e.state.fsPath !== undefined) ? e.state.fsPath : '';
  fsPath = p;
  loadFiles(false);
});
try { history.replaceState({ fsPath: '' }, '', location.pathname); } catch (err) {}
async function fsEdit(p) {
  document.getElementById('fsEditPath').textContent = p;
  document.getElementById('fsEditor').value = 'Cargando...';
  document.getElementById('fsEditModal').style.display = 'flex';
  const r = await fsPost('read', { path: p });
  document.getElementById('fsEditor').value = r.ok ? (r.content || '') : ('Error: ' + (r.msg || 'no se pudo leer'));
}
function fsCloseEdit() { document.getElementById('fsEditModal').style.display = 'none'; }
async function fsSaveEdit() {
  const p = document.getElementById('fsEditPath').textContent;
  const r = await fsPost('write', { path: p, content: document.getElementById('fsEditor').value });
  showToast(r.msg || 'OK'); if (r.ok) { fsCloseEdit(); loadFiles(false); }
}
function fsDownloadCurrent() {
  const p = document.getElementById('fsEditPath').textContent;
  if (p && p !== '—') fsDownload(p);
}
async function fsPost(action, extra) {
  const res = await fetch('/api/fs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, ...extra }) });
  return res.json();
}
async function fsCreate(kind) {
  const v = (prompt(kind === 'dir' ? 'Nombre de la nueva carpeta:' : 'Nombre del nuevo archivo:') || '').trim();
  if (!v) return;
  const clean = v.replace(new RegExp('^/+'), '');
  const r = await fsPost(kind === 'dir' ? 'mkdir' : 'touch', { path: fsJoin(fsPath, clean) });
  showToast(r.msg || 'OK'); loadFiles();
}
async function fsUpload() {
  const inp = document.getElementById('fsUploadInput');
  if (!inp.files.length) return;
  const fd = new FormData();
  for (const f of inp.files) fd.append('files', f, f.name);
  showToast('Subiendo ' + inp.files.length + ' archivo(s)...');
  try {
    const res = await fetch('/api/upload?path=' + encodeURIComponent(fsPath), { method: 'POST', body: fd });
    const r = await res.json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al subir'); }
  inp.value = '';
  loadFiles();
}
function fsDownload(p) { window.open('/api/download?path=' + encodeURIComponent(p), '_blank'); }
async function fsRename(p) {
  const dest = (prompt('Renombrar / mover a (ruta relativa):', p) || '').trim();
  if (!dest || dest === p) return;
  const r = await fsPost('rename', { path: p, dest });
  showToast(r.msg || 'OK'); loadFiles();
}
async function fsDelete(p) {
  if (!confirm('¿Eliminar "' + p + '"?')) return;
  const r = await fsPost('delete', { path: p });
  showToast(r.msg || 'OK'); loadFiles();
}

const BK_SVG_DL = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const BK_SVG_BACK = '<svg class="ico" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>';
const BK_SVG_TRASH = '<svg class="ico" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
async function loadBackups() {
  try {
    const res = await fetch('/api/backups');
    const d = await res.json();
    const st = document.getElementById('bkStatus');
    const w = d.writable ? '✔ destino escribible' : '✘ destino NO escribible' + (d.writable_msg ? ': ' + d.writable_msg : '');
    st.textContent = (d.enabled ? `Automático diario ${d.time} · ` : 'Automático desactivado · ')
      + `${d.dir} · libre ${d.free_gb} GB · ret ${d.retention_days}d${d.keep_monthly ? ' + mensual' : ''} · ${w}`;
    const job = document.getElementById('bkJob');
    job.textContent = (d.job && d.job.running) ? ('⏳ ' + (d.job.msg || 'trabajando...')) : ((d.job && d.job.msg) ? d.job.msg : '');
    const wrap = document.getElementById('bkProgWrap');
    const showBar = !!(d.job && (d.job.running || (d.job.pct > 0 && d.job.stage && d.job.stage !== '')));
    wrap.style.display = showBar ? 'block' : 'none';
    if (showBar) {
      const pct = Math.max(0, Math.min(100, d.job.pct || 0));
      document.getElementById('bkBar').style.width = pct + '%';
      document.getElementById('bkPct').textContent = pct + '%';
      document.getElementById('bkStage').textContent = d.job.stage || d.job.job || 'trabajando...';
      if (d.job.running) { clearTimeout(window._bkT); window._bkT = setTimeout(loadBackups, 1500); }
    }
    const box = document.getElementById('bkList');
    const items = (d.items || []).slice().reverse();
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin backups todavía — pulsa «Backup ahora»</span></div>'; return; }
    box.innerHTML = items.map(f => `
      <div class="file-row">
        <span class="file-name">${f.name}${f.monthly ? ' <span class="chart-badge sub">MENSUAL</span>' : ''}<br><span style="font-size:11px;color:var(--text-dim)">${f.date} · ${f.size}</span></span>
        <span class="file-actions">
          <button class="icon-btn" title="Restaurar (detiene el server)" onclick="restoreBackup('${f.name}')">${BK_SVG_BACK}</button>
          <button class="icon-btn danger" title="Eliminar" onclick="deleteBackup('${f.name}')">${BK_SVG_TRASH}</button>
        </span>
      </div>`).join('');
  } catch (e) {
    document.getElementById('bkList').innerHTML = '<div class="file-row">Error al cargar backups</div>';
  }
}
async function backupNow() {
  showToast('Iniciando backup...');
  try {
    const r = await (await fetch('/api/backup-now', { method: 'POST' })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al iniciar backup'); }
  setTimeout(loadBackups, 1500);
}
async function restoreBackup(name) {
  if (!confirm(`¿RESTAURAR "${name}"?\n\nDetiene el servidor, guarda un pre-backup de seguridad y sobrescribe el mundo actual.`)) return;
  try {
    const r = await (await fetch('/api/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al restaurar'); }
  setTimeout(loadBackups, 2000);
}
async function deleteBackup(name) {
  if (!confirm(`¿Eliminar el backup "${name}"?`)) return;
  try {
    const r = await (await fetch('/api/backup-delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al eliminar'); }
  loadBackups();
}
setInterval(() => { const t = document.getElementById('tab-backups'); if (t && t.classList.contains('active')) loadBackups(); }, 5000);

async function loadProps() {
  try {
    const res = await fetch('/api/file?path=server.properties');
    const data = await res.json();
    document.getElementById('propsText').value = data.content || '';
  } catch (e) {}
}

async function saveProps() {
  const content = document.getElementById('propsText').value;
  try {
    const res = await fetch('/api/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: 'server.properties', content: content })
    });
    const data = await res.json();
    showToast(data.msg || 'Guardado correctamente');
  } catch (e) {
    showToast('Error al guardar');
  }
}

setInterval(refreshStats, 2000);
setInterval(refreshConsole, 2500);
refreshStats();
refreshConsole();
</script>
</body>
</html>
"""

MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB


def build_page():
    """HTML con los valores de config.json (nombre, subtítulo, puertos, dir)."""
    p = HTML_PAGE
    local_ip = f"localhost:{MC_PORT}"
    repl = {
        "PrankLindorf": SERVER_NAME,
        "NeoForge 1.21.1 · Java 21 · Puerto 25566":
            f"{CFG['server_subtitle']} · Puerto {MC_PORT}",
        "localhost:25566": local_ip,
        "25566 (TCP / UDP)": f"{MC_PORT} (TCP / UDP)",
        "(puerto 25566)": f"(puerto {MC_PORT})",
        "~/PrankLindorf-NeoForge": CFG["server_dir"],
        "16 núcleos disponibles": f"{os.cpu_count() or '?'} núcleos disponibles",
        "PKHosting Panel — " + SERVER_NAME: f"PKHosting Panel — {SERVER_NAME}",
    }
    for old, new in repl.items():
        p = p.replace(old, new)
    return p


def handle_upload(handler, dest_rel):
    dest_dir = safe_fs_path(dest_rel or "")
    if dest_dir is None:
        return handler.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype or "boundary=" not in ctype:
        return handler.send_json({"ok": False, "msg": "Se esperaba multipart/form-data"}, code=400)
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except Exception:
        length = 0
    if length <= 0 or length > MAX_UPLOAD + 1024 * 1024:
        return handler.send_json({"ok": False, "msg": "Tamaño inválido o mayor a 300 MB"}, code=400)
    try:
        body = handler.rfile.read(length)
    except Exception as e:
        return handler.send_json({"ok": False, "msg": f"Error leyendo subida: {e}"}, code=500)
    bnd = ctype.split("boundary=")[1].strip().strip('"').encode("ascii", "ignore")
    delim = b"--" + bnd
    saved = []
    try:
        os.makedirs(dest_dir, exist_ok=True)
        for part in body.split(delim):
            if b'filename="' not in part:
                continue
            head, sep, content = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            m = re.search(r'filename="([^"]*)', head.decode("utf-8", "replace"))
            if not m:
                continue
            fname = os.path.basename(m.group(1).strip())
            if not fname or fname in (".", ".."):
                continue
            if content.endswith(b"\r\n"):
                content = content[:-2]
            target = safe_fs_path(((dest_rel or "").strip("/") + "/" + fname).strip("/"))
            if target is None:
                continue
            with open(target, "wb") as f:
                f.write(content)
            saved.append(fname)
    except Exception as e:
        return handler.send_json({"ok": False, "msg": f"Error guardando: {e}"}, code=500)
    if not saved:
        return handler.send_json({"ok": False, "msg": "No se recibió ningún archivo"}, code=400)
    return handler.send_json({"ok": True, "msg": f"Subido(s): {', '.join(saved)}"})


# ══════════════════════════════════════════════════════════════════
# SERVIDOR HTTP API
# ══════════════════════════════════════════════════════════════════

class BisectPanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        try:
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            try:
                body = build_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/stats":
            return self.send_json(get_server_stats_data())

        if u.path == "/api/console":
            return self.send_json({"lines": read_console_lines(250)})

        if u.path == "/api/files":
            qs = parse_qs(u.query)
            rel = qs.get("path", [""])[0]
            data, err = fs_list(rel)
            if err:
                return self.send_json({"error": err}, code=400)
            # Compat: la UI vieja espera lista directa; la nueva usa {path, entries}
            if "path" not in qs:
                return self.send_json(data["entries"])
            return self.send_json(data)

        if u.path == "/api/file":
            qs = parse_qs(u.query)
            fname = qs.get("path", [""])[0]
            target = safe_fs_path(fname)
            if target is None or not os.path.exists(target) or os.path.isdir(target):
                return self.send_json({"error": "No encontrado"}, code=404)
            try:
                if os.path.getsize(target) > MAX_FILE_READ:
                    return self.send_json({"error": "Archivo muy grande (>2MB), usa Descargar"}, code=400)
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    return self.send_json({"content": f.read()})
            except Exception as e:
                return self.send_json({"error": str(e)}, code=500)

        if u.path == "/api/download":
            qs = parse_qs(u.query)
            fname = qs.get("path", [""])[0]
            target = safe_fs_path(fname)
            if target is None or not os.path.isfile(target):
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(target, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{os.path.basename(target)}"')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        if u.path == "/api/backups":
            return self.send_json(backups_status())

        if u.path == "/api/public-ip":
            return self.send_json({"public_ip": get_public_ip()})

        if u.path == "/api/tunnels":
            return self.send_json({"public_ip": get_public_ip(), "playit": get_playit_status()})

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/upload":
            qs = parse_qs(u.query)
            return handle_upload(self, qs.get("path", [""])[0])
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if u.path == "/api/start":
            ok, msg = start_server_action()
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/stop":
            ok, msg = stop_server_action(force=False)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/restart":
            ok, msg = restart_server_action()
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/reload":
            pid = find_running_mc_pid()
            if pid is None:
                return self.send_json({"ok": False, "msg": "El servidor está apagado (usa 'start')"})
            ok, msg = send_command_action("reload")
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/kill":
            ok, msg = stop_server_action(force=True)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/cmd":
            cmd = payload.get("cmd", "")
            ok, msg = send_command_action(cmd)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/player":
            ok, msg = player_action(payload.get("action", ""),
                                    payload.get("name", ""),
                                    payload.get("reason", ""))
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/file":
            fname = payload.get("path", "")
            target = safe_fs_path(fname)
            if target is None:
                return self.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(payload.get("content", ""))
                return self.send_json({"ok": True, "msg": "Archivo guardado con éxito"})
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)

        if u.path == "/api/fs":
            action = payload.get("action", "")
            rel = payload.get("path", "")
            try:
                if action == "list":
                    data, err = fs_list(rel)
                    if err:
                        return self.send_json({"ok": False, "msg": err}, code=400)
                    return self.send_json({"ok": True, **data})
                if action == "read":
                    target = safe_fs_path(rel)
                    if target is None or not os.path.isfile(target):
                        return self.send_json({"ok": False, "msg": "No encontrado"}, code=404)
                    if os.path.getsize(target) > MAX_FILE_READ:
                        return self.send_json({"ok": False, "msg": "Archivo muy grande (>2MB), usa Descargar"}, code=400)
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        return self.send_json({"ok": True, "content": f.read()})
                if action == "write":
                    target = safe_fs_path(rel)
                    if target is None:
                        return self.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(payload.get("content", ""))
                    return self.send_json({"ok": True, "msg": "Guardado"})
                if action == "mkdir":
                    target = safe_fs_path(rel)
                    if target is None:
                        return self.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
                    os.makedirs(target, exist_ok=True)
                    return self.send_json({"ok": True, "msg": "Carpeta creada"})
                if action == "touch":
                    target = safe_fs_path(rel)
                    if target is None:
                        return self.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if not os.path.exists(target):
                        open(target, "w").close()
                    return self.send_json({"ok": True, "msg": "Archivo creado"})
                if action == "rename":
                    src = safe_fs_path(rel)
                    dst = safe_fs_path(payload.get("dest", ""))
                    if src is None or dst is None:
                        return self.send_json({"ok": False, "msg": "Ruta inválida"}, code=400)
                    if not os.path.exists(src):
                        return self.send_json({"ok": False, "msg": "No existe origen"}, code=404)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.rename(src, dst)
                    return self.send_json({"ok": True, "msg": "Renombrado"})
                if action == "delete":
                    import shutil as _shutil
                    target = safe_fs_path(rel)
                    if target is None or os.path.realpath(target) == SAFE_FS_ROOT:
                        return self.send_json({"ok": False, "msg": "No se puede borrar la raíz"}, code=400)
                    if not os.path.exists(target):
                        return self.send_json({"ok": False, "msg": "No existe"}, code=404)
                    if os.path.isdir(target) and not os.path.islink(target):
                        _shutil.rmtree(target)
                    else:
                        os.remove(target)
                    return self.send_json({"ok": True, "msg": "Eliminado"})
                return self.send_json({"ok": False, "msg": "Acción desconocida"}, code=400)
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)

        if u.path == "/api/public-ip":
            ok, msg = set_public_ip(payload.get("public_ip", "") or payload.get("ip", ""))
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/backup-now":
            if not HAVE_BACKUP:
                return self.send_json({"ok": False, "msg": "backup.py no disponible"})
            if backup_state["running"]:
                return self.send_json({"ok": False, "msg": "ya hay un trabajo en curso: " + backup_state.get("msg", "")})
            threading.Thread(target=_backup_worker, args=("run",), daemon=True).start()
            return self.send_json({"ok": True, "msg": "Backup iniciado en segundo plano"})

        if u.path == "/api/restore":
            if not HAVE_BACKUP:
                return self.send_json({"ok": False, "msg": "backup.py no disponible"})
            if backup_state["running"]:
                return self.send_json({"ok": False, "msg": "ya hay un trabajo en curso"})
            name = os.path.basename(payload.get("name", ""))
            if not name or not bk.FNAME_RE.match(name):
                return self.send_json({"ok": False, "msg": "backup inválido"}, code=400)
            threading.Thread(target=_backup_worker, args=("restore", name), daemon=True).start()
            return self.send_json({"ok": True, "msg": f"Restaurando {name} (detiene el servidor, hace pre-backup y rearranca)..."})

        if u.path == "/api/backup-delete":
            if not HAVE_BACKUP:
                return self.send_json({"ok": False, "msg": "backup.py no disponible"})
            name = os.path.basename(payload.get("name", ""))
            _, _, bdir, _, _ = _bk_dirs()
            target = os.path.join(bdir, name)
            if not name or not bk.FNAME_RE.match(name) or not os.path.isfile(target):
                return self.send_json({"ok": False, "msg": "backup inválido"}, code=400)
            try:
                os.remove(target)
                return self.send_json({"ok": True, "msg": f"Eliminado {name}"})
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), BisectPanelHandler)
    print(f"[BisectPanel] Ejecutando en http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
