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
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
import gzip
import binascii
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import urllib.request
import urllib.error

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


def _raw_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_panel_setting(key, value):
    raw = _raw_config()
    raw[key] = value
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_FILE)


def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def verify_password(pw, saved):
    try:
        salt, hexd = saved.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
        return hmac.compare_digest(dk.hex(), hexd)
    except Exception:
        return False


def auth_enabled():
    return bool(CFG.get("panel_password_hash"))


_sessions = {}
_sessions_lock = threading.Lock()
SESSION_TTL = 24 * 3600


def new_session():
    tok = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[tok] = time.time() + SESSION_TTL
        if len(_sessions) > 200:
            now = time.time()
            for k in [k for k, v in _sessions.items() if v < now]:
                _sessions.pop(k, None)
    return tok


def valid_session(tok):
    if not tok:
        return False
    with _sessions_lock:
        exp = _sessions.get(tok)
        if exp and exp > time.time():
            return True
        _sessions.pop(tok, None)
        return False


def drop_session(tok):
    with _sessions_lock:
        _sessions.pop(tok, None)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diagnostics as dg

CFG = load_config()

SERVER_DIR = CFG["server_dir"]
START_CMD = CFG["start_cmd"]
LOG_FILE = os.path.join(SERVER_DIR, "logs", "latest.log")
CHILD_LOG = os.path.join(SERVER_DIR, "logs", "panel-child.log")
PORT = int(CFG.get("panel_port", 8000))
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


class Server:
    """Todo el estado y config de un servidor Minecraft gestionado."""

    def __init__(self, cfg):
        self.id = str(cfg.get("id", "main"))
        self.name = cfg.get("server_name", "MiServidor")
        self.subtitle = cfg.get("server_subtitle", "Minecraft Server")
        self.server_dir = os.path.expanduser(cfg.get("server_dir", "~/minecraft-server"))
        self.start_cmd = cfg.get("start_cmd", ["bash", "start.sh"])
        self.world_name = cfg.get("world_name", "world")
        self.mc_port = int(cfg.get("mc_port", 25565))
        self.rcon_port = int(cfg.get("rcon_port", 25575))
        self.max_mem_gb = float(cfg.get("max_mem_gb", 4.0))
        self.backup_enabled = bool(cfg.get("backup_enabled", False))
        self.backup_dir = os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
        self.backup_time = str(cfg.get("backup_time", "04:00"))
        self.retention_days = int(cfg.get("retention_days", 7))
        self.keep_monthly = bool(cfg.get("keep_monthly", True))
        data_dir = os.path.join(CONFIG_DIR, "servers", self.id)
        self.data_dir = cfg.get("data_dir", data_dir)
        self.rcon_pass_file = cfg.get("rcon_pass_file") or _first_existing(
            os.path.join(self.data_dir, "rcon-password"),
            os.path.join(CONFIG_DIR, "rcon-password"),
            os.path.expanduser("~/.config/mc-panel-rcon"))
        self.public_ip_file = cfg.get("public_ip_file") or _first_existing(
            os.path.join(self.data_dir, "public-ip"),
            os.path.join(CONFIG_DIR, "public-ip"),
            os.path.expanduser("~/.config/mc-panel-public-ip"))
        self.lock = threading.Lock()
        self.proc = None
        self.stop_requested = False
        try:
            self.conn_off = os.path.getsize(os.path.join(self.server_dir, "logs", "latest.log"))
        except Exception:
            self.conn_off = 0
        self.conn_open = {}
        self.crash_guard = dg.CrashLoopGuard(
            int(cfg.get("crash_max", 3)), int(cfg.get("crash_window_min", 10)) * 60)
        self.crash_guard_on = bool(cfg.get("crash_guard", True))
        self.crash_restart = bool(cfg.get("crash_restart", False))
        self.crash_blocked = False
        self.crash_last = None
        self.started_at = 0.0
        self._apply_file_overrides()

    def _apply_file_overrides(self):
        """backup.json por servidor sobrescribe lo de config.json."""
        try:
            with open(os.path.join(self.data_dir, "backup.json")) as f:
                ov = json.load(f)
            if not isinstance(ov, dict):
                return
            if ov.get("backup_dir"):
                self.backup_dir = os.path.expanduser(ov["backup_dir"])
            if ov.get("backup_time"):
                self.backup_time = str(ov["backup_time"])
            if "retention_days" in ov:
                self.retention_days = max(1, int(ov["retention_days"]))
            if "keep_monthly" in ov:
                self.keep_monthly = bool(ov["keep_monthly"])
            if "backup_enabled" in ov:
                self.backup_enabled = bool(ov["backup_enabled"])
        except Exception:
            pass
        self.metrics = collections.deque(maxlen=40)
        self.cpu_prev = None
        self.tps_lock = threading.Lock()
        self.tps_cache = {"ts": 0, "data": {"ok": False}}
        self.tps_sampling = False
        self.backup_lock = threading.Lock()
        self.backup_state = {"running": False, "job": None, "msg": "",
                             "updated": 0, "pct": 0, "stage": ""}
        self.sessions_cache = {"ts": 0, "names": [], "data": {}}

    @property
    def log_file(self):
        return os.path.join(self.server_dir, "logs", "latest.log")

    @property
    def child_log(self):
        return os.path.join(self.server_dir, "logs", "panel-child.log")

    @property
    def fs_root(self):
        return os.path.realpath(self.server_dir)

    def rcon_password(self):
        try:
            with open(self.rcon_pass_file) as f:
                pw = f.read().strip()
                return pw or None
        except Exception:
            return None

    def local_ip(self):
        return f"localhost:{self.mc_port}"


def _server_cfgs():
    if isinstance(CFG.get("servers"), list) and CFG["servers"]:
        out = []
        for i, sc in enumerate(CFG["servers"]):
            d = dict(sc)
            d.setdefault("id", d.get("server_name", f"srv{i}") or f"srv{i}")
            out.append(d)
        return out
    d = dict(CFG)
    d["id"] = "main"
    return [d]


SERVERS = {}
for _sc in _server_cfgs():
    try:
        _srv = Server(_sc)
        SERVERS[_srv.id] = _srv
    except Exception as e:
        print(f"[pkhosting] servidor ignorado ({e})", flush=True)
if not SERVERS:
    SERVERS["main"] = Server(dict(CFG, id="main"))

_ctx = threading.local()


def S():
    """Servidor del request/hilo actual (default: el primero)."""
    srv = getattr(_ctx, "srv", None)
    if srv is None:
        srv = next(iter(SERVERS.values()))
        _ctx.srv = srv
    return srv


SRV_CFG_KEYS = ("id", "server_name", "server_subtitle", "server_dir", "start_cmd",
                "world_name", "mc_port", "rcon_port", "max_mem_gb", "backup_enabled",
                "backup_dir", "backup_time", "retention_days", "keep_monthly",
                "data_dir", "rcon_pass_file", "public_ip_file")


def _srv_running(s):
    try:
        return s.proc is not None and s.proc.poll() is None
    except Exception:
        return False


def _slugify(name):
    out = []
    for c in (name or "").lower().replace("_", "-"):
        if "a" <= c <= "z" or "0" <= c <= "9" or c == "-":
            out.append(c)
        elif c in (" ", ".", "/"):
            out.append("-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return (s[:32] or "srv")


def _persist_servers():
    raw = _raw_config()
    raw["servers"] = CFG.get("servers", [])
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_FILE)


def _ensure_servers_list():
    """Migra la config de un servidor a lista; devuelve la lista."""
    lst = CFG.get("servers")
    if isinstance(lst, list) and lst:
        return lst
    main_entry = {k: CFG[k] for k in SRV_CFG_KEYS if k in CFG}
    main_entry["id"] = "main"
    CFG["servers"] = [main_entry]
    return CFG["servers"]


def _cfg_server_entry(sid):
    """Devuelve (entry, es_nivel_superior) para mutar, o (None, False)."""
    lst = CFG.get("servers")
    if isinstance(lst, list) and lst:
        for e in lst:
            if isinstance(e, dict) and str(e.get("id", "")) == sid:
                return e, False
        return None, False
    if sid == "main":
        return CFG, True
    return None, False


def _parse_srv_fields(payload, exclude_id=None):
    """Valida campos comunes de add/update. Devuelve (vals, error)."""
    name = str(payload.get("name", "")).strip()[:40]
    if not name:
        return None, "El nombre es obligatorio"
    sdir = os.path.expanduser(str(payload.get("server_dir", "")).strip())
    if not sdir or not os.path.isdir(sdir):
        return None, "La carpeta del servidor no existe"
    try:
        mc = int(payload.get("mc_port", 0))
        rcon = int(payload.get("rcon_port", 0))
        mem = float(payload.get("max_mem_gb", 0))
    except (TypeError, ValueError):
        return None, "Puertos y RAM deben ser números"
    if not 1 <= mc <= 65535 or not 1 <= rcon <= 65535:
        return None, "Puertos fuera de rango (1-65535)"
    if mc == rcon:
        return None, "El puerto MC y el RCON deben diferir"
    if not 0.5 <= mem <= 64:
        return None, "RAM fuera de rango (0.5-64 GB)"
    rp = os.path.realpath(sdir)
    for s in SERVERS.values():
        if exclude_id is not None and s.id == exclude_id:
            continue
        if s.mc_port == mc:
            return None, f"Puerto MC {mc} ya usado por '{s.name}'"
        if s.rcon_port == rcon:
            return None, f"Puerto RCON {rcon} ya usado por '{s.name}'"
        if os.path.realpath(s.server_dir) == rp:
            return None, f"La carpeta ya la usa '{s.name}'"
    return {"name": name, "server_dir": sdir, "mc_port": mc,
            "rcon_port": rcon, "max_mem_gb": mem}, None


RCON_PASS_FILE = _first_existing(
    os.path.join(CONFIG_DIR, "rcon-password"),
    os.path.expanduser("~/.config/mc-panel-rcon"),  # legado
)


def get_rcon_password():
    return S().rcon_password()


def rcon_send(cmd, timeout=5):
    """Cliente RCON minimo (stdlib). Devuelve (ok, respuesta)."""
    import socket as _sock
    import struct as _struct
    srv = S()
    pw = srv.rcon_password()
    if not pw:
        return False, "RCON sin password configurado"
    try:
        s = _sock.create_connection((RCON_HOST, srv.rcon_port), timeout=timeout)
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
    srv = S()
    for path in (srv.public_ip_file,
                 os.path.join(CONFIG_DIR, "public-ip"),
                 os.path.expanduser("~/.config/mc-panel-public-ip")):
        try:
            with open(path) as f:
                ip = f.read().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return srv.local_ip()


def set_public_ip(ip):
    ip = (ip or "").strip()
    if not ip:
        return False, "IP vacía"
    if len(ip) > 120 or any(c in ip for c in ("\n", "\r")):
        return False, "IP inválida"
    target = S().public_ip_file
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(ip + "\n")
    try:
        os.chmod(target, 0o600)
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


def find_running_mc_pid():
    """PID del java del servidor: coincide cwd con SERVER_DIR (genérico para
    Vanilla/Forge/Fabric/NeoForge). Excluye lanzadores de cliente."""
    s = S()
    root = os.path.realpath(s.server_dir)
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
    s = S()
    if pid is None:
        s.stop_requested = False
        return "OFFLINE"
    if s.stop_requested:
        return "STOPPING"
    if is_port_listening(s.mc_port):
        return "RUNNING"
    return "STARTING"

def calculate_cpu(pid):
    s = S()
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        utime = int(parts[13])
        stime = int(parts[14])
        now = time.time()

        if s.cpu_prev is not None and s.cpu_prev["pid"] == pid:
            du = (utime + stime) - (s.cpu_prev["utime"] + s.cpu_prev["stime"])
            dt = now - s.cpu_prev["time"]
            s.cpu_prev = {"pid": pid, "utime": utime, "stime": stime, "time": now}
            if dt > 0:
                clk_tck = os.sysconf(os.sysconf_names.get('SC_CLK_TCK', 'SC_CLK_TCK')) or 100
                pct = (du / float(clk_tck)) / dt * 100.0
                return max(0.0, round(pct, 1))
        else:
            s.cpu_prev = {"pid": pid, "utime": utime, "stime": stime, "time": now}
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
    s = S()
    try:
        world_path = os.path.join(s.server_dir, s.world_name)
        total = sum(os.path.getsize(os.path.join(dp, f))
                    for dp, _, fns in os.walk(world_path)
                    for f in fns)
        return round(total / (1024**3), 2)
    except Exception:
        return 0.0

def query_mc_players():
    srv = S()
    s = None
    try:
        s = socket.create_connection(("127.0.0.1", srv.mc_port), timeout=2)
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
        hs = varint(0) + varint(-1) + varint(len(host)) + host + struct.pack(">H", srv.mc_port) + varint(1)
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
        plen, _ = rvi()       # largo total del paquete
        pid, _ = rvi()        # packet ID (status = 0)
        if pid != 0:
            raise ConnectionError(f"packet id inesperado: {pid}")
        slen, _ = rvi()       # el JSON viene como String: VarInt largo + bytes
        data = b""
        while len(data) < slen:
            chunk = s.recv(min(4096, slen - len(data)))
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


LOG_TS_RE = re.compile(r"\[(\d{2})([A-Za-z]{3})(\d{4}) (\d{2}):(\d{2}):(\d{2})")
_LOG_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _parse_log_ts(line):
    m = LOG_TS_RE.search(line)
    if not m:
        return None
    try:
        dd, mon, yyyy, hh, mm, ss = m.groups()
        return datetime.datetime(int(yyyy), _LOG_MONTHS[mon], int(dd),
                                 int(hh), int(mm), int(ss)).timestamp()
    except Exception:
        return None


def player_sessions(online_names):
    """Segundos conectados por jugador (último join posterior al último leave).
    Se cruza la lista del ping con latest.log; caché de 10 s."""
    s = S()
    now = time.time()
    online_names = list(online_names or [])
    if (now - s.sessions_cache["ts"] < 10
            and set(s.sessions_cache["names"]) == set(online_names)):
        return s.sessions_cache["data"]
    result = {}
    if online_names:
        try:
            joins, leaves = {}, {}
            with open(s.log_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "MinecraftServer" not in line:
                        continue
                    if "joined the game" in line:
                        ts = _parse_log_ts(line)
                        if ts:
                            for n in online_names:
                                if n in line:
                                    joins[n] = ts
                    elif "left the game" in line:
                        ts = _parse_log_ts(line)
                        if ts:
                            for n in online_names:
                                if n in line:
                                    leaves[n] = ts
            for n in online_names:
                j = joins.get(n)
                if j and j > (leaves.get(n) or 0):
                    result[n] = max(0, int(now - j))
        except Exception:
            pass
    s.sessions_cache.update(ts=now, names=online_names, data=result)
    return result


TPS_TTL = 60

def sample_tps(force=False):
    """TPS/MSPT vía `tick query` (RCON, instantáneo). Cachea TPS_TTL s."""
    s = S()
    now = time.time()
    with s.tps_lock:
        if not force and s.tps_cache["data"].get("ok") and now - s.tps_cache["ts"] < TPS_TTL:
            return s.tps_cache["data"]
    data = {"ok": False, "tps": None, "mspt": None, "updated": int(now)}
    if find_running_mc_pid() is None:
        with s.tps_lock:
            s.tps_cache.update(ts=now, data=data)
        return data
    try:
        ok, resp = rcon_send("tick query")
        if ok and resp:
            m = re.search(r"Average time per tick:\s*([\d.]+)ms", resp)
            if m:
                data["mspt"] = float(m.group(1))
    except Exception:
        pass
    if data["mspt"]:
        data["tps"] = round(min(20.0, 1000.0 / data["mspt"])
                            if data["mspt"] > 0 else 20.0, 1)
        data["ok"] = True
        if data["tps"] < 15 and now - getattr(s, "last_tps_alert", 0) > 1800:
            s.last_tps_alert = now
            discord_notify("tps", f"⚠️ TPS bajo en **{s.name}**: {data['tps']} ({data['mspt']:.1f} mspt)")
    with s.tps_lock:
        s.tps_cache.update(ts=now, data=data)
    return data


def request_tps_sample():
    """Dispara un muestreo en segundo plano (no bloquea)."""
    s = S()

    def _run():
        _ctx.srv = s
        try:
            sample_tps(force=True)
        finally:
            with s.tps_lock:
                s.tps_sampling = False
    with s.tps_lock:
        if s.tps_sampling:
            return False
        s.tps_sampling = True
    threading.Thread(target=_run, daemon=True).start()
    return True


def tps_worker():
    while True:
        try:
            for srv in SERVERS.values():
                _ctx.srv = srv
                try:
                    if find_running_mc_pid() is not None:
                        with srv.tps_lock:
                            busy = srv.tps_sampling
                            fresh = (srv.tps_cache["data"].get("ok")
                                     and time.time() - srv.tps_cache["ts"] < TPS_TTL)
                        if not busy and not fresh:
                            request_tps_sample()
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(10)


threading.Thread(target=tps_worker, daemon=True).start()


PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


PLAYER_CMDS = {"op": "op {n}", "deop": "deop {n}", "kick": "kick {n}",
               "ban": "ban {n}", "pardon": "pardon {n}",
               "wladd": "whitelist add {n}", "wlremove": "whitelist remove {n}"}


def player_action(action, name, reason=""):
    """op/deop/kick/ban/pardon/wladd/wlremove con validación. Devuelve (ok, msg)."""
    action = (action or "").lower()
    name = (name or "").strip()
    reason = (reason or "").strip().replace("\n", " ")[:100]
    if action not in PLAYER_CMDS:
        return False, "acción inválida"
    if not PLAYER_NAME_RE.match(name):
        return False, "nombre de jugador inválido (3-16 letras, números o _)"
    if find_running_mc_pid() is None:
        return False, "El servidor está apagado"
    cmd = PLAYER_CMDS[action].format(n=name)
    if action in ("kick", "ban") and reason:
        cmd += f" {reason}"
    ok, msg = send_command_action(cmd)
    if ok:
        labels = {"op": "OP otorgado a", "deop": "OP retirado a",
                  "kick": "Expulsado", "ban": "Baneado", "pardon": "Desbaneado",
                  "wladd": "Añadido a la whitelist", "wlremove": "Quitado de la whitelist"}
        return True, f"{labels[action]} {name}"
    return False, msg


def _read_name_list(fname):
    try:
        with open(os.path.join(S().server_dir, fname)) as f:
            data = json.load(f)
        return [e.get("name", "?") for e in data if isinstance(e, dict) and e.get("name")]
    except Exception:
        return []


def _read_bans():
    try:
        with open(os.path.join(S().server_dir, "banned-players.json")) as f:
            data = json.load(f)
        return [{"name": e.get("name", "?"), "reason": e.get("reason") or "—"}
                for e in data if isinstance(e, dict) and e.get("name")]
    except Exception:
        return []


def moderation_lists():
    wl_on = False
    try:
        with open(os.path.join(S().server_dir, "server.properties")) as f:
            for line in f:
                if line.startswith("white-list="):
                    wl_on = line.strip().split("=", 1)[1].lower() == "true"
                    break
    except Exception:
        pass
    return {"ops": _read_name_list("ops.json"), "banned": _read_bans(),
            "whitelist": _read_name_list("whitelist.json"), "whitelist_on": wl_on}


_CONN_EVT_RE = re.compile(r"([A-Za-z0-9_]{3,16})[^A-Za-z0-9_]*\b(joined|left) the game")


def _conn_file(srv):
    return os.path.join(srv.data_dir, "connections.jsonl")


def _conn_scan(srv):
    """Lee joins/leaves nuevos del log y los persiste. Devuelve eventos nuevos."""
    off = getattr(srv, "conn_off", 0)
    try:
        size = os.path.getsize(srv.log_file)
        if size < off:
            off = 0
        with open(srv.log_file, "rb") as f:
            f.seek(off)
            chunk = f.read()
        srv.conn_off = size
    except Exception:
        return []
    oj = getattr(srv, "conn_open", None)
    if oj is None:
        oj = {}
        srv.conn_open = oj
    new = []
    for line in chunk.decode("utf-8", "replace").splitlines():
        if "MinecraftServer" not in line:
            continue
        m = None
        for mm in _CONN_EVT_RE.finditer(line):
            m = mm
        if not m:
            continue
        ts = _parse_log_ts(line)
        if not ts:
            continue
        name, ev = m.group(1), m.group(2)
        if ev == "joined":
            oj[name] = ts
            new.append({"t": int(ts), "name": name, "ev": "join", "dur": 0})
        else:
            dur = int(ts - oj.pop(name, ts))
            new.append({"t": int(ts), "name": name, "ev": "leave", "dur": max(0, dur)})
    if new:
        try:
            os.makedirs(srv.data_dir, exist_ok=True)
            seen = set()
            try:
                with open(_conn_file(srv)) as f:
                    for line in f:
                        try:
                            o = json.loads(line)
                            seen.add((o.get("t"), o.get("name"), o.get("ev")))
                        except Exception:
                            continue
            except FileNotFoundError:
                pass
            new = [e for e in new if (e["t"], e["name"], e["ev"]) not in seen]
            if not new:
                return []
            with open(_conn_file(srv), "a") as f:
                for e in new:
                    f.write(json.dumps(e) + "\n")
            with open(_conn_file(srv)) as f:
                lines = f.readlines()
            if len(lines) > 300:
                with open(_conn_file(srv), "w") as f:
                    f.writelines(lines[-300:])
        except Exception:
            pass
        for e in new:
            if e["ev"] == "join":
                discord_notify("join", f"➡️ **{e['name']}** entró a **{srv.name}**")
            else:
                discord_notify("leave", f"⬅️ **{e['name']}** salió de **{srv.name}**")
    return new


def connection_history(limit=30):
    srv = S()
    _conn_scan(srv)
    try:
        with open(_conn_file(srv)) as f:
            items = [json.loads(l) for l in f.readlines() if l.strip()]
        return items[-limit:][::-1]
    except Exception:
        return []


def connections_worker():
    while True:
        try:
            for srv in SERVERS.values():
                _ctx.srv = srv
                try:
                    _conn_scan(srv)
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(15)


threading.Thread(target=connections_worker, daemon=True).start()


# ── Historial 24h + disco + red ────────────────────────────────
def _hist_file(srv):
    return os.path.join(srv.data_dir, "history.jsonl")


def history_append(srv, cpu, mem, tps, players):
    try:
        os.makedirs(srv.data_dir, exist_ok=True)
        with open(_hist_file(srv), "a") as f:
            f.write(json.dumps({"t": int(time.time()), "cpu": round(cpu, 1),
                                "mem": mem, "tps": tps,
                                "players": players}) + "\n")
        with open(_hist_file(srv)) as f:
            lines = f.readlines()
        if len(lines) > 1600:
            with open(_hist_file(srv), "w") as f:
                f.writelines(lines[-1440:])
    except Exception:
        pass


def history_read(srv, limit=1440):
    try:
        with open(_hist_file(srv)) as f:
            lines = f.readlines()[-limit:]
        items = [json.loads(l) for l in lines if l.strip()]
        return {"times": [i["t"] for i in items],
                "cpu": [i.get("cpu", 0) for i in items],
                "mem": [round(i.get("mem", 0) / 1024.0, 2) for i in items],
                "tps": [i.get("tps") for i in items],
                "players": [i.get("players", 0) for i in items]}
    except Exception:
        return {"times": [], "cpu": [], "mem": [], "tps": [], "players": []}


def history_worker():
    while True:
        try:
            for srv in SERVERS.values():
                _ctx.srv = srv
                try:
                    m = list(srv.metrics)[-1] if srv.metrics else None
                    t = srv.tps_cache["data"]
                    history_append(srv, m["cpu"] if m else 0.0,
                                   m["mem"] if m else 0,
                                   t.get("tps") if t.get("ok") else None,
                                   query_mc_players()[0] if get_server_status(
                                       find_running_mc_pid()) == "RUNNING" else 0)
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=history_worker, daemon=True).start()


_net_prev = {}


def net_rates():
    """kB/s RX/TX del host (todas las interfaces menos lo)."""
    try:
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                iface, vals = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                parts = vals.split()
                rx += int(parts[0])
                tx += int(parts[8])
        now = time.time()
        prev = _net_prev.get("v")
        _net_prev["v"] = (rx, tx, now)
        if not prev:
            return 0.0, 0.0
        dt = max(1, now - prev[2])
        return round((rx - prev[0]) / dt / 1024, 1), round((tx - prev[1]) / dt / 1024, 1)
    except Exception:
        return 0.0, 0.0


_disk_cache = {}


def server_disk_gb(srv):
    hit = _disk_cache.get(srv.id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    gb = round(dir_size_b(srv.server_dir) / 1e9, 2)
    _disk_cache[srv.id] = (time.time(), gb)
    return gb

def metrics_worker():
    """Hilo de fondo que muestrea métricas cada 1.5s de forma continua."""
    while True:
        try:
            for srv in SERVERS.values():
                _ctx.srv = srv
                try:
                    pid = find_running_mc_pid()
                    status = get_server_status(pid)
                    cpu = calculate_cpu(pid) if pid else 0.0
                    mem = get_memory_mb(pid) if pid else 0
                    now_str = time.strftime("%H:%M:%S")
                    srv.metrics.append({
                        "time": now_str,
                        "cpu": cpu,
                        "mem": mem,
                        "status": status
                    })
                    prev = getattr(srv, "last_status", None)
                    if prev is None:
                        srv.last_status = status
                    elif prev != status:
                        srv.last_status = status
                        if status == "RUNNING":
                            discord_notify("up", f"🟢 **{srv.name}** en línea")
                        elif prev == "RUNNING":
                            discord_notify("down", f"🔴 **{srv.name}** se detuvo")
                        if status == "OFFLINE" and prev in ("RUNNING", "STARTING"):
                            handle_unexpected_exit(srv)
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(1.5)

threading.Thread(target=metrics_worker, daemon=True).start()


def _persist_servers_guard(srv, mx, wn, enabled, restart):
    """Guarda la config de la guarda de bucle en el servidor correspondiente de config.json."""
    raw = _raw_config()
    items = raw.get("servers")
    if isinstance(items, list):
        for it in items:
            if str(it.get("id", "main")) == srv.id:
                it.update(crash_max=mx, crash_window_min=wn, crash_guard=enabled, crash_restart=restart)
        save_panel_setting("servers", items)
    else:
        save_panel_setting("crash_max", mx)
        save_panel_setting("crash_window_min", wn)
        save_panel_setting("crash_guard", enabled)
        save_panel_setting("crash_restart", restart)


def _autostart_after_crash(srv, delay=20):
    time.sleep(delay)
    _ctx.srv = srv
    try:
        if find_running_mc_pid() is None:
            start_server_action(auto=True)
    finally:
        _ctx.srv = None


def handle_unexpected_exit(srv):
    """Distingue parada pedida de caída. Si cae, registra, avisa y aplica el límite de bucle."""
    if getattr(srv, "stop_pending", False):
        srv.stop_pending = False
        return
    started = srv.started_at or 0.0
    crash = dg.newest_crash(srv.server_dir, since=started - 5) if started else None
    clean = dg.clean_stop_in_log(srv.log_file)
    if clean and not crash:
        return
    srv.crash_guard.record()
    info = None
    if crash:
        try:
            info = dg.crash_detail(srv.server_dir, crash)
        except Exception:
            info = None
    srv.crash_last = {"t": time.time(), "crash": crash,
                      "summary": (info or {}).get("summary", "Caída sin crash-report"),
                      "suspect": (info or {}).get("suspect", "")}
    n = len(srv.crash_guard.recent())
    msg = f"💥 **{srv.name}** cayó inesperadamente ({n}/{srv.crash_guard.max_crashes} en la ventana)"
    if srv.crash_last["summary"]:
        msg += f"\n`{srv.crash_last['summary'][:300]}`"
    if srv.crash_guard_on and srv.crash_guard.looping():
        srv.crash_blocked = True
        msg += "\n🛑 **Bucle de crash detectado**: se bloquean los reinicios automáticos hasta que lo desbloquees."
    discord_notify("down", msg)
    if srv.crash_restart and not srv.crash_blocked:
        threading.Thread(target=_autostart_after_crash, args=(srv,), daemon=True).start()


def start_server_action(auto=False):
    s = S()
    with s.lock:
        current_pid = find_running_mc_pid()
        if current_pid is not None:
            return False, f"El servidor ya está en ejecución (PID {current_pid})"
        if auto and s.crash_blocked:
            return False, "Inicio automático bloqueado: bucle de crash detectado (desbloquéalo en Diagnóstico)"
        try:
            s.started_at = time.time()
            s.stop_pending = False
            logf = open(s.child_log, "ab", buffering=0)
            s.proc = subprocess.Popen(
                s.start_cmd, cwd=s.server_dir,
                stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                close_fds=True
            )
            s.stop_requested = False
            return True, f"Servidor iniciando (PID {s.proc.pid})..."
        except Exception as e:
            s.proc = None
            return False, f"Error al iniciar: {e}"

def stop_server_action(force=False):
    s = S()
    with s.lock:
        pid = find_running_mc_pid()
        if pid is None:
            s.stop_requested = False
            return False, "El servidor ya está apagado"
        s.stop_requested = True
        s.stop_pending = True
        try:
            if force:
                # Intentar stop elegante por RCON primero, luego SIGKILL
                ok, _ = rcon_send("stop", timeout=3)
                time.sleep(2)
                if find_running_mc_pid() is None:
                    s.stop_requested = False
                    return True, "Servidor detenido (RCON stop)"
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                s.stop_requested = False
                return True, "Servidor terminado forzosamente (SIGKILL)"
            # Apagado elegante en cascada: stdin -> RCON -> SIGTERM
            if s.proc is not None and s.proc.poll() is None and s.proc.stdin:
                try:
                    s.proc.stdin.write(b"stop\n")
                    s.proc.stdin.flush()
                    return True, "Enviada orden de apagado segura (stdin, guardando mundo)..."
                except Exception:
                    pass
            ok, resp = rcon_send("stop", timeout=4)
            if ok:
                return True, "Enviada orden de apagado segura (RCON, guardando mundo)..."
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                s.stop_requested = False
                return False, "El servidor ya está apagado"
            return True, f"Enviada señal SIGTERM (RCON no disponible: {resp})..."
        except Exception as e:
            return False, f"Error al detener: {e}"

def restart_server_action(auto=False):
    ok, msg = stop_server_action(force=False)
    if not ok and "apagado" not in msg.lower():
        return ok, msg
    srv0 = S()

    def _delayed():
        time.sleep(5)
        _ctx.srv = srv0
        start_server_action(auto=auto)
    threading.Thread(target=_delayed, daemon=True).start()
    return True, "Reiniciando servidor..."


def send_command_action(cmd):
    s = S()
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
    if s.proc is not None and s.proc.poll() is None and s.proc.stdin:
        try:
            s.proc.stdin.write((cmd + "\n").encode("utf-8"))
            s.proc.stdin.flush()
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
    srv = S()
    pid = find_running_mc_pid()
    status = get_server_status(pid)
    uptime = get_process_uptime(pid)
    world_gb = get_world_size_gb()
    online, max_p, names = query_mc_players() if status == "RUNNING" else (0, 20, [])

    latest_metric = list(srv.metrics)[-1] if srv.metrics else {"cpu": 0.0, "mem": 0}
    
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
        "max_mem_gb": srv.max_mem_gb,
        "world_gb": world_gb,
        "online_players": online,
        "max_players": max_p,
        "player_names": names,
        "sessions": player_sessions(names) if names else {},
        # Sin proceso no hay TPS que mostrar (el caché viejo se oculta)
        "tps": srv.tps_cache["data"] if pid is not None else {"ok": False},
        "system_load": ", ".join(load),
        "port": srv.mc_port,
        "public_ip": get_public_ip(),
        "playit": get_playit_status(),
        "history": list(srv.metrics),
        "server_id": srv.id,
        "server_name": srv.name,
        "disk_gb": server_disk_gb(srv),
        "net": net_rates(),
    }


try:
    import backup as bk
    HAVE_BACKUP = True
except Exception as e:
    bk = None
    HAVE_BACKUP = False
    print(f"[pkhosting] backup.py no disponible: {e}", flush=True)

def _bk_progress(pct, stage):
    s = S()
    with s.backup_lock:
        s.backup_state.update(pct=max(0, min(100, int(pct))), stage=stage,
                              updated=time.time())


def _bk_log(m, *args, **kwargs):
    # Acepta flush=True porque backup.py llama log(msg, flush=True) como print
    with S().backup_lock:
        S().backup_state.update(msg=m, updated=time.time())


def _bk_send(cmd):
    ok, msg = send_command_action(cmd)
    return ok, msg


def _backup_worker(srv, mode, name=None):
    _ctx.srv = srv
    with srv.backup_lock:
        srv.backup_state.update(running=True, job=mode, msg="iniciando...",
                                pct=0, stage="iniciando", updated=time.time())

    def _prog(pct, stage):
        _ctx.srv = srv
        _bk_progress(pct, stage)

    def _log(m, *a, **k):
        _ctx.srv = srv
        _bk_log(m)

    def _send(cmd):
        _ctx.srv = srv
        return _bk_send(cmd)

    def _stop():
        _ctx.srv = srv
        return stop_server_action(force=False)

    def _start():
        _ctx.srv = srv
        return start_server_action()

    def _running():
        _ctx.srv = srv
        return find_running_mc_pid() is not None
    try:
        if mode == "run":
            ok, msg = bk.run_backup(srv.server_dir, srv.world_name, srv.backup_dir,
                                    send_fn=_send,
                                    retention_days=srv.retention_days,
                                    keep_monthly=srv.keep_monthly,
                                    log=_log, progress=_prog)
        elif mode == "restore":
            ok, msg = bk.restore_backup(
                srv.server_dir, srv.backup_dir, name,
                stop_fn=_stop, start_fn=_start, is_running_fn=_running,
                pre_backup=True, retention_days=srv.retention_days,
                keep_monthly=srv.keep_monthly,
                log=_log, progress=_prog)
        else:
            ok, msg = False, "trabajo desconocido"
        if not ok and mode == "run":
            discord_notify("backup", f"❌ Backup fallido en **{srv.name}**: {msg[:200]}")
        with srv.backup_lock:
            srv.backup_state.update(running=False, msg=("OK " if ok else "FAIL ") + msg,
                                pct=100 if ok else srv.backup_state.get("pct", 0),
                                stage="completado" if ok else "error",
                                updated=time.time())
    except Exception as e:
        with srv.backup_lock:
            srv.backup_state.update(running=False, msg=f"FAIL {e}",
                                    stage="error", updated=time.time())


def backups_status():
    import shutil as _sh
    srv = S()
    c = {"enabled": srv.backup_enabled, "dir": srv.backup_dir,
         "retention_days": srv.retention_days, "keep_monthly": srv.keep_monthly,
         "time": srv.backup_time}
    info = {"ok": HAVE_BACKUP, "enabled": c["enabled"], "dir": c["dir"],
            "time": c["time"], "retention_days": c["retention_days"],
            "keep_monthly": c["keep_monthly"],
            "job": dict(srv.backup_state), "items": []}
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
        total_b = 0
        for i in items:
            total_b += i["size_b"]
            i["size"] = (f"{i['size_b']/1e9:.2f} GB" if i["size_b"] > 1e9
                         else f"{i['size_b']/1e6:.0f} MB")
            i["date"] = datetime.datetime.fromtimestamp(i["mtime"]).strftime("%d/%m/%Y %H:%M")
        info["items"] = items
        info["total_b"] = total_b
        info["monthly"] = sum(1 for i in items if i.get("monthly"))
    except Exception as e:
        info["msg"] = str(e)
    return info


MAX_FILE_READ = 2 * 1024 * 1024  # 2 MB


# ── Discord (stdlib, webhook) ────────────────────────────────────
def discord_config():
    return {"webhook": CFG.get("discord_webhook", ""),
            "events": CFG.get("discord_events", {})}


def discord_send(text):
    url = (CFG.get("discord_webhook", "") or "").strip()
    if not url:
        return False, "sin webhook configurado"
    try:
        import urllib.request as _url
        req = _url.Request(url, data=json.dumps({"content": text[:1800]}).encode(),
                           headers={"Content-Type": "application/json"})
        with _url.urlopen(req, timeout=8):
            pass
        return True, "enviado"
    except Exception as e:
        return False, str(e)


def discord_notify(event, text):
    try:
        ev = CFG.get("discord_events", {})
        if not ev.get(event, False):
            return
        threading.Thread(target=discord_send, args=(text,), daemon=True).start()
    except Exception:
        pass


# ── Scheduler (tareas programadas por servidor) ──────────────────
# kinds: command | message | save | restart. El restart existe como tipo
# pero NO se crea ninguna tarea por defecto: es 100% opt-in desde la UI.
def _sched_file(srv):
    return os.path.join(srv.data_dir, "schedules.json")


def _load_schedules(srv):
    try:
        with open(_sched_file(srv)) as f:
            items = json.load(f)
            return items if isinstance(items, list) else []
    except Exception:
        return []


def _save_schedules(srv, items):
    os.makedirs(srv.data_dir, exist_ok=True)
    tmp = _sched_file(srv) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, _sched_file(srv))


def _task_due(t, now):
    if not t.get("enabled", True):
        return False
    last = float(t.get("last_run") or 0)
    if t.get("mode") == "interval":
        every = max(5, int(t.get("every_min") or 60))
        return now - last >= every * 60
    at = str(t.get("at", "04:00"))
    try:
        hh, mm = map(int, at.split(":"))
        day = datetime.datetime.now().replace(hour=0, minute=0, second=0,
                                              microsecond=0).timestamp()
        target = day + hh * 3600 + mm * 60
        return now >= target and last < target
    except Exception:
        return False


def _exec_task(srv, t):
    _ctx.srv = srv
    kind, payload = t.get("kind"), (t.get("payload") or "").strip()
    if kind == "restart":
        warn = int(t.get("warn_min") or 0)
        if warn > 0 and find_running_mc_pid() is not None:
            send_command_action(f"say Reinicio del servidor en {warn} min")
            time.sleep(warn * 60)
        restart_server_action(auto=True)
    elif kind == "save":
        send_command_action("save-all")
    elif kind == "message":
        if payload:
            send_command_action(f"say {payload}")
    else:  # command
        if payload:
            send_command_action(payload)


def scheduler_worker():
    while True:
        try:
            for srv in SERVERS.values():
                try:
                    tasks = _load_schedules(srv)
                except Exception:
                    continue
                if not tasks:
                    continue
                now = time.time()
                dirty = False
                for t in tasks:
                    try:
                        if _task_due(t, now):
                            t["last_run"] = now
                            dirty = True
                            threading.Thread(target=_exec_task,
                                             args=(srv, dict(t)),
                                             daemon=True).start()
                    except Exception:
                        pass
                if dirty:
                    try:
                        _save_schedules(srv, tasks)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(20)


threading.Thread(target=scheduler_worker, daemon=True).start()

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
    root = S().fs_root
    rel = (rel or "").lstrip("/")
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
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
                    "size_b": sz,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
        return {"path": rel.strip("/"), "entries": entries}, None
    except Exception as e:
        return None, str(e)

def read_console_lines(limit=250):
    srv = S()
    for fpath in (srv.child_log, srv.log_file):
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
# PLANTILLA HTML/CSS/JS (tema Liquid Glass)
# ══════════════════════════════════════════════════════════════════



MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8"}
_static_cache = {}


def _static_raw(name):
    path = os.path.join(STATIC_DIR, name)
    mt = os.path.getmtime(path)
    hit = _static_cache.get(name)
    if hit and hit[0] == mt:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        data = f.read()
    _static_cache[name] = (mt, data)
    return data


def static_version():
    try:
        return str(int(max(os.path.getmtime(os.path.join(STATIC_DIR, n))
                           for n in os.listdir(STATIC_DIR))))
    except OSError:
        return "0"


def render_static(name, srv=None):
    """Plantilla estática con los valores del servidor (nombre, subtítulo, puertos, dir)."""
    srv = srv or S()
    p = _static_raw(name)
    local_ip = srv.local_ip()
    repl = {
        "PrankLindorf": srv.name,
        "NeoForge 1.21.1 · Java 21 · Puerto 25566":
            f"{srv.subtitle} · Puerto {srv.mc_port}",
        "localhost:25566": local_ip,
        "25566 (TCP / UDP)": f"{srv.mc_port} (TCP / UDP)",
        "(puerto 25566)": f"(puerto {srv.mc_port})",
        "~/PrankLindorf-NeoForge": srv.server_dir,
        "16 núcleos disponibles": f"{os.cpu_count() or '?'} núcleos disponibles",
        "PKHosting Panel — " + srv.name: f"PKHosting Panel — {srv.name}",
        "__SRV__": srv.id,
        "__V__": static_version(),
    }
    for old, new in repl.items():
        p = p.replace(old, new)
    return p


def build_page(srv=None):
    srv = srv or S()
    p = render_static("index.html", srv)
    if len(SERVERS) > 1:
        opts = "".join(
            f'<option value="{s.id}"{" selected" if s.id == srv.id else ""}>{s.name}</option>'
            for s in SERVERS.values())
        switcher = (
            '<div class="server-switcher">'
            '<select id="srvSel" onchange="location.search=\'?server=\'+encodeURIComponent(this.value)" '
            'aria-label="Servidor">'
            + opts + '</select></div>')
        p = p.replace('<div class="server-selector">', switcher + '<div class="server-selector">', 1)
    return p


def backup_cfg_get(srv):
    return {"backup_enabled": srv.backup_enabled, "backup_dir": srv.backup_dir,
            "backup_time": srv.backup_time, "retention_days": srv.retention_days,
            "keep_monthly": srv.keep_monthly}


def backup_cfg_set(srv, payload):
    ov = {}
    if "backup_enabled" in payload:
        ov["backup_enabled"] = bool(payload["backup_enabled"])
    if payload.get("backup_dir"):
        ov["backup_dir"] = str(payload["backup_dir"])[:300]
    if payload.get("backup_time"):
        t = str(payload["backup_time"])
        if re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", t):
            ov["backup_time"] = t
        else:
            return False, "Hora inválida (HH:MM)"
    if "retention_days" in payload:
        try:
            ov["retention_days"] = max(1, min(365, int(payload["retention_days"])))
        except Exception:
            return False, "Retención inválida"
    if "keep_monthly" in payload:
        ov["keep_monthly"] = bool(payload["keep_monthly"])
    try:
        os.makedirs(srv.data_dir, exist_ok=True)
        with open(os.path.join(srv.data_dir, "backup.json"), "w") as f:
            json.dump(ov, f, indent=2)
    except Exception as e:
        return False, str(e)
    srv._apply_file_overrides()
    return True, "Configuración de backups guardada"


def list_mods():
    srv = S()
    mods = os.path.join(srv.server_dir, "mods")
    out = []
    try:
        for name in sorted(os.listdir(mods), key=str.lower):
            if name.startswith("."):
                continue
            if name.endswith(".jar.disabled"):
                out.append({"name": name[:-9], "enabled": False,
                            "size": os.path.getsize(os.path.join(mods, name))})
            elif name.endswith(".jar"):
                out.append({"name": name, "enabled": True,
                            "size": os.path.getsize(os.path.join(mods, name))})
    except FileNotFoundError:
        pass
    return out


def search_modrinth(query, loader="neoforge", game_ver="1.21.1", limit=12):
    q = urllib.parse.quote(query)
    facets = [f'["versions:{game_ver}"]']
    if loader:
        facets.append(f'["categories:{loader}"]')
    facets.append('["project_type:mod"]')
    facets_param = urllib.parse.quote(f"[{','.join(facets)}]")
    url = f"https://api.modrinth.com/v2/search?query={q}&facets={facets_param}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "PKHosting-Panel/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            hits = []
            for h in data.get("hits", []):
                hits.append({
                    "id": h.get("project_id"),
                    "slug": h.get("slug"),
                    "title": h.get("title"),
                    "description": h.get("description"),
                    "author": h.get("author"),
                    "icon": h.get("icon_url"),
                    "downloads": h.get("downloads", 0),
                    "follows": h.get("follows", 0),
                    "categories": h.get("categories", [])
                })
            return True, hits
    except Exception as e:
        return False, str(e)


def install_modrinth_project(slug_or_id, loader="neoforge", game_ver="1.21.1"):
    srv = S()
    mods_dir = os.path.join(srv.server_dir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    slug = urllib.parse.quote(slug_or_id)
    url = f"https://api.modrinth.com/v2/project/{slug}/version?game_versions=%5B%22{game_ver}%22%5D&loaders=%5B%22{loader}%22%5D"
    req = urllib.request.Request(url, headers={"User-Agent": "PKHosting-Panel/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            versions = json.loads(resp.read().decode())
        if not versions:
            url_fallback = f"https://api.modrinth.com/v2/project/{slug}/version"
            req_fb = urllib.request.Request(url_fallback, headers={"User-Agent": "PKHosting-Panel/2.0"})
            with urllib.request.urlopen(req_fb, timeout=10) as resp:
                versions = json.loads(resp.read().decode())
        if not versions:
            return False, "No se encontraron versiones disponibles para este mod"
        
        target_file = None
        for v in versions:
            for f in v.get("files", []):
                fn = f.get("filename", "")
                if fn.endswith(".jar"):
                    target_file = f
                    break
            if target_file:
                break
        
        if not target_file:
            return False, "No se encontró ningún archivo .jar en la versión de Modrinth"
        
        dl_url = target_file.get("url")
        dl_fn = target_file.get("filename")
        dest_path = os.path.join(mods_dir, dl_fn)
        req_dl = urllib.request.Request(dl_url, headers={"User-Agent": "PKHosting-Panel/2.0"})
        with urllib.request.urlopen(req_dl, timeout=30) as resp, open(dest_path, "wb") as out_f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out_f.write(chunk)
        return True, f"Mod '{dl_fn}' instalado correctamente en mods/ (requiere reinicio)"
    except Exception as e:
        return False, f"Error descargando mod: {e}"


# ── Render de terreno estilo MCA Selector (solo stdlib) ──
class _NBTReader:
    def __init__(self, data):
        self.d = data
        self.o = 0

    def _take(self, n):
        if self.o + n > len(self.d):
            raise ValueError("NBT truncado")
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def _u8(self):
        return self._take(1)[0]

    def _u16(self):
        return struct.unpack(">H", self._take(2))[0]

    def _i16(self):
        return struct.unpack(">h", self._take(2))[0]

    def _i32(self):
        return struct.unpack(">i", self._take(4))[0]

    def _i64(self):
        return struct.unpack(">q", self._take(8))[0]

    def _f32(self):
        return struct.unpack(">f", self._take(4))[0]

    def _f64(self):
        return struct.unpack(">d", self._take(8))[0]

    def _str(self):
        n = self._u16()
        return self._take(n).decode("utf-8", "replace")

    def _payload(self, tid):
        if tid == 1:
            return struct.unpack("b", self._take(1))[0]
        if tid == 2:
            return self._i16()
        if tid == 3:
            return self._i32()
        if tid == 4:
            return self._i64()
        if tid == 5:
            return self._f32()
        if tid == 6:
            return self._f64()
        if tid == 7:
            n = self._i32()
            return self._take(max(0, n))
        if tid == 8:
            return self._str()
        if tid == 9:
            et = self._u8()
            n = self._i32()
            return [self._payload(et) for _ in range(max(0, n))]
        if tid == 10:
            out = {}
            while True:
                st = self._u8()
                if st == 0:
                    return out
                # NOTA: no usar out[self._str()] = self._payload(st);
                # en Python 3.14 el valor se evalúa antes que la clave.
                nm = self._str()
                out[nm] = self._payload(st)
        if tid == 11:
            n = self._i32()
            return [self._i32() for _ in range(max(0, n))]
        if tid == 12:
            n = self._i32()
            return [self._i64() for _ in range(max(0, n))]
        raise ValueError(f"TAG desconocido: {tid}")


def _nbt_load(data):
    r = _NBTReader(data)
    tid = r._u8()
    if tid != 10:
        raise ValueError("Raíz NBT no es Compound")
    r._str()
    return r._payload(10)


def _mca_read_chunk(fp, cx, cz):
    """Lee y descomprime el NBT de un chunk (cx, cz locales 0-31)."""
    try:
        with open(fp, "rb") as f:
            f.seek(4 * ((cz & 31) * 32 + (cx & 31)))
            entry = struct.unpack(">I", f.read(4))[0]
            loc, count = entry >> 8, entry & 0xFF
            if not loc or not count:
                return None
            f.seek(loc * 4096)
            length = struct.unpack(">I", f.read(4))[0]
            if length <= 1 or length > 64 * 1024 * 1024:
                return None
            ctype = f.read(1)[0]
            payload = f.read(length - 1)
        if ctype == 2:
            raw = zlib.decompress(payload)
        elif ctype == 1:
            raw = gzip.decompress(payload)
        elif ctype == 3:
            raw = payload
        else:
            return None
        return _nbt_load(raw)
    except Exception:
        return None


def _sec_table(sec):
    bs = sec.get("block_states") if isinstance(sec, dict) else None
    if not isinstance(bs, dict):
        return None
    pal = bs.get("palette") or []
    names = []
    for e in pal:
        n = (e.get("Name") if isinstance(e, dict) else "") or "minecraft:air"
        names.append(n.split("[")[0])
    if len(names) <= 1:
        return (names, None, 0, 0)
    data = bs.get("data") or []
    bpb = max(4, (len(names) - 1).bit_length())
    return (names, [v & 0xFFFFFFFFFFFFFFFF for v in data], bpb, 64 // bpb)


def _block_name(tables, secs, y, lx, ly, lz):
    sec = secs.get(y >> 4)
    if sec is None:
        return "minecraft:air"
    t = tables.get(y >> 4)
    if t is None:
        return "minecraft:air"
    names, data, bpb, epl = t
    if data is None:
        return names[0] if names else "minecraft:air"
    p = (ly * 16 + lz) * 16 + lx
    li, off = p // epl, (p % epl) * bpb
    if li >= len(data):
        return "minecraft:air"
    idx = (data[li] >> off) & ((1 << bpb) - 1)
    return names[idx] if idx < len(names) else "minecraft:air"


def _height_from(hmap, idx):
    # 7 alturas de 9 bits por long (LSB primero) + offset de -min_y (64).
    longs = [v & 0xFFFFFFFFFFFFFFFF for v in hmap]
    li, off = idx // 7, (idx % 7) * 9
    if li >= len(longs):
        return -65
    return (((longs[li] >> off) & 0x1FF) - 64)


_TERRAIN_EXACT = {
    "minecraft:grass_block": (111, 175, 71),
    "minecraft:dirt": (134, 96, 67),
    "minecraft:coarse_dirt": (122, 88, 60),
    "minecraft:rooted_dirt": (128, 96, 66),
    "minecraft:mud": (110, 92, 80),
    "minecraft:sand": (221, 210, 170),
    "minecraft:red_sand": (198, 128, 74),
    "minecraft:gravel": (136, 126, 126),
    "minecraft:water": (47, 110, 214),
    "minecraft:stone": (128, 128, 128),
    "minecraft:bedrock": (58, 58, 62),
    "minecraft:snow_block": (240, 246, 250),
    "minecraft:snow": (240, 246, 250),
    "minecraft:ice": (170, 205, 235),
    "minecraft:packed_ice": (170, 200, 235),
    "minecraft:clay": (160, 165, 185),
    "minecraft:oak_log": (105, 85, 50),
    "minecraft:netherrack": (112, 52, 52),
    "minecraft:soul_sand": (92, 62, 52),
    "minecraft:soul_soil": (88, 58, 48),
    "minecraft:basalt": (72, 70, 76),
    "minecraft:blackstone": (48, 44, 52),
    "minecraft:magma_block": (185, 95, 40),
    "minecraft:glowstone": (240, 200, 120),
    "minecraft:shroomlight": (238, 190, 110),
    "minecraft:crimson_nylium": (150, 45, 65),
    "minecraft:warped_nylium": (45, 145, 130),
    "minecraft:end_stone": (228, 230, 170),
    "minecraft:obsidian": (26, 16, 36),
    "minecraft:purpur_block": (205, 175, 205),
    "minecraft:chorus_plant": (150, 130, 150),
    "minecraft:podzol": (110, 82, 52),
    "minecraft:mycelium": (120, 100, 115),
    "minecraft:farmland": (120, 80, 55),
    "minecraft:grass_path": (150, 140, 95),
}


def _terrain_base(name):
    if name in _TERRAIN_EXACT:
        return _TERRAIN_EXACT[name]
    if name.endswith("_leaves") or name in (
            "minecraft:short_grass", "minecraft:tall_grass", "minecraft:fern",
            "minecraft:large_fern", "minecraft:vine", "minecraft:glow_lichen",
            "minecraft:sugar_cane", "minecraft:bamboo", "minecraft:cactus",
            "minecraft:sweet_berry_bush", "minecraft:kelp", "minecraft:seagrass"):
        return (52, 125, 48)
    if name.endswith("_log") or name.endswith("_wood") or name.endswith("_stem") or name.endswith("_hyphae"):
        return (105, 85, 50)
    if name.endswith("_planks") or "minecraft:crafting_table" in name:
        return (170, 135, 85)
    if "sandstone" in name or name == "minecraft:sand":
        return (221, 210, 170)
    if name.endswith("_sand") or name.endswith("_gravel"):
        return (200, 180, 150)
    if "deepslate" in name:
        return (72, 72, 80)
    if name.endswith("_ore") or "infested" in name:
        return (130, 125, 120)
    if any(k in name for k in ("stone", "andesite", "diorite", "granite", "tuff",
                               "cobble", "brick", "prismarine", "quartz")):
        return (128, 128, 128)
    if "snow" in name or "ice" in name or "frosted" in name:
        return (235, 242, 248)
    if "wool" in name or "carpet" in name or "concrete" in name or "terracotta" in name:
        return (170, 170, 175)
    if "glass" in name:
        return (200, 225, 235)
    if "flower" in name or "tulip" in name or "daisy" in name or "blossom" in name:
        return (110, 175, 71)
    if "wheat" in name or "carrot" in name or "potato" in name or "beetroot" in name or "melon" in name or "pumpkin" in name:
        return (140, 170, 70)
    if "lava" in name:
        return (220, 120, 30)
    return (150, 150, 150)


def _terrain_px(name, y, lx, lz):
    if not name or name in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air"):
        return (0, 0, 0, 0)
    r, g, b = _terrain_base(name)
    if name == "minecraft:water":
        f = 0.88 + 0.12 * (((lx * 7 + lz * 13) % 5) / 4.0)
    else:
        f = 0.80 + 0.32 * max(0.0, min(1.0, (y - 55) / 130.0))
    return (max(0, min(255, int(r * f))), max(0, min(255, int(g * f))),
            max(0, min(255, int(b * f))), 255)


def _png_encode_rgba(width, height, rows):
    raw = b"".join(b"\x00" + r for r in rows)
    comp = zlib.compress(raw, 6)

    def _chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", binascii.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", comp) + _chunk(b"IEND", b""))


_TILE_LOCKS = {}
_TILE_LOCKS_GUARD = threading.Lock()


TILE_PX = 512


def _tile_paths(srv, dim_id, rx, rz):
    dd = os.path.join(srv.data_dir, "tiles", dim_id)
    return dd, os.path.join(dd, f"r.{rx}.{rz}.{TILE_PX}.png")


def mca_zero_chunks(fp, chunk_idxs):
    """Pone a cero las entradas de los chunks dados en la cabecera del .mca.
    Devuelve la cantidad de entradas que realmente cambiaron."""
    idxs = sorted({int(i) for i in chunk_idxs if 0 <= int(i) < 1024})
    if not idxs:
        return 0
    with open(fp, "r+b") as f:
        head = f.read(8192)
        if len(head) < 8192:
            raise ValueError("Archivo .mca incompleto")
        loc = bytearray(head[:4096])
        tim = bytearray(head[4096:8192])
        changed = 0
        for i in idxs:
            o = i * 4
            if loc[o:o + 4] != b"\x00\x00\x00\x00":
                loc[o:o + 4] = b"\x00\x00\x00\x00"
                tim[o:o + 4] = b"\x00\x00\x00\x00"
                changed += 1
        if changed:
            f.seek(0)
            f.write(bytes(loc))
            f.write(bytes(tim))
    return changed


def mca_world_dirs(srv):
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    return wdir, {
        "overworld": os.path.join(wdir, "region"),
        "nether": os.path.join(wdir, "DIM-1", "region"),
        "end": os.path.join(wdir, "DIM1", "region")
    }


def mca_delete_chunks(dim_id, chunks):
    """Borra chunks sueltos (requiere servidor detenido). chunks: [{file, cx, cz}]."""
    srv = S()
    if _srv_running(srv):
        return False, "Detén el servidor antes de borrar chunks (los archivos del mundo están en uso)"
    wdir, dim_paths = mca_world_dirs(srv)
    target_dir = dim_paths.get(dim_id)
    if not target_dir or not os.path.isdir(target_dir):
        return False, "Dimensión no encontrada"
    by_file = {}
    for c in chunks or []:
        try:
            fn = os.path.basename(str(c.get("file", "")))
            cx, cz = int(c.get("cx", -1)), int(c.get("cz", -1))
        except (TypeError, ValueError):
            continue
        if not re.match(r"^r\.-?\d+\.-?\d+\.mca$", fn):
            continue
        if not (0 <= cx < 32 and 0 <= cz < 32):
            continue
        by_file.setdefault(fn, set()).add(cz * 32 + cx)
    if not by_file:
        return False, "Sin chunks válidos para borrar"
    import shutil as _sh
    touched, cleared = 0, 0
    tile_dd = os.path.join(srv.data_dir, "tiles", dim_id)
    for fn, idxs in sorted(by_file.items()):
        fp = os.path.join(target_dir, fn)
        if not os.path.isfile(fp):
            continue
        try:
            m0 = re.match(r"^r\.(-?\d+)\.(-?\d+)\.mca$", fn)
            tkey = (srv.id, dim_id, int(m0.group(1)), int(m0.group(2))) if m0 else None
            with _TILE_LOCKS_GUARD:
                tlk = _TILE_LOCKS.setdefault(tkey, threading.Lock()) if tkey else None
            if tlk is None:
                import contextlib as _cl
                tlk = _cl.nullcontext()
            with tlk:
                bak = fp + ".bak"
                try:
                    _sh.copyfile(fp, bak)
                except Exception:
                    pass
                n = mca_zero_chunks(fp, idxs)
                try:
                    m = re.match(r"^r\.(-?\d+)\.(-?\d+)\.mca$", fn)
                    for tp in (os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.png"),
                               os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.{TILE_PX}.png")):
                        try:
                            if os.path.isfile(tp):
                                os.remove(tp)
                        except Exception:
                            pass
                except Exception:
                    pass
            if n:
                touched += 1
                cleared += n
                base = os.path.dirname(target_dir)
                poi_dir = os.path.join(base, "poi")
                ent_dir = os.path.join(base, "entities")
                if dim_id == "overworld":
                    poi_dir = os.path.join(wdir, "poi")
                    ent_dir = os.path.join(wdir, "entities")
                for sub in (poi_dir, ent_dir):
                    sf = os.path.join(sub, fn)
                    if os.path.isfile(sf):
                        try:
                            mca_zero_chunks(sf, idxs)
                        except Exception:
                            pass
        except Exception as e:
            return False, f"Error en {fn}: {e}"
    if not touched:
        return False, "Esos chunks ya estaban vacíos"
    return True, (f"Se borraron {cleared} chunks en {touched} regiones de "
                  f"{dim_id.upper()} (copia .bak guardada junto a cada .mca). "
                  f"Se regenerarán al explorar la zona.")


def render_region_tile(srv, dim_id, rx, rz, size=TILE_PX):
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    dim_paths = {
        "overworld": os.path.join(wdir, "region"),
        "nether": os.path.join(wdir, "DIM-1", "region"),
        "end": os.path.join(wdir, "DIM1", "region")
    }
    fp = os.path.join(dim_paths.get(dim_id, ""), f"r.{rx}.{rz}.mca")
    if not os.path.isfile(fp) or os.path.getsize(fp) < 8192:
        return None
    step = max(1, 512 // size)
    out_w = 512 // step
    try:
        with open(fp, "rb") as f:
            head = f.read(8192)
        if len(head) < 8192:
            return None
        locs = []
        for i in range(1024):
            e = struct.unpack(">I", head[i * 4:(i + 1) * 4])[0]
            locs.append((e >> 8, e & 0xFF) if e >> 8 else (0, 0))
    except Exception:
        return None
    if not any(a for a, _ in locs):
        return None
    px = bytearray(out_w * out_w * 4)
    try:
        with open(fp, "rb") as f:
            for ci, (loc, count) in enumerate(locs):
                if not loc or not count:
                    continue
                try:
                    f.seek(loc * 4096)
                    length = struct.unpack(">I", f.read(4))[0]
                    if length <= 1 or length > 64 * 1024 * 1024:
                        continue
                    ctype = f.read(1)[0]
                    payload = f.read(length - 1)
                    if ctype == 2:
                        raw = zlib.decompress(payload)
                    elif ctype == 1:
                        raw = gzip.decompress(payload)
                    elif ctype == 3:
                        raw = payload
                    else:
                        continue
                    root = _nbt_load(raw)
                except Exception:
                    continue
                try:
                    secs = {}
                    for s in root.get("sections", []) or []:
                        if isinstance(s, dict) and isinstance(s.get("Y"), int):
                            secs[s["Y"]] = s
                    if not secs:
                        continue
                    tables = {yy: _sec_table(s) for yy, s in secs.items()}
                    hm = (root.get("Heightmaps") or {})
                    hmap = hm.get("WORLD_SURFACE") or hm.get("MOTION_BLOCKING")
                    if not hmap:
                        continue
                    uhmap = [v & 0xFFFFFFFFFFFFFFFF for v in hmap]
                    ccx, ccz = ci & 31, ci >> 5
                    for lz in range(0, 16, step):
                        for lx in range(0, 16, step):
                            li, off = (lz * 16 + lx) // 7, ((lz * 16 + lx) % 7) * 9
                            if li >= len(uhmap):
                                continue
                            y = ((uhmap[li] >> off) & 0x1FF) - 64 - 1
                            if y < -64:
                                continue
                            name = _block_name(tables, secs, y, lx, y & 15, lz)
                            r, g, b, a = _terrain_px(name, y, lx, lz)
                            ox = (ccx * 16 + lx) // step
                            oy = (ccz * 16 + lz) // step
                            o = (oy * out_w + ox) * 4
                            px[o] = r
                            px[o + 1] = g
                            px[o + 2] = b
                            px[o + 3] = a
                except Exception:
                    continue
    except Exception:
        return None
    rows = [bytes(px[r * out_w * 4:(r + 1) * out_w * 4]) for r in range(out_w)]
    return _png_encode_rgba(out_w, out_w, rows)


def get_region_tile(srv, dim_id, rx, rz):
    try:
        rx, rz = int(rx), int(rz)
    except (TypeError, ValueError):
        return None, "Coordenadas inválidas"
    if dim_id not in ("overworld", "nether", "end"):
        return None, "Dimensión inválida"
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    dim_paths = {
        "overworld": os.path.join(wdir, "region"),
        "nether": os.path.join(wdir, "DIM-1", "region"),
        "end": os.path.join(wdir, "DIM1", "region")
    }
    fp = os.path.join(dim_paths.get(dim_id, ""), f"r.{rx}.{rz}.mca")
    if not os.path.isfile(fp):
        return None, "La región no existe"
    try:
        src_mtime = os.path.getmtime(fp)
    except Exception:
        return None, "Sin acceso"
    dd, tp = _tile_paths(srv, dim_id, rx, rz)
    try:
        if os.path.isfile(tp) and os.path.getmtime(tp) >= src_mtime:
            with open(tp, "rb") as f:
                return f.read(), None
    except Exception:
        pass
    key = (srv.id, dim_id, rx, rz)
    with _TILE_LOCKS_GUARD:
        lk = _TILE_LOCKS.get(key)
        if lk is None:
            lk = _TILE_LOCKS[key] = threading.Lock()
    with lk:
        try:
            if os.path.isfile(tp) and os.path.getmtime(tp) >= src_mtime:
                with open(tp, "rb") as f:
                    return f.read(), None
        except Exception:
            pass
        png = render_region_tile(srv, dim_id, rx, rz)
        if png is None:
            return None, "Región vacía"
        try:
            os.makedirs(dd, exist_ok=True)
            legacy = os.path.join(dd, f"r.{rx}.{rz}.png")
            try:
                if os.path.isfile(legacy):
                    os.remove(legacy)
            except Exception:
                pass
            tmp = tp + ".tmp"
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, tp)
        except Exception:
            pass
        return png, None


def mca_present_count(fp):
    """Cuenta chunks generados leyendo la cabecera de localizaciones del .mca (8 primeros KB)."""
    try:
        with open(fp, "rb") as f:
            head = f.read(8192)
        if len(head) < 8192:
            return 0
        n = 0
        for i in range(1024):
            entry = struct.unpack(">I", head[i * 4:(i + 1) * 4])[0]
            if entry >> 8:
                n += 1
        return n
    except Exception:
        return 0


def mca_region_detail(dim_id, filename):
    """Devuelve la rejilla 32x32 de chunks (ocupación + timestamp) de una región."""
    srv = S()
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    dim_paths = {
        "overworld": os.path.join(wdir, "region"),
        "nether": os.path.join(wdir, "DIM-1", "region"),
        "end": os.path.join(wdir, "DIM1", "region")
    }
    target_dir = dim_paths.get(dim_id)
    fn = os.path.basename(filename or "")
    if not target_dir or not re.match(r"^r\.-?\d+\.-?\d+\.mca$", fn):
        return None, "Región inválida"
    fp = os.path.join(target_dir, fn)
    if not os.path.isfile(fp):
        return None, "La región no existe"
    try:
        with open(fp, "rb") as f:
            head = f.read(8192)
        if len(head) < 8192:
            return None, "Archivo .mca incompleto"
        grid, times = [], []
        newest = 0
        for i in range(1024):
            entry = struct.unpack(">I", head[i * 4:(i + 1) * 4])[0]
            ts = struct.unpack(">I", head[4096 + i * 4:4100 + i * 4])[0]
            grid.append(1 if entry >> 8 else 0)
            times.append(ts)
            if ts > newest:
                newest = ts
        m = re.match(r"r\.(-?\d+)\.(-?\d+)\.mca", fn)
        return {"file": fn, "x": int(m.group(1)), "z": int(m.group(2)),
                "size": os.path.getsize(fp),
                "mtime": int(os.path.getmtime(fp)),
                "present": sum(grid), "grid": grid,
                "times": times, "newest": newest}, None
    except Exception as e:
        return None, str(e)


def get_world_summary():
    srv = S()
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    if not os.path.isdir(wdir):
        return {"exists": False, "name": wname, "path": wdir}

    dims = [
        {"id": "overworld", "label": "Overworld (Superficie)", "path": os.path.join(wdir, "region")},
        {"id": "nether", "label": "The Nether (Inframundo)", "path": os.path.join(wdir, "DIM-1", "region")},
        {"id": "end", "label": "The End (Fin)", "path": os.path.join(wdir, "DIM1", "region")}
    ]
    
    total_size = 0
    dim_results = []
    for d in dims:
        rp = d["path"]
        mca_files = []
        d_size = 0
        if os.path.isdir(rp):
            for fn in os.listdir(rp):
                if fn.endswith(".mca"):
                    fp = os.path.join(rp, fn)
                    try:
                        sz = os.path.getsize(fp)
                        d_size += sz
                        m = re.match(r"r\.(-?\d+)\.(-?\d+)\.mca", fn)
                        rx, rz = int(m.group(1)), int(m.group(2)) if m else (0, 0)
                        try:
                            mt = int(os.path.getmtime(fp))
                        except Exception:
                            mt = 0
                        present = mca_present_count(fp)
                        mca_files.append({"file": fn, "x": rx, "z": rz,
                                          "size": sz, "mtime": mt,
                                          "present": present})
                    except Exception:
                        pass
        total_size += d_size
        dim_results.append({
            "id": d["id"],
            "label": d["label"],
            "exists": os.path.isdir(rp),
            "size": d_size,
            "regions_count": len(mca_files),
            "regions": sorted(mca_files, key=lambda r: (r["x"], r["z"]))
        })
    
    return {
        "exists": True,
        "name": wname,
        "path": wdir,
        "total_size": total_size,
        "dimensions": dim_results
    }


def mca_delete_regions(dim_id, region_files):
    srv = S()
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    
    dim_paths = {
        "overworld": os.path.join(wdir, "region"),
        "nether": os.path.join(wdir, "DIM-1", "region"),
        "end": os.path.join(wdir, "DIM1", "region")
    }
    target_dir = dim_paths.get(dim_id)
    if not target_dir or not os.path.isdir(target_dir):
        return False, "Dimensión no encontrada"

    deleted = 0
    errors = []
    tile_dd = os.path.join(S().data_dir, "tiles", dim_id)
    for fn in region_files:
        fn = os.path.basename(fn)
        m = re.match(r"^r\.(-?\d+)\.(-?\d+)\.mca$", fn)
        if not m:
            continue
        fp = os.path.join(target_dir, fn)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
                for tp in (os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.png"),
                           os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.{TILE_PX}.png")):
                    try:
                        if os.path.isfile(tp):
                            os.remove(tp)
                    except Exception:
                        pass
                poi_dir = os.path.join(os.path.dirname(target_dir), "poi") if dim_id != "overworld" else os.path.join(wdir, "poi")
                ent_dir = os.path.join(os.path.dirname(target_dir), "entities") if dim_id != "overworld" else os.path.join(wdir, "entities")
                for sub_dir in (poi_dir, ent_dir):
                    sub_f = os.path.join(sub_dir, fn)
                    if os.path.isfile(sub_f):
                        try: os.remove(sub_f)
                        except Exception: pass
        except Exception as e:
            errors.append(f"{fn}: {e}")
    
    msg = f"Se eliminaron {deleted} regiones ({deleted * 1024} chunks) de {dim_id.upper()}."
    if errors:
        msg += f" Hubo {len(errors)} errores."
    return True, msg


def mca_reset_dimension(dim_id):
    srv = S()
    wname = srv.world_name or "world"
    wdir = os.path.join(srv.server_dir, wname)
    if not os.path.isdir(wdir):
        wdir = os.path.join(srv.server_dir, "world")
    
    if dim_id not in ("nether", "end"):
        return False, "Por seguridad, solo se pueden reiniciar dimensiones secundarias (Nether o The End)"
    
    target_dim = os.path.join(wdir, "DIM-1" if dim_id == "nether" else "DIM1")
    if not os.path.isdir(target_dim):
        return False, "La carpeta de la dimensión no existe"
    
    count = 0
    try:
        import shutil
        for item in os.listdir(target_dim):
            p = os.path.join(target_dim, item)
            if os.path.isdir(p):
                shutil.rmtree(p)
                count += 1
            elif os.path.isfile(p):
                os.remove(p)
                count += 1
        try:
            tdd = os.path.join(srv.data_dir, "tiles", dim_id)
            if os.path.isdir(tdd):
                shutil.rmtree(tdd)
        except Exception:
            pass
        return True, f"Dimensión {dim_id.upper()} reiniciada con éxito (se regenerará al entrar un jugador)"
    except Exception as e:
        return False, f"Error reiniciando dimensión: {e}"


def get_player_details(srv, player_name):
    uc_path = os.path.join(srv.server_dir, "usercache.json")
    uuid = None
    if os.path.isfile(uc_path):
        try:
            with open(uc_path) as f:
                uc = json.load(f)
                for entry in uc:
                    if entry.get("name", "").lower() == player_name.lower():
                        uuid = entry.get("uuid")
                        break
        except Exception:
            pass

    stats_data = {}
    if uuid:
        wname = srv.world_name or "world"
        for candidate in (wname, "world", "PrankLindorf"):
            sp = os.path.join(srv.server_dir, candidate, "stats", f"{uuid}.json")
            if os.path.isfile(sp):
                try:
                    with open(sp) as f:
                        stats_data = json.load(f).get("stats", {})
                        break
                except Exception:
                    pass

    custom = stats_data.get("minecraft:custom", {})
    play_time_ticks = custom.get("minecraft:play_time", 0) or custom.get("minecraft:total_world_time", 0)
    deaths = custom.get("minecraft:deaths", 0)
    mob_kills = custom.get("minecraft:mob_kills", 0)
    player_kills = custom.get("minecraft:player_kills", 0)
    walk_cm = custom.get("minecraft:walk_one_cm", 0)
    sprint_cm = custom.get("minecraft:sprint_one_cm", 0)
    fly_cm = custom.get("minecraft:fly_one_cm", 0)
    total_dist_km = round((walk_cm + sprint_cm + fly_cm) / 100000, 2)
    hours = round(play_time_ticks / (20 * 3600), 1)

    coords = None
    level = None
    if _srv_running(srv):
        ok, res = rcon_send("data get entity " + player_name + " Pos")
        if ok and "[" in res and "]" in res:
            try:
                coords_str = res[res.index("[")+1 : res.index("]")]
                coords = [round(float(c.rstrip("d").strip()), 1) for c in coords_str.split(",")]
            except Exception:
                pass
        ok_lvl, res_lvl = rcon_send("experience query " + player_name + " levels")
        if ok_lvl and res_lvl:
            try:
                m = re.search(r"has (\d+) experience levels", res_lvl)
                if m: level = int(m.group(1))
            except Exception:
                pass

    return {
        "ok": True,
        "name": player_name,
        "uuid": uuid,
        "hours_played": hours,
        "deaths": deaths,
        "mob_kills": mob_kills,
        "player_kills": player_kills,
        "km_traveled": total_dist_km,
        "coords": coords,
        "level": level
    }


def get_network_ping(srv):
    def ping_tcp(host, port, timeout=1.2):
        if not host or not port: return None
        t0 = time.time()
        try:
            s = socket.create_connection((host, int(port)), timeout=timeout)
            s.close()
            return round((time.time() - t0) * 1000, 1)
        except Exception:
            return None

    local_mc = ping_tcp("127.0.0.1", srv.mc_port)
    local_rcon = ping_tcp("127.0.0.1", srv.rcon_port)
    
    pub_ip = get_public_ip()
    pub_host, pub_port = None, None
    if pub_ip and ":" in pub_ip:
        pub_host, pub_port = pub_ip.split(":", 1)
    elif pub_ip:
        pub_host, pub_port = pub_ip, srv.mc_port

    tunnel_ping = ping_tcp(pub_host, pub_port) if pub_host else None

    return {
        "ok": True,
        "local_mc_ms": local_mc,
        "local_rcon_ms": local_rcon,
        "tunnel_ms": tunnel_ping,
        "public_target": f"{pub_host}:{pub_port}" if pub_host else None
    }


def handle_spark_action(srv, action):
    if not _srv_running(srv):
        return False, "El servidor debe estar encendido para interactuar con spark"

    if action == "health":
        ok, res = rcon_send("spark health --memory")
        return ok, res or "Sin respuesta de spark"
    elif action == "sampler_start":
        ok, res = rcon_send("spark sampler start --timeout 45")
        return ok, res or "Muestreo spark iniciado"
    elif action == "sampler_stop":
        ok, res = rcon_send("spark sampler stop --upload")
        return ok, res or "Muestreo spark detenido"
    elif action == "tps":
        ok, res = rcon_send("spark tps")
        return ok, res or "Sin respuesta de spark"
    elif action == "gc":
        ok, res = rcon_send("spark gc")
        return ok, res or "Recolección de basura ejecutada"
    return False, "Acción de spark no reconocida"


def get_fleet_status():
    fleet = []
    for sid, s in SERVERS.items():
        is_run = _srv_running(s)
        fleet.append({
            "id": s.id,
            "name": s.name,
            "subtitle": s.subtitle,
            "server_dir": s.server_dir,
            "mc_port": s.mc_port,
            "max_mem_gb": s.max_mem_gb,
            "running": is_run,
            "current": s.id == S().id
        })
    return {"ok": True, "servers": fleet}


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
    msg = f"Subido(s): {', '.join(saved)}"
    try:
        want_unzip = parse_qs(urlparse(handler.path).query).get("unzip", [""])[0] == "1"
    except Exception:
        want_unzip = False
    if want_unzip:
        import zipfile as _zf
        done = []
        for fname in saved:
            if not fname.lower().endswith(".zip"):
                continue
            zp = os.path.join(dest_dir, fname)
            try:
                with _zf.ZipFile(zp) as z:
                    for m in z.infolist():
                        # anti Zip-Slip
                        rel = os.path.normpath(m.filename).lstrip("/")
                        if rel.startswith("..") or os.path.isabs(m.filename):
                            continue
                        z.extract(m, dest_dir)
                os.remove(zp)
                done.append(fname)
            except Exception as e:
                return handler.send_json({"ok": False, "msg": f"Subido pero falló unzip de {fname}: {e}"}, code=500)
        if done:
            msg += f" · descomprimido(s): {', '.join(done)}"
    return handler.send_json({"ok": True, "msg": msg})


# ══════════════════════════════════════════════════════════════════
# SERVIDOR HTTP API
# ══════════════════════════════════════════════════════════════════

LOGIN_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PKHosting — Acceso</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;color:#f5f3ff;
font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Inter',system-ui,sans-serif;
background:radial-gradient(900px 480px at 12% -8%,rgba(124,58,237,.22),transparent 65%),radial-gradient(760px 520px at 88% 4%,rgba(168,85,247,.16),transparent 60%),#050508}
.card{width:min(380px,92vw);padding:32px 28px;border-radius:24px;background:linear-gradient(155deg,rgba(255,255,255,.09),rgba(255,255,255,.02) 55%,rgba(168,85,247,.06));border:1px solid rgba(255,255,255,.12);box-shadow:inset 0 1px 0 rgba(255,255,255,.16),0 12px 40px rgba(0,0,0,.55);-webkit-backdrop-filter:blur(22px);backdrop-filter:blur(22px);text-align:center}
.login-logo{width:64px;height:64px;margin:0 auto 16px;border-radius:16px;box-shadow:0 8px 28px rgba(168,85,247,.45)}
.login-logo img{width:100%;height:100%;display:block;border-radius:16px}
h1{font-size:22px;letter-spacing:-.5px}h1 span span.g-letter{background:linear-gradient(90deg,#c084fc,#a855f7);-webkit-background-clip:text;background-clip:text;color:transparent}
p{font-size:13px;color:#a89fc7;margin:6px 0 18px}
input{width:100%;background:#06060b;border:1px solid #2b2440;border-radius:12px;padding:11px 13px;color:#fff;font-size:14px;outline:none;margin-bottom:10px}
input:focus{border-color:rgba(168,85,247,.6);box-shadow:0 0 0 3px rgba(168,85,247,.22)}
button{width:100%;border:0;border-radius:12px;padding:11px;font-size:14px;font-weight:700;color:#fff;cursor:pointer;background:linear-gradient(135deg,#a855f7,#7c3aed);box-shadow:0 4px 16px rgba(168,85,247,.4)}
#err{color:#f87171;font-size:12.5px;min-height:18px;margin-top:8px}
</style></head><body>
<div class="card">
<div class="login-logo"><img src="/icon.svg" alt="PKHosting"></div>
<h1 class="brand-grav"><span class="g-letter">P</span><span class="g-letter">K</span><span><span class="g-letter">H</span><span class="g-letter">o</span><span class="g-letter">s</span><span class="g-letter">t</span><span class="g-letter">i</span><span class="g-letter">n</span><span class="g-letter">g</span></span></h1><p>Introduce la contraseña del panel</p>
<input type="password" id="pw" placeholder="Contraseña" autofocus>
<button onclick="login()">Entrar</button><div id="err"></div></div>
<script>
async function login(){
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  const d = await r.json();
  if(d.ok) location.reload(); else document.getElementById('err').textContent = d.msg || 'Error';
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""

PUBLIC_PATHS = ("/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/icon.svg")


class PKHostingPanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        super().end_headers()

    def _cookie_token(self):
        try:
            for part in (self.headers.get("Cookie", "") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k.strip() == "pksess":
                    return v.strip()
        except Exception:
            pass
        return None

    def authed(self):
        if not auth_enabled():
            return True
        return valid_session(self._cookie_token())

    def _need_auth(self):
        if self.authed():
            return False
        if self.path == "/" or self.path.startswith("/index.html"):
            try:
                body = LOGIN_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            try:
                self.send_json({"ok": False, "error": "login requerido"}, code=401)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return True

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

    def _select_server(self, u, payload=None):
        sid = None
        qs = parse_qs(u.query)
        if "server" in qs and qs["server"]:
            sid = qs["server"][0]
        elif isinstance(payload, dict) and payload.get("server"):
            sid = payload["server"]
        if sid is None:
            _ctx.srv = next(iter(SERVERS.values()))
        elif sid in SERVERS:
            _ctx.srv = SERVERS[sid]
        else:
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path not in PUBLIC_PATHS and self._need_auth():
            return
        if not self._select_server(u):
            return self.send_json({"error": "servidor desconocido"}, code=404)
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

        if u.path.startswith("/static/"):
            name = os.path.basename(u.path)
            ext = os.path.splitext(name)[1]
            if ext not in STATIC_TYPES or not os.path.isfile(os.path.join(STATIC_DIR, name)):
                return self.send_json({"error": "no existe"}, code=404)
            try:
                body = render_static(name).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", STATIC_TYPES[ext])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
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

        if u.path == "/api/browse":
            qs = parse_qs(u.query)
            raw = ((qs.get("path", [""]) or [""])[0] or "").strip() or "~"
            p = os.path.realpath(os.path.expanduser(raw))
            if not os.path.isdir(p):
                return self.send_json({"ok": False, "msg": "No existe o sin acceso"}, code=404)
            try:
                kids = [n for n in os.listdir(p)
                        if not n.startswith(".") and os.path.isdir(os.path.join(p, n))]
            except PermissionError:
                return self.send_json({"ok": False, "msg": "Sin permiso"}, code=403)
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)
            dirs = []
            for n in sorted(kids, key=str.lower)[:500]:
                full = os.path.join(p, n)
                ready = os.path.isfile(os.path.join(full, "start.sh")) or \
                    os.path.isfile(os.path.join(full, "server.properties"))
                dirs.append({"name": n, "ready": ready})
            parent = os.path.dirname(p) if p != "/" else "/"
            return self.send_json({"ok": True, "path": p, "parent": parent,
                                   "dirs": dirs, "home": os.path.expanduser("~")})

        if u.path == "/api/servers":
            cur = S().id
            return self.send_json({"servers": [
                {"id": s.id, "name": s.name, "mc_port": s.mc_port,
                 "rcon_port": s.rcon_port, "max_mem_gb": s.max_mem_gb,
                 "server_dir": s.server_dir, "running": _srv_running(s),
                 "current": s.id == cur} for s in SERVERS.values()],
                "auth": auth_enabled()})

        if u.path == "/manifest.webmanifest":
            return self.send_json(PWA_MANIFEST)

        if u.path == "/sw.js":
            body = PWA_SW.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/icon.svg":
            body = PWA_ICON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/backups":
            return self.send_json(backups_status())

        if u.path == "/api/public-ip":
            return self.send_json({"public_ip": get_public_ip()})

        if u.path == "/api/tunnels":
            return self.send_json({"public_ip": get_public_ip(), "playit": get_playit_status()})

        if u.path == "/api/schedules":
            return self.send_json({"ok": True, "tasks": _load_schedules(S()),
                "kinds": ["command", "message", "save", "restart"]})

        if u.path == "/api/discord":
            return self.send_json({"ok": True, "config": discord_config()})

        if u.path == "/api/lists":
            return self.send_json({"ok": True, **moderation_lists()})

        if u.path == "/api/connections":
            return self.send_json({"ok": True, "items": connection_history()})

        if u.path == "/api/activity":
            srv = S()
            days = []
            try:
                n_days = max(1, min(30, int(parse_qs(u.query).get("days", ["14"])[0])))
            except Exception:
                n_days = 14
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            joins = {}
            try:
                with open(_conn_file(srv)) as f:
                    for line in f:
                        try:
                            o = json.loads(line)
                            if o.get("ev") == "join":
                                joins[datetime.datetime.fromtimestamp(
                                    o["t"]).strftime("%Y-%m-%d")] = joins.get(
                                    datetime.datetime.fromtimestamp(
                                        o["t"]).strftime("%Y-%m-%d"), 0) + 1
                        except Exception:
                            continue
            except Exception:
                pass
            bks = {}
            if HAVE_BACKUP:
                try:
                    for i in bk.list_backups(srv.backup_dir):
                        bks[datetime.datetime.fromtimestamp(
                            i["mtime"]).strftime("%Y-%m-%d")] = bks.get(
                            datetime.datetime.fromtimestamp(
                                i["mtime"]).strftime("%Y-%m-%d"), 0) + 1
                except Exception:
                    pass
            for d in range(n_days - 1, -1, -1):
                day = today - datetime.timedelta(days=d)
                k = day.strftime("%Y-%m-%d")
                days.append({"date": day.strftime("%d/%m"), "joins": joins.get(k, 0),
                             "backups": bks.get(k, 0)})
            return self.send_json({"ok": True, "days": days})

        if u.path == "/api/crashes":
            qs = parse_qs(u.query)
            name = qs.get("name", [""])[0]
            srv = S()
            if name:
                d = dg.crash_detail(srv.server_dir, name)
                if d is None:
                    return self.send_json({"ok": False, "msg": "no existe"}, code=404)
                return self.send_json({"ok": True, "crash": d})
            g = srv.crash_guard
            return self.send_json({"ok": True, "items": dg.crash_list(srv.server_dir),
                                   "guard": {"enabled": srv.crash_guard_on, "blocked": srv.crash_blocked,
                                             "max": g.max_crashes, "window_min": g.window_s // 60, "restart": srv.crash_restart,
                                             "recent": len(g.recent()), "last": srv.crash_last}})

        if u.path == "/api/jvm":
            return self.send_json({"ok": True, **dg.jvm_read(S().server_dir)})

        if u.path == "/api/props":
            return self.send_json({"ok": True, **dg.props_read(S().server_dir)})

        if u.path == "/api/logs":
            qs = parse_qs(u.query)
            try:
                limit = max(50, min(3000, int(qs.get("limit", ["600"])[0])))
            except ValueError:
                limit = 600
            if qs.get("list"):
                return self.send_json({"ok": True, "files": dg.logs_list(S().server_dir)})
            r = dg.logs_read(S().server_dir, qs.get("name", ["latest.log"])[0],
                             qs.get("level", ["ALL"])[0], qs.get("q", [""])[0], limit,
                             regex=qs.get("regex", ["0"])[0] == "1")
            if r is None:
                return self.send_json({"ok": False, "msg": "log no encontrado"}, code=404)
            return self.send_json({"ok": True, **r})

        if u.path == "/api/logs/download":
            qs = parse_qs(u.query)
            name = qs.get("name", ["latest.log"])[0]
            if name not in {x["name"] for x in dg.logs_list(S().server_dir)}:
                return self.send_json({"ok": False, "msg": "log no encontrado"}, code=404)
            try:
                with open(os.path.join(dg.logs_dir(S().server_dir), name), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                return self.send_json({"ok": False, "msg": "no se pudo leer"}, code=500)
            return

        if u.path == "/api/mods":
            return self.send_json({"ok": True, "mods": list_mods()})

        if u.path == "/api/backup-download":
            qs = parse_qs(u.query)
            name = os.path.basename(qs.get("name", [""])[0])
            if not HAVE_BACKUP or not name or not bk.FNAME_RE.match(name):
                self.send_response(404)
                self.end_headers()
                return
            target = os.path.join(S().backup_dir, name)
            if not os.path.isfile(target):
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
                                 f'attachment; filename="{name}"')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        if u.path == "/api/backup-cfg":
            return self.send_json({"ok": True, "config": backup_cfg_get(S())})

        if u.path == "/api/uiconfig":
            return self.send_json({"ok": True, "quick_commands":
                CFG.get("quick_commands") or ["say ¡Hola!", "list", "save-all"]})

        if u.path == "/api/history":
            return self.send_json({"ok": True, **history_read(S())})

        if u.path == "/api/world":
            return self.send_json(get_world_summary())

        if u.path == "/api/world/region":
            qs = parse_qs(u.query)
            dim = (qs.get("dimension", ["overworld"])[0] or "overworld").strip()
            fn = (qs.get("file", [""])[0] or "").strip()
            data, err = mca_region_detail(dim, fn)
            if err:
                return self.send_json({"ok": False, "msg": err}, code=404)
            return self.send_json({"ok": True, **data})

        if u.path == "/api/world/tile":
            qs = parse_qs(u.query)
            dim = (qs.get("dimension", ["overworld"])[0] or "overworld").strip()
            png, err = get_region_tile(S(), dim, qs.get("rx", [""])[0],
                                       qs.get("rz", [""])[0])
            if err or png is None:
                return self.send_json({"ok": False, "msg": err or "Sin tile"}, code=404)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "public, max-age=600")
                self.end_headers()
                try:
                    self.wfile.write(png)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except Exception:
                pass
            return

        if u.path == "/api/modrinth/search":
            qs = parse_qs(u.query)
            q = qs.get("q", [""])[0]
            ok, res = search_modrinth(q)
            return self.send_json({"ok": ok, "hits": res if ok else [], "error": None if ok else res})

        if u.path == "/api/player-info":
            qs = parse_qs(u.query)
            name = qs.get("name", [""])[0]
            return self.send_json(get_player_details(S(), name))

        if u.path == "/api/network-ping":
            return self.send_json(get_network_ping(S()))

        if u.path == "/api/fleet":
            return self.send_json(get_fleet_status())

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/upload":
            # binario: no pre-leer el cuerpo como texto
            if u.path not in PUBLIC_PATHS and self._need_auth():
                return
            if not self._select_server(u):
                return self.send_json({"error": "servidor desconocido"}, code=404)
            qs = parse_qs(u.query)
            return handle_upload(self, qs.get("path", [""])[0])
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        if u.path == "/api/login":
            pw = payload.get("password", "")
            if auth_enabled() and verify_password(pw, CFG.get("panel_password_hash", "")):
                tok = new_session()
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"pksess={tok}; HttpOnly; Path=/; SameSite=Lax")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            time.sleep(1)
            return self.send_json({"ok": False, "msg": "Contraseña incorrecta"}, code=401)

        if u.path not in PUBLIC_PATHS and self._need_auth():
            return
        if not self._select_server(u, payload):
            return self.send_json({"error": "servidor desconocido"}, code=404)
        if u.path == "/api/logout":
            drop_session(self._cookie_token())
            return self.send_json({"ok": True, "auth": auth_enabled()})

        if u.path == "/api/password":
            cur, new = payload.get("current", ""), payload.get("new", "")
            if not auth_enabled():
                if len(new) < 8:
                    return self.send_json({"ok": False, "msg": "Mínimo 8 caracteres"})
                CFG["panel_password_hash"] = hash_password(new)
                save_panel_setting("panel_password_hash", CFG["panel_password_hash"])
                return self.send_json({"ok": True, "msg": "Contraseña creada. Serás redirigido al login.", "login": True})
            if not verify_password(cur, CFG.get("panel_password_hash", "")):
                return self.send_json({"ok": False, "msg": "Actual incorrecta"})
            if len(new) < 8:
                return self.send_json({"ok": False, "msg": "Mínimo 8 caracteres"})
            CFG["panel_password_hash"] = hash_password(new)
            save_panel_setting("panel_password_hash", CFG["panel_password_hash"])
            return self.send_json({"ok": True, "msg": "Contraseña actualizada"})

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

        if u.path == "/api/tps":
            if find_running_mc_pid() is None:
                return self.send_json({"ok": False, "msg": "El servidor está apagado"})
            started = request_tps_sample()
            return self.send_json({"ok": True,
                "msg": "Muestreando TPS..." if started
                       else "Ya hay un muestreo en curso"})

        if u.path == "/api/schedules":
            srv = S()
            tasks = _load_schedules(srv)
            return self.send_json({"ok": True, "tasks": tasks,
                "kinds": ["command", "message", "save", "restart"]})

        if u.path == "/api/schedule":
            srv = S()
            action = payload.get("action", "")
            tasks = _load_schedules(srv)
            if action == "create":
                kind = payload.get("kind", "command")
                if kind not in ("command", "message", "save", "restart"):
                    return self.send_json({"ok": False, "msg": "Tipo inválido"})
                t = {"id": secrets.token_hex(6),
                     "name": (payload.get("name") or kind)[:60],
                     "kind": kind, "payload": (payload.get("payload") or "")[:300],
                     "mode": "daily" if payload.get("mode") != "interval" else "interval",
                     "at": payload.get("at") or "04:00",
                     "every_min": max(5, int(payload.get("every_min") or 60)),
                     "warn_min": max(0, min(30, int(payload.get("warn_min") or 0))),
                     "enabled": True, "last_run": 0}
                tasks.append(t)
                _save_schedules(srv, tasks)
                return self.send_json({"ok": True, "msg": "Tarea creada"})
            tid = payload.get("id", "")
            t = next((x for x in tasks if x.get("id") == tid), None)
            if not t:
                return self.send_json({"ok": False, "msg": "No existe"})
            if action == "toggle":
                t["enabled"] = not t.get("enabled", True)
            elif action == "delete":
                tasks = [x for x in tasks if x.get("id") != tid]
            elif action == "run":
                threading.Thread(target=_exec_task, args=(srv, dict(t)),
                                 daemon=True).start()
                return self.send_json({"ok": True, "msg": "Ejecutando ahora"})
            else:
                return self.send_json({"ok": False, "msg": "Acción inválida"})
            _save_schedules(srv, tasks)
            return self.send_json({"ok": True, "msg": "OK"})

        if u.path == "/api/discord":
            CFG["discord_webhook"] = (payload.get("webhook") or "").strip()[:300]
            ev = payload.get("events") or {}
            CFG["discord_events"] = {k: bool(ev.get(k)) for k in
                                     ("up", "down", "tps", "backup", "join", "leave")}
            save_panel_setting("discord_webhook", CFG["discord_webhook"])
            save_panel_setting("discord_events", CFG["discord_events"])
            return self.send_json({"ok": True, "msg": "Discord guardado"})

        if u.path == "/api/discord-test":
            ok, msg = discord_send("✅ PKHosting: prueba de webhook OK")
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/uiconfig":
            v = payload.get("quick_commands", [])
            v = [str(x).strip()[:120] for x in v if str(x).strip()][:12]
            CFG["quick_commands"] = v
            save_panel_setting("quick_commands", v)
            return self.send_json({"ok": True, "msg": "Botones guardados"})

        if u.path == "/api/whitelist":
            on = bool(payload.get("on", False))
            ok, msg = send_command_action(f"whitelist {'on' if on else 'off'}")
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/servers":
            action = str(payload.get("action", ""))
            if action == "add":
                vals, err = _parse_srv_fields(payload)
                if err:
                    return self.send_json({"ok": False, "msg": err}, code=400)
                base = _slugify(vals["name"])
                sid, i = base, 2
                while sid in SERVERS:
                    sid = f"{base}-{i}"
                    i += 1
                entry = {"id": sid, "server_name": vals["name"],
                         "server_subtitle": "Minecraft Server",
                         "server_dir": vals["server_dir"],
                         "start_cmd": ["bash", "start.sh"],
                         "mc_port": vals["mc_port"], "rcon_port": vals["rcon_port"],
                         "max_mem_gb": vals["max_mem_gb"], "backup_enabled": False}
                try:
                    obj = Server(entry)
                except Exception as e:
                    return self.send_json({"ok": False, "msg": str(e)}, code=500)
                _ensure_servers_list().append(entry)
                _persist_servers()
                SERVERS[sid] = obj
                return self.send_json({"ok": True, "msg": f"Servidor '{vals['name']}' añadido", "id": sid})
            if action == "update":
                sid = str(payload.get("id", ""))
                obj = SERVERS.get(sid)
                if obj is None:
                    return self.send_json({"ok": False, "msg": "Servidor desconocido"}, code=404)
                entry, top = _cfg_server_entry(sid)
                if entry is None:
                    return self.send_json({"ok": False, "msg": "Servidor desconocido"}, code=404)
                vals, err = _parse_srv_fields(payload, exclude_id=sid)
                if err:
                    return self.send_json({"ok": False, "msg": err}, code=400)
                if _srv_running(obj) and (os.path.realpath(vals["server_dir"]) != os.path.realpath(obj.server_dir)
                        or vals["mc_port"] != obj.mc_port
                        or vals["rcon_port"] != obj.rcon_port
                        or vals["max_mem_gb"] != obj.max_mem_gb):
                    return self.send_json({"ok": False, "msg": "Detén el servidor para cambiar carpeta, puertos o RAM (el nombre sí se puede)"}, code=400)
                changes = {"server_name": vals["name"], "server_dir": vals["server_dir"],
                           "mc_port": vals["mc_port"], "rcon_port": vals["rcon_port"],
                           "max_mem_gb": vals["max_mem_gb"]}
                if top:
                    for k, v in changes.items():
                        CFG[k] = v
                        save_panel_setting(k, v)
                else:
                    entry.update(changes)
                    _persist_servers()
                obj.name = vals["name"]
                obj.server_dir = vals["server_dir"]
                obj.mc_port = vals["mc_port"]
                obj.rcon_port = vals["rcon_port"]
                obj.max_mem_gb = vals["max_mem_gb"]
                return self.send_json({"ok": True, "msg": f"Servidor '{vals['name']}' actualizado"})
            if action == "delete":
                sid = str(payload.get("id", ""))
                obj = SERVERS.get(sid)
                if obj is None:
                    return self.send_json({"ok": False, "msg": "Servidor desconocido"}, code=404)
                if len(SERVERS) <= 1:
                    return self.send_json({"ok": False, "msg": "No se puede eliminar el único servidor"}, code=400)
                if _srv_running(obj):
                    return self.send_json({"ok": False, "msg": "Detén el servidor antes de eliminarlo"}, code=400)
                lst = CFG.get("servers")
                if isinstance(lst, list):
                    CFG["servers"] = [e for e in lst if not (isinstance(e, dict) and str(e.get("id", "")) == sid)]
                    _persist_servers()
                del SERVERS[sid]
                nxt = next(iter(SERVERS))
                return self.send_json({"ok": True, "msg": f"Servidor '{obj.name}' eliminado", "switchTo": nxt})
            return self.send_json({"ok": False, "msg": "Acción inválida"}, code=400)

        if u.path == "/api/world/mca":
            action = payload.get("action", "")
            dim = payload.get("dimension", "overworld")
            if action == "delete":
                files = payload.get("files", [])
                ok, msg = mca_delete_regions(dim, files)
                return self.send_json({"ok": ok, "msg": msg})
            elif action == "reset_dimension":
                ok, msg = mca_reset_dimension(dim)
                return self.send_json({"ok": ok, "msg": msg})
            elif action == "delete_chunks":
                ok, msg = mca_delete_chunks(dim, payload.get("chunks", []))
                return self.send_json({"ok": ok, "msg": msg})
            return self.send_json({"ok": False, "msg": "Acción desconocida"}, code=400)

        if u.path == "/api/modrinth/install":
            slug = payload.get("slug", "")
            loader = payload.get("loader", "neoforge")
            game_ver = payload.get("game_ver", "1.21.1")
            ok, msg = install_modrinth_project(slug, loader, game_ver)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/spark":
            action = payload.get("action", "health")
            ok, msg = handle_spark_action(S(), action)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/mod":
            action, name = payload.get("action", ""), payload.get("name", "")
            mods = os.path.join(S().server_dir, "mods")
            if action in ("enable_all", "disable_all"):
                try:
                    n = 0
                    for fn in os.listdir(mods):
                        full = os.path.join(mods, fn)
                        if not os.path.isfile(full):
                            continue
                        if action == "disable_all" and fn.endswith(".jar"):
                            os.rename(full, full + ".disabled")
                            n += 1
                        elif action == "enable_all" and fn.endswith(".jar.disabled"):
                            os.rename(full, full[:-9])
                            n += 1
                    word = "activados" if action == "enable_all" else "desactivados"
                    return self.send_json({"ok": True, "msg": f"{n} mods {word} (requiere reinicio)"})
                except Exception as e:
                    return self.send_json({"ok": False, "msg": str(e)}, code=500)
            base = os.path.basename(name)
            if not base or "/" in name or "\\" in name or ".." in name:
                return self.send_json({"ok": False, "msg": "Nombre inválido"}, code=400)
            on_f = os.path.join(mods, base if base.endswith(".jar") else base + ".jar")
            off_f = on_f + ".disabled"
            try:
                if action == "toggle":
                    if os.path.isfile(on_f):
                        os.rename(on_f, off_f)
                        return self.send_json({"ok": True, "msg": f"{base} desactivado (requiere reinicio)"})
                    elif os.path.isfile(off_f):
                        os.rename(off_f, on_f)
                        return self.send_json({"ok": True, "msg": f"{base} activado (requiere reinicio)"})
                    return self.send_json({"ok": False, "msg": "No existe"}, code=404)
                if action == "delete":
                    target = on_f if os.path.isfile(on_f) else off_f
                    if not os.path.isfile(target):
                        return self.send_json({"ok": False, "msg": "No existe"}, code=404)
                    os.remove(target)
                    return self.send_json({"ok": True, "msg": f"{base} eliminado"})
                return self.send_json({"ok": False, "msg": "Acción inválida"}, code=400)
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)

        if u.path == "/api/crashes":
            srv = S()
            action = payload.get("action", "")
            if action == "delete":
                ok = dg.crash_delete(srv.server_dir, payload.get("name", ""))
                return self.send_json({"ok": ok, "msg": "Crash-report eliminado" if ok else "No existe"})
            if action == "unblock":
                srv.crash_guard.reset()
                srv.crash_blocked = False
                return self.send_json({"ok": True, "msg": "Bucle de crash desbloqueado"})
            if action == "config":
                try:
                    mx = max(1, min(20, int(payload.get("max", 3))))
                    wn = max(1, min(240, int(payload.get("window_min", 10))))
                except (TypeError, ValueError):
                    return self.send_json({"ok": False, "msg": "Valores inválidos"}, code=400)
                srv.crash_guard.configure(mx, wn * 60)
                srv.crash_guard_on = bool(payload.get("enabled", True))
                srv.crash_restart = bool(payload.get("restart", False)) and srv.crash_guard_on
                if not srv.crash_guard_on:
                    srv.crash_blocked = False
                _persist_servers_guard(srv, mx, wn, srv.crash_guard_on, srv.crash_restart)
                return self.send_json({"ok": True, "msg": "Guarda de bucle guardada"})
            return self.send_json({"ok": False, "msg": "Acción inválida"}, code=400)

        if u.path == "/api/jvm":
            ok, msg, warns = dg.jvm_write(S().server_dir, str(payload.get("text", "")))
            return self.send_json({"ok": ok, "msg": msg, "warnings": warns}, code=200 if ok else 400)

        if u.path == "/api/jvm/validate":
            ok, errs, warns = dg.jvm_validate(str(payload.get("text", "")))
            return self.send_json({"ok": ok, "errors": errs, "warnings": warns})

        if u.path == "/api/props":
            ok, msg, needs = dg.props_write(S().server_dir, payload.get("changes") or {})
            return self.send_json({"ok": ok, "msg": msg, "restart": needs}, code=200 if ok else 400)

        if u.path == "/api/backup-cfg":
            ok, msg = backup_cfg_set(S(), payload)
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
                    if target is None or os.path.realpath(target) == S().fs_root:
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
            srv = S()
            if srv.backup_state["running"]:
                return self.send_json({"ok": False, "msg": "ya hay un trabajo en curso: " + srv.backup_state.get("msg", "")})
            threading.Thread(target=_backup_worker, args=(srv, "run"), daemon=True).start()
            return self.send_json({"ok": True, "msg": "Backup iniciado en segundo plano"})

        if u.path == "/api/restore":
            if not HAVE_BACKUP:
                return self.send_json({"ok": False, "msg": "backup.py no disponible"})
            srv = S()
            if srv.backup_state["running"]:
                return self.send_json({"ok": False, "msg": "ya hay un trabajo en curso"})
            name = os.path.basename(payload.get("name", ""))
            if not name or not bk.FNAME_RE.match(name):
                return self.send_json({"ok": False, "msg": "backup inválido"}, code=400)
            threading.Thread(target=_backup_worker, args=(srv, "restore", name), daemon=True).start()
            return self.send_json({"ok": True, "msg": f"Restaurando {name} (detiene el servidor, hace pre-backup y rearranca)..."})

        if u.path == "/api/backup-delete":
            if not HAVE_BACKUP:
                return self.send_json({"ok": False, "msg": "backup.py no disponible"})
            name = os.path.basename(payload.get("name", ""))
            target = os.path.join(S().backup_dir, name)
            if not name or not bk.FNAME_RE.match(name) or not os.path.isfile(target):
                return self.send_json({"ok": False, "msg": "backup inválido"}, code=400)
            try:
                os.remove(target)
                return self.send_json({"ok": True, "msg": f"Eliminado {name}"})
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, code=500)

        self.send_response(404)
        self.end_headers()

PWA_MANIFEST = {"name": "PKHosting", "short_name": "PKHosting",
    "start_url": "/", "display": "standalone",
    "background_color": "#050508", "theme_color": "#0a0a13",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
PWA_SW = """self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
if(u.pathname.startsWith('/api/')){e.respondWith(fetch(e.request));return;}
e.respondWith(fetch(e.request).then(r=>{const c=r.clone();
caches.open('pk1').then(ch=>ch.put(e.request,c)).catch(()=>{});return r;}).catch(()=>caches.match(e.request)));});"""
PWA_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none"><defs><linearGradient id="pk_bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#1f1138"/><stop offset="50%" stop-color="#0e071c"/><stop offset="100%" stop-color="#05020a"/></linearGradient><linearGradient id="pk_top" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#f5d0fe"/><stop offset="35%" stop-color="#d8b4fe"/><stop offset="100%" stop-color="#9333ea"/></linearGradient><linearGradient id="pk_left" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#7e22ce"/><stop offset="100%" stop-color="#3b0764"/></linearGradient><linearGradient id="pk_right" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#581c87"/><stop offset="100%" stop-color="#1e0538"/></linearGradient><linearGradient id="pk_cyan" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#7dd3fc"/><stop offset="100%" stop-color="#38bdf8"/></linearGradient><linearGradient id="pk_purple" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#ffffff"/><stop offset="100%" stop-color="#e9d5ff"/></linearGradient><filter id="pk_glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="1.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs><rect width="64" height="64" rx="15" fill="url(#pk_bg)" stroke="rgba(168,85,247,0.38)" stroke-width="1.2"/><circle cx="32" cy="32" r="24" fill="rgba(168,85,247,0.12)" filter="url(#pk_glow)"/><g transform="translate(0, 1)"><polygon points="32,9 53,21 32,33 11,21" fill="url(#pk_top)"/><path d="M24,17 L40,26 M28,14 L44,23 M17,25 L33,33" stroke="rgba(255,255,255,0.4)" stroke-width="0.8"/><circle cx="32" cy="21" r="2.2" fill="#38bdf8" filter="url(#pk_glow)"/><polygon points="11,21 32,33 32,55 11,43" fill="url(#pk_left)"/><polygon points="32,33 53,21 53,43 32,55" fill="url(#pk_right)"/><g filter="url(#pk_glow)"><path d="M18,31 L23,34 L23,48 L18,45 Z" fill="url(#pk_purple)"/><path d="M23,34 L29,37 L29,43 L23,40 Z" fill="url(#pk_purple)"/><polygon points="23,36 26.5,38 26.5,41 23,39" fill="#7e22ce"/></g><g filter="url(#pk_glow)"><path d="M35,37 L40,34 L40,48 L35,51 Z" fill="url(#pk_cyan)"/><path d="M40,40 L47,35 L48.5,36.5 L42.5,42.5 Z" fill="url(#pk_cyan)"/><path d="M41,41.5 L48.5,47.5 L46.5,49 L39.5,43.5 Z" fill="url(#pk_cyan)"/></g><polyline points="11,21 32,9 53,21" stroke="rgba(255,255,255,0.65)" stroke-width="0.9"/><line x1="32" y1="33" x2="32" y2="55" stroke="rgba(192,132,252,0.5)" stroke-width="0.9"/><polyline points="11,21 11,43 32,55 53,43 53,21" stroke="rgba(168,85,247,0.3)" stroke-width="0.8"/></g></svg>"""

if __name__ == "__main__":
    bind = CFG.get("bind", "127.0.0.1")
    server = ThreadingHTTPServer((bind, PORT), PKHostingPanelHandler)
    cert, key = CFG.get("ssl_cert"), CFG.get("ssl_key")
    scheme = "http"
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"[PKHosting] Ejecutando en {scheme}://{bind}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
