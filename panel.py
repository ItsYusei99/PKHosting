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
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(1.5)

threading.Thread(target=metrics_worker, daemon=True).start()

def start_server_action():
    s = S()
    with s.lock:
        current_pid = find_running_mc_pid()
        if current_pid is not None:
            return False, f"El servidor ya está en ejecución (PID {current_pid})"
        try:
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


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
        restart_server_action()
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

HTML_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PKHosting Panel — PrankLindorf</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0a0a13">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{});}</script>
<script>window.onerror=function(){/* noop: sin telemetría */};</script>
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
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border-radius: 11px;
  box-shadow: 0 4px 16px rgba(168,85,247,0.35);
  transition: transform 0.25s var(--ease-apple), box-shadow 0.25s;
}
.brand-icon:hover {
  transform: scale(1.08) rotate(-2deg);
  box-shadow: 0 6px 22px rgba(168,85,247,0.55);
}
.brand-icon img, .brand-icon svg {
  width: 100%;
  height: 100%;
  display: block;
  border-radius: 11px;
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
.logout-btn {
  width: 100%;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  background: transparent; border: 1px solid var(--border-color); border-radius: 8px;
  color: var(--text-muted); font-family: inherit; font-size: 12.5px; font-weight: 600;
  padding: 9px 12px; margin-bottom: 10px; cursor: pointer;
  transition: all 0.2s var(--ease-apple);
}
.logout-btn svg { width: 15px; height: 15px; }
.logout-btn:hover { color: #fca5a5; border-color: rgba(248,113,113,0.5); background: rgba(248,113,113,0.08); }

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

/* POWER BUTTONS */
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
/* Cuadros de texto */
.tin { background: var(--bg-terminal); border: 1px solid var(--border-color); border-radius: 10px; padding: 9px 12px; color: #fff; font-family: 'JetBrains Mono', monospace; font-size: 12.5px; outline: none; caret-color: #c084fc; transition: border-color 0.15s, box-shadow 0.15s, background 0.15s; }
.tin::placeholder { color: var(--text-dim); }
.tin:hover { border-color: rgba(168,85,247,0.45); }
.tin:focus { background: rgba(168,85,247,0.05); }
#logSearch { width: 110px; padding: 5px 10px; font-size: 11px; border-radius: 8px; transition: width 0.22s var(--ease-apple), border-color 0.15s, box-shadow 0.15s, background 0.15s; }
#logSearch:focus { width: 165px; }
.cmd-field { flex: 1; display: flex; align-items: center; gap: 10px; background: var(--bg-terminal); border: 1px solid var(--border-color); border-radius: 10px; padding: 9px 12px; transition: border-color 0.15s, box-shadow 0.15s, background 0.15s; }
.cmd-field:hover { border-color: rgba(168,85,247,0.45); }
.cmd-field:focus-within { border-color: rgba(168,85,247,0.6); box-shadow: 0 0 0 3px rgba(168,85,247,0.22), 0 0 18px rgba(168,85,247,0.15); background: rgba(168,85,247,0.04); }
.cmd-field .cmd-input { caret-color: #c084fc; }
.cmd-prompt { text-shadow: 0 0 8px rgba(34,211,238,0.7); }

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
.seg { display: inline-flex; background: var(--bg-terminal); border: 1px solid var(--border-color); border-radius: 9px; padding: 3px; gap: 2px; }
.seg button { background: transparent; border: 0; color: var(--text-muted); font-size: 12px; font-weight: 600; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-family: inherit; transition: all 0.15s; }
.seg button:hover { color: var(--text-main); }
.seg button.on { background: linear-gradient(135deg, #a855f7, #7c3aed); color: #fff; box-shadow: 0 2px 10px rgba(168,85,247,0.4); }
.btn-ghost { display: inline-flex; align-items: center; gap: 6px; background: rgba(168,85,247,0.1); border: 1px solid rgba(168,85,247,0.4); color: #d8b4fe; font-size: 12px; font-weight: 600; padding: 7px 14px; border-radius: 8px; cursor: pointer; font-family: inherit; transition: all 0.15s; }
.btn-ghost:hover { background: rgba(168,85,247,0.25); color: #fff; border-color: #a855f7; }

/* MODS: gestor mejorado */
.mods-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.mods-head h3 { font-size: 14px; margin: 0; }
.mods-count { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 700; color: var(--text-muted); background: rgba(255,255,255,0.04); border: 1px solid var(--border-color); border-radius: 999px; padding: 4px 12px; }
.mods-count .dot { width: 7px; height: 7px; border-radius: 50%; }
.mods-count .dot.on { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.9); }
.mods-count .dot.off { background: #64748b; }
.mod-search { flex: 1; min-width: 130px; max-width: 240px; background: var(--bg-terminal); border: 1px solid var(--border-color); border-radius: 8px; color: var(--text-main); font-size: 12.5px; padding: 7px 12px; font-family: inherit; outline: none; transition: border-color 0.15s, box-shadow 0.15s; }
.mod-search:focus { border-color: #a855f7; box-shadow: 0 0 0 3px rgba(168,85,247,0.18); }
.mod-search::placeholder { color: var(--text-dim); }
.mod-row { display: flex; align-items: center; gap: 12px; padding: 10px 18px; border-bottom: 1px solid var(--border-subtle); font-size: 13px; transition: background 0.2s var(--ease-apple), box-shadow 0.2s, opacity 0.2s; }
.mod-row:last-child { border-bottom: 0; }
.mod-row:hover { background: rgba(168,85,247,0.07); box-shadow: inset 2px 0 0 #a855f7; }
.mod-row.is-off { opacity: 0.72; }
.mod-row.is-off:hover { opacity: 1; }
.mod-ico { display: inline-flex; flex-shrink: 0; color: #4ade80; filter: drop-shadow(0 0 6px rgba(34,197,94,0.45)); transition: color 0.2s, filter 0.2s, transform 0.2s; }
.mod-ico svg { width: 20px; height: 20px; }
.mod-row:hover .mod-ico { transform: scale(1.12); }
.mod-row.is-off .mod-ico { color: #64748b; filter: none; }
.mod-main { flex: 1; min-width: 0; }
.mod-name { display: block; font-family: 'JetBrains Mono', monospace; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mod-meta { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: 11px; color: var(--text-dim); }
.mchip { font-size: 10px; font-weight: 800; letter-spacing: 0.03em; padding: 1px 8px; border-radius: 999px; border: 1px solid; white-space: nowrap; }
.mchip.ver { color: #93c5fd; border-color: rgba(147,197,253,0.35); background: rgba(147,197,253,0.08); }
.mchip.ld { color: #d8b4fe; border-color: rgba(216,180,254,0.35); background: rgba(168,85,247,0.1); }
.mod-state { font-size: 10.5px; font-weight: 800; letter-spacing: 0.08em; width: 28px; text-align: right; flex-shrink: 0; }
.mod-state.s-on { color: #4ade80; text-shadow: 0 0 8px rgba(34,197,94,0.6); }
.mod-state.s-off { color: #64748b; }
/* switch deslizante real */
.msw { position: relative; width: 46px; height: 26px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.08); cursor: pointer; flex-shrink: 0; padding: 0; transition: background 0.25s var(--ease-apple), border-color 0.25s, box-shadow 0.25s, filter 0.15s; }
.msw::after { content: ''; position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; border-radius: 50%; background: #fff; box-shadow: 0 2px 6px rgba(0,0,0,0.45); transition: transform 0.28s cubic-bezier(0.34,1.65,0.4,1); }
.msw:hover { filter: brightness(1.15); }
.msw:focus-visible { outline: 2px solid #a855f7; outline-offset: 2px; }
.msw[aria-checked="true"] { background: linear-gradient(135deg, #22c55e, #15803d); border-color: rgba(34,197,94,0.6); box-shadow: 0 0 12px rgba(34,197,94,0.35), inset 0 1px 2px rgba(255,255,255,0.25); }
.msw[aria-checked="true"]::after { transform: translateX(20px); }
/* iconos por extension en archivos + filas */
.file-row { cursor: default; box-shadow: inset 2px 0 0 transparent; }
.file-row:hover { box-shadow: inset 2px 0 0 rgba(168,85,247,0.6); }
.file-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-row:hover .file-name { color: #fff; }
.file-icon.is-json { color: #67e8f9; }
.file-icon.is-cfg { color: #fbbf24; }
.file-icon.is-log { color: #64748b; }
.file-icon.is-jar { color: #c084fc; filter: drop-shadow(0 0 6px rgba(168,85,247,0.45)); }
.file-icon.is-zip { color: #fb923c; }
.file-icon.is-txt { color: #4ade80; }
/* switches de opcion (reemplazan checkboxes nativos) */
.opt-sw { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.opt-lbl { font-size: 12px; color: var(--text-muted); transition: color 0.15s; }
.opt-sw:has(.tsw.on) .opt-lbl { color: var(--text-main); }
.tsw { position: relative; width: 38px; height: 22px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.08); flex-shrink: 0; transition: background 0.25s var(--ease-apple), border-color 0.25s, box-shadow 0.25s, filter 0.15s; }
.tsw::after { content: ''; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #8b93a7; transition: transform 0.25s cubic-bezier(0.34,1.65,0.4,1), background 0.2s; }
.tsw.on { background: linear-gradient(135deg, #22c55e, #15803d); border-color: rgba(34,197,94,0.6); box-shadow: 0 0 10px rgba(34,197,94,0.3); }
.tsw.on::after { transform: translateX(16px); background: #fff; }
.opt-sw:hover .tsw { filter: brightness(1.18); }
.tsw:focus-visible { outline: 2px solid #a855f7; outline-offset: 2px; }

/* TOASTS estilo Sonner (dark + rail de color, layout flex simple) */
.toaster { position: fixed; z-index: 400; display: flex; flex-direction: column;
  gap: 8px; padding: 12px; pointer-events: none; width: min(380px, calc(100vw - 24px)); }
.toaster[data-pos^="top"] { top: 0; }
.toaster[data-pos^="bottom"] { bottom: 0; }
.toaster[data-pos$="left"] { left: 0; align-items: flex-start; }
.toaster[data-pos$="right"] { right: 0; align-items: flex-end; }
.toaster[data-pos$="center"] { left: 50%; transform: translateX(-50%); align-items: center; }
.toast { --tk: #a855f7; display: flex; align-items: center; gap: 12px; width: 356px;
  max-width: 100%; padding: 14px 14px 14px 13px; border-radius: 12px;
  cursor: pointer; pointer-events: auto; color: #fff;
  background: rgba(18, 13, 30, 0.94);
  border: 1px solid rgba(255, 255, 255, 0.09); border-left: 3px solid var(--tk);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px);
  opacity: 0; transform: translateY(-12px) scale(0.97);
  transition: transform 350ms cubic-bezier(0.32, 0.72, 0, 1), opacity 250ms ease; }
.toaster[data-pos^="bottom"] .toast { transform: translateY(12px) scale(0.97); }
.toast.ready { opacity: 1; transform: none; }
.toast.exiting { opacity: 0; pointer-events: none; }
.toaster[data-pos^="top"] .toast.exiting { transform: translateY(-8px) scale(0.97); }
.toaster[data-pos^="bottom"] .toast.exiting { transform: translateY(8px) scale(0.97); }
.toast[data-state="success"] { --tk: #22c55e; }
.toast[data-state="error"] { --tk: #ef4444; }
.toast[data-state="warning"] { --tk: #f59e0b; }
.toast[data-state="info"] { --tk: #a855f7; }
.toast[data-state="loading"] { --tk: #8b93a7; cursor: default; }
.toast .tk-ico { width: 20px; height: 20px; flex-shrink: 0; display: flex;
  align-items: center; justify-content: center; color: var(--tk); }
.toast .tk-ico svg { width: 20px; height: 20px; }
.toast .tk-main { flex: 1; min-width: 0; }
.toast .tk-title { font-size: 13px; font-weight: 600; color: #f1f5f9;
  line-height: 1.4; word-break: break-word; }
.toast .tk-desc { font-size: 12px; color: var(--text-muted); margin-top: 2px;
  line-height: 1.45; word-break: break-word; }
.toast .tk-act { margin-top: 8px; display: inline-flex; align-items: center;
  height: 28px; padding: 0 12px; border-radius: 9999px; border: 0;
  font-size: 12px; font-weight: 600; font-family: inherit; cursor: pointer;
  color: #fff; background: rgba(255, 255, 255, 0.1); }
.toast .tk-act:hover { background: rgba(255, 255, 255, 0.18); }
.toast .tk-x { flex-shrink: 0; align-self: flex-start; background: none; border: 0;
  color: var(--text-dim); font-size: 15px; line-height: 1; cursor: pointer;
  padding: 2px 5px; border-radius: 6px; }
.toast .tk-x:hover { color: #fff; background: rgba(255, 255, 255, 0.08); }
.toast .tk-spin { animation: sl-spin 1s linear infinite; }
@keyframes sl-spin { to { transform: rotate(360deg); } }

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
  background: transparent;
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

/* ═══ RARE UI · microinteracciones ═══ */
.orbs { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
.orbs i { position: absolute; width: 44vmax; height: 44vmax; border-radius: 50%; filter: blur(90px); opacity: 0.5; }
.orbs i:first-child { left: -12vmax; top: -14vmax; background: radial-gradient(circle, rgba(124,58,237,0.5), transparent 65%); animation: orb-a 14s ease-in-out infinite alternate; }
.orbs i:last-child { right: -14vmax; bottom: -16vmax; background: radial-gradient(circle, rgba(217,70,239,0.35), transparent 65%); animation: orb-b 18s ease-in-out infinite alternate; }
@keyframes orb-a { to { transform: translate(9vmax, 7vmax) scale(1.15); } }
@keyframes orb-b { to { transform: translate(-8vmax, -6vmax) scale(1.1); } }

#scrollProgress { position: fixed; top: 0; left: 0; height: 3px; width: 0%; z-index: 300;
  background: linear-gradient(90deg, #7c3aed, #d946ef); box-shadow: 0 0 12px rgba(168,85,247,0.8); }

.nav-item { --prox: 0; transform: translateX(calc(var(--prox) * 7px));
  background: rgba(168,85,247, calc(var(--prox) * 0.13)); transition: transform 0.18s ease-out, background 0.18s ease-out, color 0.15s; }
.nav-item.active { --prox: 0; transform: none; }

.file-row:hover .file-icon.is-folder svg { transform: scale(1.18) rotate(-5deg); color: #e9d5ff; filter: drop-shadow(0 0 8px rgba(168,85,247,0.7)); }
.file-icon.is-folder svg { transition: transform 0.25s cubic-bezier(0.34, 1.8, 0.4, 1), color 0.2s; }

.g-letter { display: inline-block; transition: transform 0.45s cubic-bezier(0.34, 1.9, 0.4, 1); }
.brand-grav:hover .g-letter { transform: translateY(-7px); }
.brand-grav:hover .g-letter:nth-child(2n) { transform: translateY(5px) rotate(6deg); }
.brand-grav:hover .g-letter:nth-child(3n) { transform: translateY(-10px) rotate(-5deg); transition-delay: 0.03s; }
.brand-grav:hover .g-letter:nth-child(4n) { transition-delay: 0.06s; }

.heat { display: flex; gap: 5px; align-items: flex-end; }
.heat-cell { flex: 1; min-width: 0; border-radius: 5px; background: rgba(168,85,247,0.12);
  border: 1px solid rgba(168,85,247,0.18); position: relative; transition: transform 0.15s; }
.heat-cell:hover { transform: scaleY(1.08); }
.heat-cell.has-bk { border-color: rgba(52,211,153,0.55); }

.dur { display: inline-flex; align-items: stretch; border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; background: var(--bg-terminal); }
.dur button { background: rgba(168,85,247,0.12); color: #d8b4fe; border: 0; width: 30px; font-size: 15px; cursor: pointer; }
.dur button:hover { background: rgba(168,85,247,0.3); }
.dur input { width: 56px; text-align: center; background: transparent; border: 0; color: #fff; font-family: 'JetBrains Mono', monospace; font-size: 12.5px; padding: 9px 2px; outline: none; }

/* ═══ AVATARES & PLAYER INSPECTOR ═══ */
.p-avatar { width: 28px; height: 28px; border-radius: 6px; vertical-align: middle; background: #1a102f; box-shadow: 0 2px 6px rgba(0,0,0,0.45); flex-shrink: 0; image-rendering: pixelated; }
.p-chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px; border-radius: 8px; background: rgba(168,85,247,0.1); border: 1px solid rgba(168,85,247,0.25); color: #e9d5ff; font-size: 12px; cursor: pointer; transition: all 0.15s; }
.p-chip:hover { background: rgba(168,85,247,0.22); border-color: #a855f7; transform: translateY(-1px); }
.p-stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin: 14px 0; }
.p-stat-card { background: var(--bg-terminal); border: 1px solid var(--border-color); border-radius: 10px; padding: 10px; }
.p-stat-val { font-size: 18px; font-weight: 800; color: #c084fc; font-family: 'JetBrains Mono', monospace; }
.p-stat-lbl { font-size: 11px; color: var(--text-dim); text-transform: uppercase; margin-top: 2px; }

/* ═══ COMMAND PALETTE (CTRL+K) ═══ */
#cmdPalette { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(8px); z-index: 999; align-items: flex-start; justify-content: center; padding-top: 12vh; }
.cp-box { width: min(600px, 92vw); background: #0c0817; border: 1px solid rgba(168,85,247,0.4); border-radius: 16px; box-shadow: 0 16px 50px rgba(0,0,0,0.8), 0 0 30px rgba(168,85,247,0.2); overflow: hidden; display: flex; flex-direction: column; }
.cp-input-wrap { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border-color); }
.cp-input { flex: 1; background: transparent; border: 0; outline: none; color: #fff; font-size: 15px; font-family: inherit; }
.cp-input::placeholder { color: var(--text-dim); }
.cp-list { max-height: 380px; overflow-y: auto; padding: 8px; }
.cp-item { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-radius: 10px; cursor: pointer; color: #cbd5e1; font-size: 13px; transition: background 0.15s; }
.cp-item:hover, .cp-item.active { background: rgba(168,85,247,0.18); color: #fff; }
.cp-item .cp-badge { font-size: 10px; font-weight: 700; text-transform: uppercase; background: rgba(255,255,255,0.06); padding: 3px 8px; border-radius: 6px; color: var(--text-muted); }
.cp-footer { padding: 8px 16px; border-top: 1px solid var(--border-color); font-size: 11px; color: var(--text-dim); display: flex; justify-content: space-between; }

/* ═══ TERMINAL FULLSCREEN & TEMAS ═══ */
.terminal-wrapper.fullscreen { position: fixed; inset: 0; z-index: 500; border-radius: 0; margin: 0; height: 100vh; max-height: 100vh; display: flex; flex-direction: column; }
.terminal-wrapper.fullscreen .terminal-body { height: calc(100vh - 120px) !important; max-height: calc(100vh - 120px) !important; }
.term-theme-matrix { --bg-terminal: #020c04; }
.term-theme-matrix .terminal-body { color: #22c55e !important; text-shadow: 0 0 6px rgba(34,197,94,0.4); }
.term-theme-monokai { --bg-terminal: #19181a; }
.term-theme-monokai .terminal-body { color: #ffd866 !important; }
.term-theme-tokyo { --bg-terminal: #13141c; }
.term-theme-tokyo .terminal-body { color: #7aa2f7 !important; }
.term-theme-dracula { --bg-terminal: #1e1a29; }
.term-theme-dracula .terminal-body { color: #50fa7b !important; }

/* ═══ CENTRO DE NOTIFICACIONES ═══ */
.hdr-tools { display: flex; align-items: center; gap: 6px; background: var(--bg-main); border: 1px solid var(--border-color); border-radius: 10px; padding: 4px; }
.tool-btn { position: relative; display: inline-flex; align-items: center; gap: 7px; height: 32px; padding: 0 12px; border-radius: 7px; border: 1px solid transparent; background: transparent; color: var(--text-muted); font-family: inherit; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.15s ease; white-space: nowrap; }
.tool-btn:hover { background: rgba(168,85,247,0.14); border-color: rgba(168,85,247,0.35); color: #e9d5ff; }
.tool-btn:active { transform: scale(0.97); }
.tool-btn:focus-visible { outline: 2px solid var(--accent-soft); outline-offset: 1px; }
.tool-btn svg { width: 15px; height: 15px; flex-shrink: 0; }
.tool-btn kbd { font-family: inherit; font-size: 10.5px; font-weight: 700; line-height: 1; color: var(--text-dim); background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 5px; padding: 3px 6px; }
.tool-btn:hover kbd { color: #e9d5ff; border-color: rgba(168,85,247,0.4); background: rgba(168,85,247,0.12); }
.tool-btn.icon-only { width: 32px; padding: 0; justify-content: center; }
.notif-btn { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 7px; border: 1px solid transparent; background: transparent; color: var(--text-muted); cursor: pointer; transition: all 0.15s ease; }
.notif-btn:hover { background: rgba(168,85,247,0.14); border-color: rgba(168,85,247,0.35); color: #e9d5ff; }
.notif-btn:active { transform: scale(0.97); }
.notif-btn:focus-visible { outline: 2px solid var(--accent-soft); outline-offset: 1px; }
.notif-btn svg { width: 15px; height: 15px; }
.notif-dot { position: absolute; top: 5px; right: 5px; width: 9px; height: 9px; border-radius: 50%; background: var(--accent-soft); border: 2px solid var(--bg-main); box-shadow: 0 0 8px rgba(192,132,252,0.9); display: none; }
#notifDropdown { display: none; position: absolute; top: calc(100% + 8px); right: 0; width: 340px; max-height: 420px; background: #0c0817; border: 1px solid rgba(168,85,247,0.4); border-radius: 14px; box-shadow: 0 14px 40px rgba(0,0,0,0.8); z-index: 350; overflow: hidden; }
.notif-header { padding: 12px 16px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; font-size: 13px; font-weight: 700; color: #fff; }
.notif-list { max-height: 340px; overflow-y: auto; padding: 6px; }
.notif-item { padding: 8px 12px; border-radius: 8px; font-size: 12px; margin-bottom: 4px; display: flex; gap: 8px; align-items: flex-start; color: #cbd5e1; }
.notif-item:hover { background: rgba(168,85,247,0.08); }
.notif-time { font-size: 10.5px; color: var(--text-dim); margin-top: 2px; }

/* ═══ PERFORMANCE LAG BANNER ═══ */
#perfBanner { display: none; background: linear-gradient(90deg, #7f1d1d, #991b1b 50%, #b45309); color: #fff; padding: 8px 16px; font-size: 12.5px; font-weight: 600; align-items: center; justify-content: space-between; box-shadow: 0 4px 14px rgba(0,0,0,0.5); z-index: 280; }

/* ═══ WORLD MANAGER & MCA CLEANER ═══ */
.dim-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 14px; padding: 16px; display: flex; flex-direction: column; justify-content: space-between; transition: all 0.2s var(--ease-apple); }
.dim-card:hover { border-color: rgba(168,85,247,0.5); transform: translateY(-2px); }
.mca-grid { max-height: 380px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 10px; background: var(--bg-terminal); }
.mca-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid var(--border-subtle); font-size: 12.5px; font-family: 'JetBrains Mono', monospace; }
.mca-row:hover { background: rgba(168,85,247,0.08); }

/* ═══ MODRINTH EXPLORER ═══ */
.mr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; margin-top: 12px; }
.mr-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 14px; display: flex; flex-direction: column; justify-content: space-between; transition: all 0.2s; }
.mr-card:hover { border-color: #a855f7; box-shadow: 0 6px 20px rgba(168,85,247,0.25); }
.mr-icon { width: 44px; height: 44px; border-radius: 10px; object-fit: cover; background: #1a102f; flex-shrink: 0; }

@media (prefers-reduced-motion: reduce) {
  .orbs i, .g-letter, .file-icon.is-folder svg, .toast, .toaster { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
}
</style>
</head>
<body>
<div class="orbs" aria-hidden="true"><i></i><i></i></div>
<div id="scrollProgress"></div>

<!-- SIDEBAR -->
<aside>
  <div class="sidebar-header">
    <div class="brand-icon"><img src="/icon.svg" alt="PKHosting"></div>
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
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></span> Métricas
    </a>
    <a class="nav-item" onclick="switchTab('files')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></span> Archivos & Mods
    </a>
    <a class="nav-item" onclick="switchTab('worlds')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></span> Mundos & MCA
    </a>
    <a class="nav-item" onclick="switchTab('backups')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><rect x="1" y="3" width="22" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><line x1="10" y1="12" x2="14" y2="12"/></svg></span> Backups
    </a>
    <a class="nav-item" onclick="switchTab('tasks')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></span> Tareas
    </a>
    <a class="nav-item" onclick="switchTab('history')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><polyline points="12 7 12 12 15 15"/></svg></span> Historial
    </a>
    <a class="nav-item" onclick="switchTab('fleet')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg></span> Flota Servidores
    </a>
    <a class="nav-item" onclick="switchTab('settings')">
      <span class="nav-icon"><svg class="ico" viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg></span> Configuración
    </a>
  </nav>

  <div class="sidebar-footer">
    <button class="logout-btn" onclick="logout()" title="Cerrar sesión"><svg class="ico" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg> Cerrar sesión</button>
    <div class="ip-chip" onclick="copyIp()">
      <span id="ipText">localhost:25566</span>
      <span class="copy-badge" id="copyBadge">Copiar</span>
    </div>
  </div>
</aside>

<!-- MAIN -->
<main>
  <div id="perfBanner">
    <div style="display:flex; align-items:center; gap:8px">
      <span>⚠️ Rendimiento comprometido: <span id="perfBannerMsg">TPS bajo</span></span>
    </div>
    <div style="display:flex; align-items:center; gap:8px">
      <button class="term-tool-btn" style="background:rgba(0,0,0,0.35);color:#fff" onclick="switchTab('metrics')">Ver métricas</button>
      <button class="term-tool-btn" style="background:rgba(0,0,0,0.35);color:#fff" onclick="sparkQuickAction('health')">Diagnóstico Spark</button>
      <button class="icon-btn" onclick="document.getElementById('perfBanner').style.display='none'">✕</button>
    </div>
  </div>

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

    <div style="display:flex; align-items:center; gap:10px; position:relative">
      <div class="hdr-tools" role="toolbar" aria-label="Herramientas del panel">
        <button class="tool-btn" onclick="openCmdPalette()" title="Paleta de comandos (Ctrl+K)">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <kbd>Ctrl K</kbd>
        </button>
        <button class="tool-btn icon-only" id="notifBtn" onclick="toggleNotifDropdown()" title="Centro de notificaciones" aria-label="Centro de notificaciones" aria-haspopup="true" aria-expanded="false">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          <span class="notif-dot" id="notifDot"></span>
        </button>
      </div>

      <div id="notifDropdown" role="dialog" aria-label="Registro de actividad">
        <div class="notif-header">
          <span>Registro de Actividad</span>
          <button class="term-tool-btn" style="font-size:11px" onclick="clearNotifs()">Limpiar</button>
        </div>
        <div class="notif-list" id="notifList">
          <div style="padding:16px;text-align:center;color:var(--text-dim);font-size:12px">Sin notificaciones pendientes</div>
        </div>
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
      </div>
    </div>

    <!-- TAB 1: CONSOLA -->
    <div id="tab-console" class="tab-content active">
      <div class="terminal-wrapper">
        <div class="terminal-topbar">
          <div>Consola del Servidor (Registro en Vivo)</div>
          <div class="terminal-topbar-tools">
            <input id="logSearch" class="tin" placeholder="Buscar…" oninput="renderConsole()">
            <button class="term-tool-btn" id="fltAll" onclick="setLogFilter('')">Todos</button>
            <button class="term-tool-btn" onclick="setLogFilter('INFO')">INFO</button>
            <button class="term-tool-btn" onclick="setLogFilter('WARN')">WARN</button>
            <button class="term-tool-btn" onclick="setLogFilter('ERROR')">ERROR</button>
            <button class="term-tool-btn" onclick="toggleAutoScroll()" id="btnAutoScroll">Auto-scroll: ON</button>
            <button class="term-tool-btn" onclick="copyLogs()">Copiar registro</button>
            <button class="term-tool-btn" onclick="clearTerminal()">Limpiar</button>
            <select class="tin" id="termThemeSel" onchange="setTermTheme(this.value)" style="padding:4px 8px;font-size:11px" title="Tema de la terminal">
              <option value="default">Tema: Morado</option>
              <option value="matrix">Tema: Matrix</option>
              <option value="monokai">Tema: Monokai</option>
              <option value="tokyo">Tema: Tokyo Night</option>
              <option value="dracula">Tema: Dracula</option>
            </select>
            <button class="term-tool-btn" onclick="toggleTermFullscreen()" title="Pantalla completa (Esc para salir)">⛶</button>
          </div>
        </div>
        <div class="terminal-body" id="termBody">Conectando a la consola del servidor...</div>
        <div class="terminal-input-bar">
          <div class="cmd-field">
            <span class="cmd-prompt">&gt;</span>
            <input type="text" class="cmd-input" id="cmdInput" list="cmdList" placeholder="Comandos MC (/say, /op, /list...) + panel: start · stop · restart · reload · kill" onkeydown="handleCmdKey(event)" autocomplete="off">
            <datalist id="cmdList"></datalist>
          </div>
          <button class="cmd-btn" onclick="submitCmd()">Enviar</button>
        </div>
        <div id="quickCmds" style="display:flex; gap:6px; flex-wrap:wrap; padding:8px 14px; border-top:1px solid var(--border-color)"></div>
      </div>
      <div class="system-details-card" id="playerMgmt" style="margin-top:12px; display:none">
        <h3 style="margin-bottom:8px; font-size:15px;">Gestión de jugadores</h3>
        <div class="file-list" id="playerRows"></div>
      </div>
      <div class="system-details-card" style="margin-top:12px">
        <h3 style="margin-bottom:8px; font-size:15px;">Moderación</h3>
        <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:10px" id="modLists">
          <div><div style="font-size:11px;color:var(--text-dim);text-transform:uppercase;margin-bottom:4px">OPs</div><div id="modOps" style="font-size:12.5px">—</div></div>
          <div><div style="font-size:11px;color:var(--text-dim);text-transform:uppercase;margin-bottom:4px">Baneados</div><div id="modBans" style="font-size:12.5px">—</div></div>
          <div><div style="font-size:11px;color:var(--text-dim);text-transform:uppercase;margin-bottom:4px">Whitelist <a onclick="wlToggle()" style="color:var(--accent-cyan);cursor:pointer" id="wlState"></a></div><div id="modWl" style="font-size:12.5px">—</div></div>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input id="wlName" class="tin" maxlength="16" placeholder="Añadir a whitelist" style="flex:1; min-width:160px">
          <button class="cmd-btn" onclick="wlAdd()">Añadir</button>
        </div>
      </div>
    </div>

    <!-- TAB 2: METRICAS -->
    <div id="tab-metrics" class="tab-content">
      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><svg class="ico" viewBox="0 0 24 24" style="width:16px;height:16px;vertical-align:-3px"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> TPS del servidor</div>
            <div style="display:flex;gap:8px;align-items:center">
              <div class="chart-badge" id="tpsBadge">—</div>
              <button class="term-tool-btn" onclick="refreshTps()">Actualizar</button>
            </div>
          </div>
          <div class="scard-val" id="tpsVal" style="font-size:34px">—</div>
          <div class="scard-sub" id="tpsSub">Sin datos todavía</div>
          <div class="scard-bar"><div class="scard-bar-fill" id="tpsBar" style="width:0%"></div></div>
        </div>
      </div>
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

      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">CPU — últimas 24 h</div>
            <div class="chart-badge" id="h24CpuBadge">—</div>
          </div>
          <canvas id="canvasH24Cpu"></canvas>
        </div>
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">RAM — últimas 24 h</div>
            <div class="chart-badge" id="h24MemBadge">—</div>
          </div>
          <canvas id="canvasH24Mem"></canvas>
        </div>
      </div>

      <div class="chart-card" style="margin-bottom:20px">
        <div class="chart-header">
          <div class="chart-title">Actividad — últimos 14 días</div>
          <div class="chart-badge" id="heatBadge">—</div>
        </div>
        <div class="heat" id="heatMap" style="height:74px"></div>
        <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-dim); margin-top:6px">
          <span>menos</span><span><span style="color:#c084fc">■</span> joins · <span style="color:#34d399">■</span> día con backup</span><span>más</span>
        </div>
      </div>

      <div class="system-details-card">
        <h3 style="margin-bottom:14px; font-size:16px;">Información del Servidor y Entorno</h3>
        <table class="details-table">
          <tr><td>Estado del Proceso</td><td id="detStatus">OFFLINE</td></tr>
          <tr><td>Identificador de Proceso (PID)</td><td id="detPid">-</td></tr>
          <tr><td>Carga del Sistema (1m, 5m, 15m)</td><td id="detLoad">-</td></tr>
          <tr><td>Tamaño del Mundo (PrankLindorf)</td><td id="detWorld">-</td></tr>
          <tr><td>Disco usado (servidor)</td><td id="detDisk">-</td></tr>
          <tr><td>Red host (RX/TX)</td><td id="detNet">-</td></tr>
          <tr><td>Puerto Minecraft</td><td>25566 (TCP / UDP)</td></tr>
          <tr><td>Entorno Java</td><td>OpenJDK 21 (64-Bit Server VM)</td></tr>
          <tr><td>Directorio</td><td>~/PrankLindorf-NeoForge</td></tr>
        </table>
      </div>

      <div class="system-details-card" style="margin-top:14px">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px">
          <h3 style="font-size:15px; margin:0">Monitor de Red y Latencia</h3>
          <button class="term-tool-btn" onclick="refreshPingMetrics()">Medir latencia</button>
        </div>
        <div class="stats-cards" style="grid-template-columns:repeat(3, 1fr); margin-bottom:0">
          <div class="scard" style="padding:14px">
            <div class="scard-label"><span>Local Minecraft (TCP)</span></div>
            <div class="scard-val" id="pingLocalMc" style="font-size:22px">—</div>
            <div class="scard-sub" id="pingLocalMcSub">127.0.0.1:25566</div>
          </div>
          <div class="scard" style="padding:14px">
            <div class="scard-label"><span>Local RCON</span></div>
            <div class="scard-val" id="pingLocalRcon" style="font-size:22px">—</div>
            <div class="scard-sub" id="pingLocalRconSub">127.0.0.1:25575</div>
          </div>
          <div class="scard" style="padding:14px">
            <div class="scard-label"><span>Túnel Público (Playit)</span></div>
            <div class="scard-val" id="pingTunnel" style="font-size:22px">—</div>
            <div class="scard-sub" id="pingTunnelSub">Túnel playit.gg</div>
          </div>
        </div>
      </div>

      <div class="system-details-card" style="margin-top:14px">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px">
          <div>
            <h3 style="font-size:15px; margin:0">Diagnósticos de Rendimiento Spark</h3>
            <div style="font-size:11.5px; color:var(--text-dim); margin-top:2px">Mod oficial integrado para NeoForge 1.21.1</div>
          </div>
          <span class="mchip ld">spark 1.10</span>
        </div>
        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px">
          <button class="cmd-btn" onclick="sparkAction('health')"><svg class="ico" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg> Diagnóstico de Salud (Health)</button>
          <button class="btn-ghost" onclick="sparkAction('sampler_start')">Grabar Sampler (45s)</button>
          <button class="btn-ghost" onclick="sparkAction('sampler_stop')">Subir & Ver Reporte</button>
          <button class="btn-ghost" onclick="sparkAction('tps')">Ver TPS Spark</button>
          <button class="term-tool-btn" onclick="sparkAction('gc')">Forzar GC</button>
        </div>
        <div id="sparkResultBox" style="display:none; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:10px; padding:14px; font-family:'JetBrains Mono',monospace; font-size:12px; white-space:pre-wrap; max-height:280px; overflow-y:auto; color:#d8b4fe"></div>
      </div>
    </div>

    <!-- TAB 3: ARCHIVOS -->
    <div id="tab-files" class="tab-content">
      <div class="fs-toolbar">
        <button class="cmd-btn" onclick="fsCreate('file')"><svg class="ico" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/></svg> Nuevo archivo</button>
        <button class="cmd-btn" onclick="fsCreate('dir')"><svg class="ico" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg> Nueva carpeta</button>
        <label class="cmd-btn" style="cursor:pointer"><svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Subir<input type="file" id="fsUploadInput" style="display:none" multiple onchange="fsUpload()"></label>
        <span class="opt-sw" onclick="fsToggleSw('fsUnzip')" title="Descomprimir automáticamente los .zip al subirlos"><span class="tsw" id="fsUnzipSw" role="switch" tabindex="0" aria-checked="false" aria-label="Descomprimir .zip" onkeydown="swKey('fsUnzip',event)"></span><span class="opt-lbl">Descomprimir .zip</span><input type="checkbox" id="fsUnzip" hidden></span>
        <span style="flex:1"></span>
        <span class="fs-crumb"><a onclick="fsNavUp()">↑ Subir</a>&nbsp;<span id="fsBreadcrumb">/</span></span>
        <button class="term-tool-btn" onclick="loadFiles()">Recargar</button>
      </div>
      <div class="fs-toolbar">
        <label style="font-size:12px; color:var(--text-muted)">Ordenar:</label>
        <div class="seg" id="fsSeg">
          <button data-k="name" class="on" onclick="fsSortKey('name')">Nombre</button>
          <button data-k="mtime" onclick="fsSortKey('mtime')">Fecha</button>
          <button data-k="size" onclick="fsSortKey('size')">Tamaño</button>
        </div>
        <button class="term-tool-btn" id="fsSortDir" onclick="fsToggleDir()" title="Dirección">↓</button>
        <span class="opt-sw" onclick="fsToggleSw('fsGroup')" title="Mostrar las carpetas antes que los archivos"><span class="tsw on" id="fsGroupSw" role="switch" tabindex="0" aria-checked="true" aria-label="Carpetas primero" onkeydown="swKey('fsGroup',event)"></span><span class="opt-lbl">Carpetas primero</span><input type="checkbox" id="fsGroup" checked hidden onchange="loadFiles()"></span>
      </div>
      <div class="file-list" id="filesContainer">
        <div class="file-row"><span>Cargando archivos del servidor...</span></div>
      </div>
      <div class="system-details-card" style="margin-top:12px">
        <div class="mods-head">
          <h3>Mods instalados <span style="font-size:11px;color:var(--text-dim)">(los cambios requieren reinicio)</span></h3>
          <span class="mods-count" id="modCounts"></span>
          <span style="flex:1"></span>
          <input class="mod-search" id="modSearch" placeholder="Buscar mod..." oninput="renderMods()" autocomplete="off">
          <button class="btn-ghost" onclick="modBulk(true)">Activar todos</button>
          <button class="btn-ghost" onclick="modBulk(false)">Desactivar todos</button>
          <button class="btn-ghost" onclick="toggleModrinthView()" style="color:#38bdf8; border-color:rgba(56,189,248,0.4)">🌐 Explorador Modrinth</button>
        </div>
        <div class="file-list" id="modsList"><div class="file-row"><span class="file-name">Cargando mods...</span></div></div>
        <div id="modrinthSection" style="display:none; margin-top:16px; border-top:1px solid var(--border-color); padding-top:14px">
          <div style="display:flex; gap:10px; align-items:center; margin-bottom:12px; flex-wrap:wrap">
            <input id="mrSearchInput" class="tin" placeholder="Buscar mods para NeoForge 1.21.1 en Modrinth (ej. create, jei, appleskin, ferritecore...)" style="flex:1; min-width:220px" onkeydown="if(event.key==='Enter')searchModrinth()">
            <button class="cmd-btn" onclick="searchModrinth()">Buscar</button>
            <button class="term-tool-btn" onclick="toggleModrinthView()">Cerrar</button>
          </div>
          <div id="mrResults" class="mr-grid"><div style="color:var(--text-dim); font-size:12px; padding:12px">Escribe un término y pulsa Buscar</div></div>
        </div>
      </div>
    </div>
      <div id="fsEditModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:200; align-items:center; justify-content:center; padding:20px">
        <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:12px; width:min(860px,100%); max-height:90vh; display:flex; flex-direction:column; overflow:hidden">
          <div style="display:flex; align-items:center; gap:10px; padding:12px 16px; border-bottom:1px solid var(--border-color)">
            <span style="font-family:'JetBrains Mono',monospace; font-size:12.5px; color:#93c5fd; flex:1; overflow:hidden; text-overflow:ellipsis" id="fsEditPath">—</span>
            <span id="fsEditStats" style="font-size:11px; color:var(--text-dim)">0 líneas</span>
            <button class="term-tool-btn" onclick="fsChangeFontSize(1)" title="Aumentar texto">A+</button>
            <button class="term-tool-btn" onclick="fsChangeFontSize(-1)" title="Reducir texto">A-</button>
            <button class="term-tool-btn" onclick="fsDownloadCurrent()">Descargar</button>
            <button class="term-tool-btn" onclick="fsCloseEdit()">Cerrar ✕</button>
          </div>
          <textarea id="fsEditor" style="flex:1; min-height:360px; background:var(--bg-terminal); color:#cbd5e1; border:0; padding:14px; font-family:'JetBrains Mono',monospace; font-size:12.5px; resize:vertical; tab-size:2; line-height:1.45" placeholder="Cargando..." oninput="fsUpdateEditorStats()" onkeydown="fsHandleEditorTab(event)"></textarea>
          <div style="display:flex; justify-content:space-between; align-items:center; padding:12px 16px; border-top:1px solid var(--border-color)">
            <span style="font-size:11px; color:var(--text-dim)">Presiona TAB para indentar código sin perder el foco</span>
            <button class="cmd-btn" onclick="fsSaveEdit()">Guardar cambios</button>
          </div>
        </div>
      </div>

    <!-- TAB: MUNDOS & MCA REGION CLEANER -->
    <div id="tab-worlds" class="tab-content">
      <div class="stats-cards" style="grid-template-columns:repeat(3,1fr); margin-bottom:16px">
        <div class="dim-card" id="dimCardOverworld">
          <div style="display:flex; justify-content:space-between; align-items:flex-start">
            <div>
              <div style="font-size:11px; color:var(--text-muted); font-weight:700">DIMENSIÓN 0</div>
              <div style="font-size:18px; font-weight:800; color:#34d399; margin:4px 0">Overworld</div>
              <div style="font-size:11.5px; color:var(--text-dim)">Superficie principal</div>
            </div>
            <span class="mchip ld" id="dimOwRegions">0 regiones</span>
          </div>
          <div style="margin-top:14px; display:flex; justify-content:space-between; align-items:center">
            <span style="font-size:18px; font-weight:800; color:#fff" id="dimOwSize">—</span>
            <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="selectMcaDim('overworld')">Explorar MCA</button>
          </div>
        </div>

        <div class="dim-card" id="dimCardNether">
          <div style="display:flex; justify-content:space-between; align-items:flex-start">
            <div>
              <div style="font-size:11px; color:var(--text-muted); font-weight:700">DIMENSIÓN -1</div>
              <div style="font-size:18px; font-weight:800; color:#f87171; margin:4px 0">The Nether</div>
              <div style="font-size:11.5px; color:var(--text-dim)">Inframundo (DIM-1)</div>
            </div>
            <span class="mchip ld" id="dimNetherRegions">0 regiones</span>
          </div>
          <div style="margin-top:14px; display:flex; justify-content:space-between; align-items:center">
            <span style="font-size:18px; font-weight:800; color:#fff" id="dimNetherSize">—</span>
            <div style="display:flex; gap:6px">
              <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d; font-size:11px" onclick="mcaResetDim('nether')">Reiniciar</button>
              <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="selectMcaDim('nether')">Explorar</button>
            </div>
          </div>
        </div>

        <div class="dim-card" id="dimCardEnd">
          <div style="display:flex; justify-content:space-between; align-items:flex-start">
            <div>
              <div style="font-size:11px; color:var(--text-muted); font-weight:700">DIMENSIÓN 1</div>
              <div style="font-size:18px; font-weight:800; color:#c084fc; margin:4px 0">The End</div>
              <div style="font-size:11.5px; color:var(--text-dim)">El Fin (DIM1)</div>
            </div>
            <span class="mchip ld" id="dimEndRegions">0 regiones</span>
          </div>
          <div style="margin-top:14px; display:flex; justify-content:space-between; align-items:center">
            <span style="font-size:18px; font-weight:800; color:#fff" id="dimEndSize">—</span>
            <div style="display:flex; gap:6px">
              <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d; font-size:11px" onclick="mcaResetDim('end')">Reiniciar</button>
              <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="selectMcaDim('end')">Explorar</button>
            </div>
          </div>
        </div>
      </div>

      <!-- MCA Region Cleaner Card -->
      <div class="system-details-card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px">
          <div>
            <h3 style="margin:0; font-size:15px">MCA Region Cleaner <span style="font-size:11.5px; color:var(--text-muted)">(Herramienta estilo MCA Selector)</span></h3>
            <div style="font-size:11.5px; color:var(--text-dim); margin-top:2px">Elimina regiones (.mca) para regenerar terreno, limpiar chunks corruptos o liberar espacio</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center">
            <div class="seg" id="mcaDimSeg">
              <button data-dim="overworld" class="on" onclick="selectMcaDim('overworld')">Overworld</button>
              <button data-dim="nether" onclick="selectMcaDim('nether')">Nether</button>
              <button data-dim="end" onclick="selectMcaDim('end')">The End</button>
            </div>
            <button class="term-tool-btn" onclick="loadWorldManager()">Recargar</button>
          </div>
        </div>

        <!-- Calculadora de Coordenadas -->
        <div style="background:rgba(168,85,247,0.06); border:1px solid rgba(168,85,247,0.2); border-radius:10px; padding:12px; margin-bottom:12px; display:flex; align-items:center; gap:12px; flex-wrap:wrap">
          <span style="font-size:12px; font-weight:700; color:#d8b4fe">Calculadora de Coordenadas:</span>
          <input type="number" id="calcCoordX" class="tin" placeholder="Bloque X" style="width:110px; padding:6px 10px; font-size:12px" oninput="calcMcaFromCoords()">
          <input type="number" id="calcCoordZ" class="tin" placeholder="Bloque Z" style="width:110px; padding:6px 10px; font-size:12px" oninput="calcMcaFromCoords()">
          <span id="calcMcaResult" style="font-size:12.5px; font-family:'JetBrains Mono',monospace; color:#38bdf8">Introduce X y Z</span>
          <button class="term-tool-btn" style="font-size:11px" onclick="selectCalculatedMca()">Marcar en lista</button>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px">
          <div style="display:flex; gap:12px; align-items:center">
            <label style="font-size:12px; color:var(--text-muted); cursor:pointer; display:inline-flex; align-items:center; gap:6px"><input type="checkbox" id="mcaSelectAll" onchange="toggleSelectAllMca(this.checked)"> Seleccionar todos</label>
            <span id="mcaSelectedCount" style="font-size:12px; color:#c084fc; font-weight:700">0 regiones seleccionadas</span>
          </div>
          <button class="cmd-btn danger" style="background:#dc2626; color:#fff" onclick="deleteSelectedMca()">Borrar regiones seleccionadas</button>
        </div>

        <div class="mca-grid" id="mcaListContainer">
          <div class="mca-row" style="color:var(--text-dim)">Cargando archivos de región...</div>
        </div>
      </div>
    </div>

    <!-- TAB: BACKUPS -->
    <div id="tab-backups" class="tab-content">
      <div class="stats-cards" style="grid-template-columns:repeat(3,1fr); margin-bottom:16px">
        <div class="scard">
          <div class="scard-label"><span>Próximo automático</span></div>
          <div class="scard-val" id="bkNext" style="font-size:19px">—</div>
          <div class="scard-sub" id="bkNextSub">—</div>
        </div>
        <div class="scard">
          <div class="scard-label"><span>Espacio en backups</span></div>
          <div class="scard-val" id="bkSpace" style="font-size:19px">—</div>
          <div class="scard-sub" id="bkSpaceSub">—</div>
        </div>
        <div class="scard">
          <div class="scard-label"><span>Copias guardadas</span></div>
          <div class="scard-val" id="bkCount" style="font-size:19px">—</div>
          <div class="scard-sub" id="bkCountSub">—</div>
        </div>
      </div>
      <div class="system-details-card" style="margin-bottom:12px">
        <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center">
          <button class="cmd-btn" onclick="backupNow()"><svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Backup ahora</button>
          <button class="term-tool-btn" onclick="loadBackups()">Recargar</button>
          <span id="bkStatus" style="font-size:12px; color:var(--text-muted)">Cargando estado...</span>
        </div>
        <div id="bkJob" style="font-size:12.5px; color:#c084fc; margin-top:8px; min-height:18px"></div>
        <div id="bkProgWrap" style="display:none; margin-top:8px">
          <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--text-muted); margin-bottom:4px">
            <span id="bkStage">…</span><span id="bkPct">0%</span>
          </div>
          <div class="scard-bar" style="height:8px"><div class="scard-bar-fill" id="bkBar" style="width:0%"></div></div>
        </div>
      </div>
      <div class="system-details-card" style="margin-bottom:12px">
        <h3 style="margin-bottom:10px; font-size:14px">Copias de seguridad</h3>
        <div class="file-list" id="bkList" style="background:transparent; border:0">
          <div class="file-row"><span>Cargando backups...</span></div>
        </div>
      </div>
      <details class="system-details-card" style="margin-top:0">
        <summary style="cursor:pointer; font-size:14px; font-weight:700">Configuración avanzada</summary>
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px">
          <label style="font-size:12px;color:var(--text-muted);display:inline-flex;align-items:center;gap:6px"><input type="checkbox" id="bkEn"> Automático diario</label>
          <input id="bkTime" placeholder="04:00" style="width:90px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 10px; color:#fff; font-family:'JetBrains Mono',monospace; font-size:12.5px">
          <input id="bkDir" placeholder="Carpeta destino" style="flex:2; min-width:180px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-size:12.5px">
          <span class="dur" title="Días de retención"><button onclick="durStep('bkRet',-1,1,365)">−</button><input id="bkRet" value="7" readonly><button onclick="durStep('bkRet',1,1,365)">+</button></span>
          <label style="font-size:12px;color:var(--text-muted);display:inline-flex;align-items:center;gap:6px"><input type="checkbox" id="bkMonthly"> Mensual eterno</label>
          <button class="cmd-btn" onclick="saveBkCfg()">Guardar</button>
        </div>
        <div style="font-size:11.5px; color:var(--text-dim); margin-top:8px" id="bkRetNote"></div>
      </details>
    </div>

    <!-- TAB: TAREAS PROGRAMADAS -->
    <div id="tab-tasks" class="tab-content">
      <div class="system-details-card" style="margin-bottom:12px">
        <h3 style="margin-bottom:8px">Nueva tarea</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input id="skName" placeholder="Nombre" style="flex:2; min-width:140px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-size:12.5px">
          <select id="skKind" style="flex:1; min-width:120px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 10px; color:#e2e8f0; font-size:12.5px">
            <option value="command">Comando</option>
            <option value="message">Anuncio (say)</option>
            <option value="save">Guardar mundo</option>
            <option value="restart">Reiniciar (opt-in)</option>
          </select>
          <select id="skMode" onchange="document.getElementById('skWhen').placeholder = this.value === 'interval' ? 'Cada N minutos' : 'Hora HH:MM'" style="flex:1; min-width:120px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 10px; color:#e2e8f0; font-size:12.5px">
            <option value="daily">Diaria</option>
            <option value="interval">Intervalo</option>
          </select>
          <input id="skWhen" placeholder="Hora HH:MM" style="flex:1; min-width:110px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-family:'JetBrains Mono',monospace; font-size:12.5px">
          <button class="cmd-btn" onclick="skCreate()">Crear</button>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:8px">
          <input id="skPayload" placeholder="Comando o mensaje (vacío para save/restart)" style="flex:3; min-width:200px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:9px 12px; color:#fff; font-family:'JetBrains Mono',monospace; font-size:12.5px">
          <span class="dur" title="Aviso previo en minutos (solo restart)"><button onclick="durStep('skWarn',-1,0,30)">−</button><input id="skWarn" value="0" readonly><button onclick="durStep('skWarn',1,0,30)">+</button></span>
        </div>
        <div style="font-size:11.5px; color:var(--text-dim); margin-top:6px">El reinicio automático viene desactivado: solo se ejecuta si creas y activas una tarea de ese tipo.</div>
      </div>
      <div class="file-list" id="skList">
        <div class="file-row"><span>Cargando tareas...</span></div>
      </div>
    </div>

    <!-- TAB: HISTORIAL DE JUGADORES -->
    <div id="tab-history" class="tab-content">
      <div class="system-details-card" style="margin-bottom:12px">
        <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center">
          <h3 style="font-size:15px; flex:1">Historial de jugadores</h3>
          <button class="term-tool-btn" onclick="loadHistory()">Recargar</button>
        </div>
        <div style="font-size:12px; color:var(--text-muted); margin-top:4px" id="histSub">Entradas y salidas del servidor</div>
      </div>
      <div class="file-list" id="connHist"><div class="file-row"><span class="file-name">Sin registros</span></div></div>
    </div>

    <!-- TAB: FLOTA DE SERVIDORES -->
    <div id="tab-fleet" class="tab-content">
      <div class="system-details-card" style="margin-bottom:16px">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px">
          <div>
            <h2 style="font-size:18px; font-weight:800; margin:0">Flota de Servidores PKHosting</h2>
            <div style="font-size:12px; color:var(--text-muted); margin-top:2px">Vista global y conmutación rápida de todas tus instancias</div>
          </div>
          <button class="cmd-btn" onclick="switchTab('settings')">+ Añadir o configurar servidor</button>
        </div>
      </div>
      <div id="fleetCardsContainer" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:16px">
        <div class="scard" style="padding:16px">Cargando flota...</div>
      </div>
    </div>

    <!-- TAB 4: CONFIGURACION -->
    <div id="tab-settings" class="tab-content">
      <div class="system-details-card" style="margin-bottom:12px">
        <div class="mods-head">
          <h3 style="margin:0; font-size:14px">Servidores gestionados</h3>
          <span class="mods-count" id="srvCounts"></span>
          <span style="flex:1"></span>
          <button class="btn-ghost" onclick="srvShowForm()">+ Añadir servidor</button>
        </div>
        <div class="file-list" id="srvList"><div class="file-row"><span class="file-name">Cargando servidores...</span></div></div>
        <div id="srvForm" style="display:none; margin-top:12px; background:rgba(255,255,255,0.02); border:1px solid var(--border-color); border-radius:10px; padding:14px">
          <div style="font-size:13px; font-weight:700; margin-bottom:10px" id="srvFormTitle">Añadir servidor</div>
          <input type="hidden" id="srvEditId" value="">
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px">
            <label style="font-size:11.5px; color:var(--text-muted)">Nombre<input id="srvName" class="tin" maxlength="40" placeholder="MiSegundoServidor" style="width:100%; margin-top:4px"></label>
            <label style="font-size:11.5px; color:var(--text-muted)">Carpeta del servidor
              <div style="display:flex; gap:8px; margin-top:4px">
                <input id="srvDir" class="tin" placeholder="~/MiServidor" style="flex:1; min-width:0">
                <button class="term-tool-btn" type="button" onclick="openSrvBrowser()" title="Explorar carpetas">Examinar…</button>
              </div>
            </label>
            <label style="font-size:11.5px; color:var(--text-muted)">Puerto MC<input id="srvMc" class="tin" type="number" min="1" max="65535" style="width:100%; margin-top:4px"></label>
            <label style="font-size:11.5px; color:var(--text-muted)">Puerto RCON<input id="srvRcon" class="tin" type="number" min="1" max="65535" style="width:100%; margin-top:4px"></label>
            <label style="font-size:11.5px; color:var(--text-muted)">RAM (GB)<input id="srvMem" class="tin" type="number" min="0.5" max="64" step="0.5" style="width:100%; margin-top:4px"></label>
          </div>
          <div style="font-size:11.5px; color:var(--text-dim); margin-top:8px">Se usa <span style="font-family:'JetBrains Mono',monospace">start.sh</span> dentro de la carpeta. Eliminar un servidor no borra sus archivos.</div>
          <div style="display:flex; gap:8px; margin-top:12px">
            <button class="cmd-btn" onclick="srvSubmit()">Guardar</button>
            <button class="term-tool-btn" onclick="srvHideForm()">Cancelar</button>
          </div>
        </div>
      </div>
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
      <div class="system-details-card" style="margin-top:12px">
        <h3 style="margin-bottom:8px">Contraseña del panel</h3>
        <div style="font-size:12px; color:var(--text-muted); margin-bottom:8px" id="pwState">Comprobando...</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input type="password" id="pwCur" placeholder="Actual" style="flex:1; min-width:140px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; color:#fff">
          <input type="password" id="pwNew" placeholder="Nueva (mín. 8)" style="flex:1; min-width:140px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; color:#fff">
          <button class="cmd-btn" onclick="changePw()">Cambiar</button>
        </div>
      </div>
      <div class="system-details-card" style="margin-top:12px">
        <h3 style="margin-bottom:8px">Botones rápidos de consola</h3>
        <div style="font-size:12px; color:var(--text-muted); margin-bottom:8px">Separados por comas (máx. 12)</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <input id="quickInput" placeholder="say Hola, list, save-all" style="flex:1; min-width:220px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; color:#fff; font-size:12.5px">
          <button class="cmd-btn" onclick="saveQuick()">Guardar</button>
        </div>
      </div>
      <div class="system-details-card" style="margin-top:12px">
        <h3 style="margin-bottom:8px">Notificaciones Discord</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px">
          <input id="dcHook" placeholder="Webhook URL de Discord" style="flex:1; min-width:220px; background:var(--bg-terminal); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; color:#e2e8f0; font-size:12.5px">
          <button class="cmd-btn" onclick="saveDiscord()">Guardar</button>
          <button class="term-tool-btn" onclick="testDiscord()">Probar</button>
        </div>
        <div id="dcEvents" style="display:flex; gap:12px; flex-wrap:wrap; font-size:12.5px; color:var(--text-muted)"></div>
      </div>
    </div>

  </div>
</main>

<div id="srvBrowseModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:200; align-items:center; justify-content:center; padding:20px">
  <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:12px; width:min(560px,100%); max-height:80vh; display:flex; flex-direction:column; overflow:hidden">
    <div style="display:flex; align-items:center; gap:10px; padding:12px 16px; border-bottom:1px solid var(--border-color)">
      <span class="fs-crumb" style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" id="srvBrowsePath">—</span>
      <button class="term-tool-btn" onclick="srvBrowseUp()" title="Subir un nivel">↑ Subir</button>
      <button class="term-tool-btn" onclick="srvBrowseClose()">Cerrar ✕</button>
    </div>
    <div class="file-list" id="srvBrowseList" style="overflow-y:auto; border:0; border-radius:0"><div class="file-row"><span class="file-name">Cargando...</span></div></div>
    <div style="display:flex; gap:10px; padding:12px 16px; border-top:1px solid var(--border-color)">
      <button class="cmd-btn" style="flex:1" onclick="srvBrowsePick()">Elegir esta carpeta</button>
    </div>
  </div>
</div>

<!-- PLAYER INSPECTOR MODAL -->
<div id="playerModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.72); backdrop-filter:blur(10px); z-index:450; align-items:center; justify-content:center; padding:20px">
  <div style="background:var(--bg-card); border:1px solid rgba(168,85,247,0.4); border-radius:18px; width:min(520px, 100%); max-height:90vh; overflow-y:auto; padding:24px; box-shadow:0 16px 50px rgba(0,0,0,0.8), 0 0 30px rgba(168,85,247,0.25)">
    <div style="display:flex; justify-content:space-between; align-items:flex-start">
      <div style="display:flex; gap:16px; align-items:center">
        <img id="pmAvatar" src="" class="p-avatar" style="width:52px; height:52px; border-radius:12px" alt="">
        <div>
          <div style="font-size:20px; font-weight:800; color:#fff" id="pmName">Jugador</div>
          <div style="font-size:11px; font-family:'JetBrains Mono',monospace; color:var(--text-dim)" id="pmUuid">—</div>
        </div>
      </div>
      <button class="icon-btn" onclick="closePlayerModal()">✕</button>
    </div>

    <div class="p-stats-grid">
      <div class="p-stat-card"><div class="p-stat-val" id="pmHours">—</div><div class="p-stat-lbl">Horas jugadas</div></div>
      <div class="p-stat-card"><div class="p-stat-val" id="pmDeaths">—</div><div class="p-stat-lbl">Muertes</div></div>
      <div class="p-stat-card"><div class="p-stat-val" id="pmMobKills">—</div><div class="p-stat-lbl">Mobs abatidos</div></div>
      <div class="p-stat-card"><div class="p-stat-val" id="pmDist">—</div><div class="p-stat-lbl">Km recorridos</div></div>
    </div>

    <div id="pmLiveSection" style="background:rgba(255,255,255,0.03); border:1px solid var(--border-color); border-radius:12px; padding:12px; margin-bottom:14px">
      <div style="font-size:12px; font-weight:700; color:#38bdf8; margin-bottom:6px">Estado en Vivo (RCON):</div>
      <div style="display:flex; justify-content:space-between; font-size:12.5px; flex-wrap:wrap; gap:8px">
        <span>Coordenadas: <b id="pmCoords" style="color:#fff; font-family:'JetBrains Mono',monospace">—</b></span>
        <span>Nivel Exp: <b id="pmLevel" style="color:#22c55e">—</b></span>
      </div>
    </div>

    <div style="font-size:12px; font-weight:700; color:var(--text-muted); margin-bottom:8px">Acciones rápidas sobre el jugador:</div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px">
      <button class="term-tool-btn" onclick="pmQuickCmd('heal')">❤️ Curar vida</button>
      <button class="term-tool-btn" onclick="pmQuickCmd('feed')">🍗 Alimentar</button>
      <button class="term-tool-btn" onclick="pmQuickCmd('creative')">🎨 Modo Creativo</button>
      <button class="term-tool-btn" onclick="pmQuickCmd('survival')">⚔️ Supervivencia</button>
      <button class="term-tool-btn" onclick="pmQuickCmd('spawn')">📍 Spawnpoint</button>
      <button class="term-tool-btn" onclick="pmQuickCmd('clearlag')">🧹 Vaciar inventario</button>
    </div>
  </div>
</div>

<!-- COMMAND PALETTE (CTRL+K) -->
<div id="cmdPalette" onclick="if(event.target===this)closeCmdPalette()">
  <div class="cp-box">
    <div class="cp-input-wrap">
      <svg class="ico" viewBox="0 0 24 24" style="color:var(--text-muted); width:18px; height:18px"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input id="cpInput" class="cp-input" placeholder="Buscar sección o comando (ej: reiniciar, archivos, mca, spark, backups...)" autocomplete="off" oninput="renderCmdPalette()" onkeydown="handlePaletteKey(event)">
      <span style="font-size:11px; background:rgba(255,255,255,0.06); padding:2px 6px; border-radius:4px; color:var(--text-dim)">ESC</span>
    </div>
    <div class="cp-list" id="cpList"></div>
    <div class="cp-footer">
      <span>↑ ↓ para navegar · ENTER para ejecutar</span>
      <span>PKHosting Spotlight</span>
    </div>
  </div>
</div>

<div class="toaster" id="toaster" data-pos="top-right" data-theme="dark" aria-live="polite"></div>

<script>
let autoScroll = true;
let historyBuffer = [];
let cmdHistory = [];
let cmdIndex = -1;

const SRV = new URLSearchParams(location.search).get('server') || '';
function U(p) {
  if (!SRV) return p;
  return p + (p.includes('?') ? '&' : '?') + 'server=' + encodeURIComponent(SRV);
}
const _fetch = window.fetch.bind(window);
window.fetch = async (...a) => {
  const r = await _fetch(...a);
  if (r.status === 401) location.reload();
  return r;
};
async function logout() {
  let auth = true;
  try {
    const r = await (await _fetch(U('/api/logout'), { method: 'POST' })).json();
    auth = r.auth !== false;
  } catch (e) { /* sin sesión que cerrar */ }
  if (!auth) {
    showToast('Sin contraseña: créala en Configuración para activar las sesiones', 'info');
    switchTab('settings');
    return;
  }
  location.href = '/login';
}
const SILEO_ICONS = {
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  loading: '<svg class="tk-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg>'
};
const SILEO_POS = ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'];
let sileoDefaultPos = 'top-right';
function escToast(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function sileoBox(pos) {
  pos = SILEO_POS.includes(pos) ? pos : sileoDefaultPos;
  let box = document.querySelector(`.toaster[data-pos="${pos}"]`);
  if (!box) {
    box = document.createElement('div');
    box.className = 'toaster';
    box.dataset.pos = pos;
    box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  return box;
}
function sileoShow({ title, description, type, duration, action, position } = {}) {
  type = SILEO_ICONS[type] ? type : 'info';
  if (duration === undefined) duration = 4000;
  const box = sileoBox(position);
  const el = document.createElement('div');
  el.className = 'toast';
  el.dataset.state = type;
  const icon = type === 'loading' ? SILEO_ICONS.loading : SILEO_ICONS[type];
  el.innerHTML = `<span class="tk-ico">${icon}</span>`
    + `<div class="tk-main"><div class="tk-title">${escToast(title || '')}</div>`
    + (description ? `<div class="tk-desc">${escToast(description)}</div>` : '')
    + (action ? `<button class="tk-act">${escToast(action.label)}</button>` : '')
    + `</div><button class="tk-x" title="Cerrar">×</button>`;
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    el.classList.add('exiting');
    setTimeout(() => el.remove(), 300);
  };
  let timer = null, left = duration, started = Date.now();
  const tick = () => {
    if (duration == null) return;
    clearTimeout(timer);
    timer = setTimeout(dismiss, left);
  };
  el.addEventListener('mouseenter', () => {
    clearTimeout(timer);
    if (duration != null) left -= Date.now() - started;
  });
  el.addEventListener('mouseleave', () => { started = Date.now(); tick(); });
  el.addEventListener('click', e => {
    if (e.target.closest('.tk-act') && action) { try { action.onClick(); } catch (err) {} }
    dismiss();
  });
  box.appendChild(el);
  while (box.children.length > 5) box.firstChild.remove();
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('ready')));
  if (duration != null) tick();
  return dismiss;
}
function showToast(msg, type) {
  sileoShow({ title: String(msg == null ? '' : msg), type: type || 'info' });
}
const sileo = {
  show: sileoShow,
  setPosition: pos => { if (SILEO_POS.includes(pos)) sileoDefaultPos = pos; },
  dismissAll: () => document.querySelectorAll('.toast').forEach(el => el.remove()),
  success: (title, desc, o) => sileoShow({ title, description: desc, type: 'success', ...(o || {}) }),
  error: (title, desc, o) => sileoShow({ title, description: desc, type: 'error', ...(o || {}) }),
  warning: (title, desc, o) => sileoShow({ title, description: desc, type: 'warning', ...(o || {}) }),
  info: (title, desc, o) => sileoShow({ title, description: desc, type: 'info', ...(o || {}) }),
  loading: (title, o) => sileoShow({ title, type: 'loading', duration: null, ...(o || {}) }),
  promise: async (p, { loading, success, error } = {}) => {
    const stop = sileoShow({ title: loading || 'Cargando…', type: 'loading', duration: null });
    try {
      const r = await p;
      stop();
      const msg = typeof success === 'function' ? success(r) : (success || 'Listo');
      sileoShow({ title: String(msg), type: 'success' });
      return r;
    } catch (e) {
      stop();
      const msg = typeof error === 'function' ? error(e) : (error || 'Falló la operación');
      sileoShow({ title: String(msg), type: 'error' });
      throw e;
    }
  }
};

let currentPublicIp = 'localhost:25566';
function copyIp() {
  navigator.clipboard.writeText(currentPublicIp);
  document.getElementById('copyBadge').textContent = '¡Copiado!';
  setTimeout(() => document.getElementById('copyBadge').textContent = 'Copiar', 1800);
  showToast('Dirección del servidor copiada: ' + currentPublicIp);
}
async function refreshPublicIp() {
  try {
    const res = await fetch(U('/api/tunnels'));
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
    const res = await fetch(U('/api/public-ip'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ public_ip: v }) });
    const data = await res.json();
    showToast(data.msg || 'OK');
    refreshPublicIp();
  } catch (e) { showToast('Error al guardar IP'); }
}

const KNOWN_TABS = ['console', 'metrics', 'files', 'worlds', 'backups', 'tasks', 'history', 'fleet', 'settings'];
function rememberTab(name) {
  try {
    localStorage.setItem('pkTab', name);
    if (name === 'files') localStorage.setItem('pkFs', fsPath);
  } catch (e) {}
}
function switchTab(name, fromHash) {
  if (!fromHash) syncHash(name);
  rememberTab(name);
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  
  const targetNav = Array.from(document.querySelectorAll('.nav-item')).find(el => el.textContent.toLowerCase().includes(
    name === 'console' ? 'consola' :
    name === 'metrics' ? 'métrica' :
    name === 'files' ? 'archivo' :
    name === 'worlds' ? 'mundo' :
    name === 'backups' ? 'backup' :
    name === 'tasks' ? 'tarea' :
    name === 'history' ? 'historial' :
    name === 'fleet' ? 'flota' : 'config'
  ));
  if (targetNav) targetNav.classList.add('active');

  const tab = document.getElementById('tab-' + name);
  if (tab) tab.classList.add('active');

  if (name === 'metrics') { renderCharts(); loadHeat(); refreshPingMetrics(); }
  if (name === 'files') { loadFiles(); loadMods(); }
  if (name === 'worlds') { loadWorldManager(); }
  if (name === 'backups') loadBackups();
  if (name === 'tasks') loadSchedules();
  if (name === 'history') loadHistory();
  if (name === 'fleet') loadFleetView();
  if (name === 'console') { loadQuick(); loadModeration(); refreshCmdList(); }
  if (name === 'settings') { loadProps(); refreshPublicIp(); loadDiscord(); loadQuickCfg(); loadServers(); }
}

async function serverAction(act) {
  showToast('Enviando acción: ' + act.toUpperCase() + '...');
  try {
    const res = await fetch(U('/api/' + act), { method: 'POST' });
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
  refreshCmdList();
  try {
    const res = await fetch(U('/api/cmd'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: val })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'Comando enviado' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar comando', 'error');
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

async function playerAction(act, name) {
  name = (name || '').trim();
  if (!name) { showToast('Jugador no válido'); return; }
  if ((act === 'kick' || act === 'ban') && !confirm(`¿${act === 'ban' ? 'BANEAR' : 'Expulsar'} a "${name}"?`)) return;
  try {
    const res = await fetch(U('/api/player'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: act, name })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? 'OK' : 'Error'));
    setTimeout(refreshConsole, 700);
  } catch (e) {
    showToast('Fallo al enviar acción', 'error');
  }
}

function fmtSess(sec) {
  if (sec == null) return '';
  if (sec < 60) return 'menos de 1 min';
  const m = Math.floor(sec / 60), h = Math.floor(m / 60);
  if (h > 0) return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  return m + ' min';
}

let _wlOn = false;
async function loadModeration() {
  try {
    const d = await (await fetch(U('/api/lists'))).json();
    const chip = (n, act, danger) => `<button class="p-chip"${danger ? ' style="color:#f87171;border-color:#7f1d1d"' : ''} title="${act}" onclick="modAct('${act}','${n.replace(/'/g, "")}')"><img src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/18" class="p-avatar" style="width:16px;height:16px" onerror="this.style.display='none'">${n} ✕</button>`;
    document.getElementById('modOps').innerHTML = (d.ops && d.ops.length) ? d.ops.map(n => chip(n, 'deop')).join(' ') : '—';
    document.getElementById('modBans').innerHTML = (d.banned && d.banned.length) ? d.banned.map(b => chip(b.name, 'pardon', true)).join(' ') : '—';
    _wlOn = !!d.whitelist_on;
    document.getElementById('wlState').textContent = _wlOn ? '[ON]' : '[OFF]';
    document.getElementById('modWl').innerHTML = (d.whitelist && d.whitelist.length) ? d.whitelist.map(n => chip(n, 'wlremove')).join(' ') : '—';
  } catch (e) {}
}
async function loadHistory() {
  try {
    const h = await (await fetch(U('/api/connections'))).json();
    const box = document.getElementById('connHist');
    const items = (h && h.items) || [];
    const sub = document.getElementById('histSub');
    if (sub) sub.textContent = items.length ? `${items.length} registros · Entradas y salidas del servidor` : 'Entradas y salidas del servidor';
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin registros</span></div>'; return; }
    box.innerHTML = items.slice(0, 30).map(e => {
      const dt = new Date(e.t * 1000).toLocaleString();
      const what = e.ev === 'join' ? 'entró' : `salió (${fmtSess(e.dur || 0)})`;
      const dot = e.ev === 'join' ? '#34d399' : '#f87171';
      return `<div class="file-row"><img src="https://mc-heads.net/avatar/${encodeURIComponent(e.name)}/22" class="p-avatar" style="width:22px;height:22px" onerror="this.style.display='none'"><span class="file-icon" style="color:${dot}" title="${e.ev === 'join' ? 'Entrada' : 'Salida'}">●</span><span class="file-name" style="cursor:pointer" onclick="openPlayerModal('${e.name}')">${e.name} ${what}</span><span class="file-size">${dt}</span></div>`;
    }).join('');
  } catch (e) {}
}
async function modAct(act, name) {
  if ((act === 'wlremove') && !confirm(`¿Quitar a "${name}" de la whitelist?`)) return;
  try {
    const r = await (await fetch(U('/api/player'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: act, name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlAdd() {
  const v = document.getElementById('wlName').value.trim();
  if (!v) return;
  try {
    const r = await (await fetch(U('/api/player'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'wladd', name: v }) })).json();
    showToast(r.msg || 'OK');
    document.getElementById('wlName').value = '';
  } catch (e) { showToast('Error'); }
  loadModeration();
}
async function wlToggle() {
  try {
    const r = await (await fetch(U('/api/whitelist'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: !_wlOn }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  setTimeout(loadModeration, 1200);
}

function clearTerminal() {
  document.getElementById('termBody').innerHTML = '';
}

let stickBottom = true;
function paintAutoBtn() {
  document.getElementById('btnAutoScroll').textContent =
    !autoScroll ? 'Auto-scroll: OFF' : (stickBottom ? 'Auto-scroll: ON' : 'Ir abajo ↓');
}
function toggleAutoScroll() {
  autoScroll = !autoScroll;
  if (autoScroll) {
    stickBottom = true;
    const body = document.getElementById('termBody');
    body.scrollTop = body.scrollHeight;
  }
  paintAutoBtn();
}
document.getElementById('termBody').addEventListener('scroll', () => {
  const body = document.getElementById('termBody');
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  if (atBottom !== stickBottom) {
    stickBottom = atBottom;
    paintAutoBtn();
  }
});

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

let termLines = [];
let logFilter = '';
const CMD_BASE = ['help', 'list', 'say ', 'op ', 'deop ', 'kick ', 'ban ', 'pardon ', 'stop', 'save-all', 'save-on', 'save-off', 'reload', 'whitelist ', 'start', 'restart', 'reload', 'kill', 'tick query', 'profile entities'];
function setLogFilter(f) {
  logFilter = (logFilter === f) ? '' : f;
  renderConsole();
}
function renderConsole() {
  const q = (document.getElementById('logSearch').value || '').toLowerCase();
  const body = document.getElementById('termBody');
  const rows = termLines.filter(l => {
    if (logFilter === 'INFO' && !l.includes('INFO')) return false;
    if (logFilter === 'WARN' && !l.includes('WARN')) return false;
    if (logFilter === 'ERROR' && !(l.includes('ERROR') || l.includes('FATAL') || l.includes('Exception'))) return false;
    if (q && !l.toLowerCase().includes(q)) return false;
    return true;
  });
  const prevH = body.scrollHeight, prevT = body.scrollTop;
  body.innerHTML = rows.length ? rows.map(highlightLogLine).join('<br>') : '<span class="log-info">Sin coincidencias</span>';
  if (autoScroll && stickBottom) {
    body.scrollTop = body.scrollHeight;
  } else {
    body.scrollTop = Math.max(0, prevT + (body.scrollHeight - prevH));
  }
}
async function refreshConsole() {
  try {
    const res = await fetch(U('/api/console'));
    const data = await res.json();
    if (data.lines && data.lines.length) {
      termLines = data.lines;
      renderConsole();
    }
  } catch (e) {}
}
function refreshCmdList() {
  const seen = new Set();
  const all = CMD_BASE.concat(cmdHistory.slice().reverse()).filter(c => {
    const k = c.trim().toLowerCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  }).slice(0, 30);
  document.getElementById('cmdList').innerHTML = all.map(c => `<option value="${c.replace(/"/g, '&quot;')}">`).join('');
}
async function loadQuick() {
  try {
    const d = await (await fetch(U('/api/uiconfig'))).json();
    const box = document.getElementById('quickCmds');
    box.innerHTML = ((d && d.quick_commands) || []).map(c =>
      `<button class="term-tool-btn" onclick="quickSend(this.dataset.c)" data-c="${c.replace(/"/g, '&quot;')}">${c.replace(/</g, '&lt;')}</button>`
    ).join('');
  } catch (e) {}
}
async function quickSend(c) {
  try {
    const r = await (await fetch(U('/api/cmd'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: c }) })).json();
    showToast(r.msg || 'OK');
    setTimeout(refreshConsole, 700);
  } catch (e) { showToast('Error'); }
}
async function loadQuickCfg() {
  try {
    const d = await (await fetch(U('/api/uiconfig'))).json();
    document.getElementById('quickInput').value = ((d && d.quick_commands) || []).join(', ');
  } catch (e) {}
}
async function saveQuick() {
  const v = document.getElementById('quickInput').value.split(',').map(s => s.trim()).filter(Boolean).slice(0, 12);
  try {
    const r = await (await fetch(U('/api/uiconfig'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quick_commands: v }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadQuick();
}

async function refreshStats() {
  try {
    const res = await fetch(U('/api/stats'));
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
    const mgmt = document.getElementById('playerMgmt');
    const rows = document.getElementById('playerRows');
    const hasPlayers = d.online_players > 0 && d.player_names && d.player_names.length;
    if (mgmt) mgmt.style.display = hasPlayers ? 'block' : 'none';
    if (rows && hasPlayers) {
      const sess = d.sessions || {};
      rows.innerHTML = d.player_names.map(n => {
        const safe = n.replace(/'/g, "").replace(/"/g, '&quot;');
        const t = sess[n] != null ? ` · <span style="font-size:11px;color:var(--text-dim)">${fmtSess(sess[n])}</span>` : '';
        return `<div class="file-row">
          <img class="p-avatar" src="https://mc-heads.net/avatar/${encodeURIComponent(n)}/28" alt="${safe}" onerror="this.src='/icon.svg'">
          <span class="file-name" style="cursor:pointer" onclick="openPlayerModal('${safe}')" title="Ver ficha del jugador"><b>${safe}</b>${t}</span>
          <span class="file-actions">
            <button class="cmd-btn" title="Ficha del jugador" onclick="openPlayerModal('${safe}')">Ficha</button>
            <button class="term-tool-btn" title="Dar OP" onclick="playerAction('op', '${safe}')">OP</button>
            <button class="term-tool-btn" title="Quitar OP" onclick="playerAction('deop', '${safe}')">DeOP</button>
            <button class="term-tool-btn" title="Expulsar" onclick="playerAction('kick', '${safe}')">Kick</button>
            <button class="term-tool-btn" style="color:#f87171; border-color:#7f1d1d" title="Banear" onclick="playerAction('ban', '${safe}')">Ban</button>
          </span>
        </div>`;
      }).join('');
    }

    // Metrics tab badges
    paintBadge(document.getElementById('chartCpuBadge'), d.cpu.toFixed(1) + '%', cpuColor);
    paintBadge(document.getElementById('chartMemBadge'), d.mem_gb + ' GB', memColor);

    // TPS + MSPT (muestreo `tick query` cada 60 s)
    const t = d.tps || {};
    const tpsVal = document.getElementById('tpsVal');
    if (t.ok && t.tps != null) {
      const tc = t.tps >= 19 ? '#34d399' : (t.tps >= 15 ? '#fbbf24' : '#f87171');
      tpsVal.textContent = t.tps.toFixed(1);
      tpsVal.style.backgroundImage = `linear-gradient(180deg, #ffffff 15%, ${tc} 95%)`;
      tpsVal.style.filter = `drop-shadow(0 0 14px ${tc}59)`;
      document.getElementById('tpsBadge').textContent = t.tps >= 19 ? 'ESTABLE' : (t.tps >= 15 ? 'CARGADO' : 'SOBRECARGADO');
      paintBadge(document.getElementById('tpsBadge'), document.getElementById('tpsBadge').textContent, tc);
      document.getElementById('tpsSub').textContent = `MSPT: ${(t.mspt != null ? t.mspt.toFixed(2) : '—')} ms · objetivo 50 ms/tick`;
      const bar = document.getElementById('tpsBar');
      bar.style.width = Math.min(100, (t.tps / 20) * 100) + '%';
      bar.style.background = `linear-gradient(90deg, ${tc}99, ${tc})`;
    } else {
      tpsVal.textContent = d.status === 'RUNNING' ? '…' : '—';
      document.getElementById('tpsSub').textContent = d.status === 'RUNNING' ? 'Muestreando…' : 'Servidor apagado';
    }

    const perfBanner = document.getElementById('perfBanner');
    if (perfBanner) {
      if (d.status === 'RUNNING' && ((t.ok && t.tps != null && t.tps < 16.0) || memPct > 90)) {
        perfBanner.style.display = 'flex';
        document.getElementById('perfBannerMsg').textContent = (t.ok && t.tps < 16.0)
          ? `TPS crítico (${t.tps.toFixed(1)} / 20) detectado.`
          : `Uso de memoria RAM elevado (${memPct.toFixed(1)}%).`;
      } else {
        perfBanner.style.display = 'none';
      }
    }

    document.getElementById('detStatus').textContent = d.status;
    document.getElementById('detPid').textContent = d.pid || '-';
    document.getElementById('detLoad').textContent = d.system_load;
    document.getElementById('detWorld').textContent = d.world_gb + ' GB';
    document.getElementById('detDisk').textContent = (d.disk_gb != null ? d.disk_gb + ' GB' : '—');
    document.getElementById('detNet').textContent = (d.net ? d.net[0] + ' / ' + d.net[1] + ' kB/s' : '—');

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
let h24At = 0;
async function renderCharts() {
  if (Date.now() - h24At > 60000) {
    h24At = Date.now();
    try {
      const h = await (await fetch(U('/api/history'))).json();
      if (h.cpu && h.cpu.length > 1) {
        drawSmoothChart('canvasH24Cpu', h.cpu, 100.0, '#a855f7', 'rgba(168,85,247,0.25)', '%');
        drawSmoothChart('canvasH24Mem', h.mem, 8.0, '#7c3aed', 'rgba(124,58,237,0.30)', 'GB');
        document.getElementById('h24CpuBadge').textContent = h.cpu[h.cpu.length - 1].toFixed(1) + '%';
        const mm = h.mem[h.mem.length - 1];
        document.getElementById('h24MemBadge').textContent = mm + ' GB';
      }
    } catch (e) {}
  }
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
const SVG_BOX = '<svg class="ico" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>';
const SVG_SRV = '<svg class="ico" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>';
function fileTone(name) {
  const e = (String(name).split('.').pop() || '').toLowerCase();
  if (e === 'json') return 'is-json';
  if (['properties', 'yml', 'yaml', 'toml', 'cfg', 'conf', 'ini'].includes(e)) return 'is-cfg';
  if (['log', 'gz', 'bak', 'old', 'tmp'].includes(e)) return 'is-log';
  if (e === 'jar') return 'is-jar';
  if (['zip', '7z', 'rar'].includes(e)) return 'is-zip';
  if (['txt', 'md', 'csv'].includes(e)) return 'is-txt';
  return 'is-file';
}
function fsSortKey(k) {
  fsSort.key = k;
  document.querySelectorAll('#fsSeg button').forEach(b => b.classList.toggle('on', b.dataset.k === k));
  loadFiles(false);
}
function fsToggleDir() { fsSort.dir *= -1; document.getElementById('fsSortDir').textContent = fsSort.dir === 1 ? '↓' : '↑'; loadFiles(false); }
function syncSw(id) {
  const el = document.getElementById(id);
  const sw = document.getElementById(id + 'Sw');
  if (sw && el) { sw.classList.toggle('on', el.checked); sw.setAttribute('aria-checked', String(el.checked)); }
}
function fsToggleSw(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.checked = !el.checked;
  syncSw(id);
  el.dispatchEvent(new Event('change'));
}
function swKey(id, ev) {
  if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); fsToggleSw(id); }
}
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
    const res = await fetch(U('/api/files?path=' + encodeURIComponent(fsPath)));
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
          <span class="file-icon ${f.is_dir ? 'is-folder' : fileTone(f.name)}" title="${f.is_dir ? 'Carpeta' : 'Archivo'}">${f.is_dir ? SVG_FOLDER : SVG_FILE}</span>
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
function fsNav(p, push = true) { fsPath = p; try { localStorage.setItem('pkFs', fsPath); } catch (e) {} loadFiles(push); }
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
  const res = await fetch(U('/api/fs'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, ...extra }) });
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
  const uz = document.getElementById('fsUnzip') && document.getElementById('fsUnzip').checked ? '&unzip=1' : '';
  try {
    const res = await fetch(U('/api/upload?path=' + encodeURIComponent(fsPath) + uz), { method: 'POST', body: fd });
    const r = await res.json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al subir'); }
  inp.value = '';
  loadFiles();
}
function fsDownload(p) { window.open(U('/api/download?path=' + encodeURIComponent(p)), '_blank'); }
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
function fmtBytes(b) {
  if (b == null) return '—';
  if (b > 1e9) return (b / 1e9).toFixed(2) + ' GB';
  if (b > 1e6) return Math.round(b / 1e6) + ' MB';
  return Math.round(b / 1024) + ' KB';
}
function bkNextRun(timeStr) {
  const m = /^([01][0-9]|2[0-3]):([0-5][0-9])$/.exec(timeStr || '');
  if (!m) return null;
  const n = new Date();
  const t = new Date(n);
  t.setHours(parseInt(m[1]), parseInt(m[2]), 0, 0);
  if (t <= n) t.setDate(t.getDate() + 1);
  return t;
}
function bkCountdown(t) {
  const s = Math.max(0, Math.round((t - Date.now()) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return `en ${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `en ${m} min`;
  return 'en menos de 1 min';
}
async function loadBackups() {
  try {
    const res = await fetch(U('/api/backups'));
    const d = await res.json();
    const items = (d.items || []).slice().reverse();
    // Resumen superior
    if (d.enabled) {
      const nx = bkNextRun(d.time);
      document.getElementById('bkNext').textContent = nx ? bkCountdown(nx) : '—';
      document.getElementById('bkNextSub').textContent = nx ? `hoy/mañana · ${d.time}` : `hora inválida (${d.time})`;
    } else {
      document.getElementById('bkNext').textContent = 'Desactivado';
      document.getElementById('bkNextSub').textContent = 'actívalo en Configuración avanzada';
    }
    document.getElementById('bkSpace').textContent = fmtBytes(d.total_b || 0);
    document.getElementById('bkSpaceSub').textContent = `libre en disco: ${d.free_gb} GB`;
    document.getElementById('bkCount').textContent = items.length;
    document.getElementById('bkCountSub').textContent = `${d.monthly || 0} mensuales eternos`;
    const w = d.writable ? '✔ destino escribible' : '✘ destino NO escribible' + (d.writable_msg ? ': ' + d.writable_msg : '');
    document.getElementById('bkStatus').textContent =
      `${d.dir} · ret ${d.retention_days}d${d.keep_monthly ? ' + mensual' : ''} · ${w}`;
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
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin backups todavía — pulsa «Backup ahora»</span></div>'; }
    else {
      box.innerHTML = items.map(f => `
        <div class="file-row" style="align-items:flex-start; padding:14px 18px">
          <span class="file-icon is-file" title="Backup">${BK_SVG_DL}</span>
          <span class="file-name" style="font-size:13.5px">${f.name}${f.monthly ? ' <span class="chart-badge sub">MENSUAL</span>' : ''}<br><span style="font-size:11.5px;color:var(--text-dim)">${f.date} · ${f.size}</span></span>
          <span class="file-actions" style="gap:8px">
          <button class="btn-ghost" onclick="bkDownload('${f.name}')">Descargar</button>
          <button class="btn-ghost" onclick="restoreBackup('${f.name}')">Restaurar</button>
            <button class="icon-btn danger" title="Eliminar" onclick="deleteBackup('${f.name}')">${BK_SVG_TRASH}</button>
          </span>
        </div>`).join('');
    }
    try {
      const c = await (await fetch(U('/api/backup-cfg'))).json();
      const cfg = (c && c.config) || {};
      document.getElementById('bkEn').checked = !!cfg.backup_enabled;
      if (!document.getElementById('bkTime').value) document.getElementById('bkTime').value = cfg.backup_time || '04:00';
      if (!document.getElementById('bkDir').value) document.getElementById('bkDir').value = cfg.backup_dir || '';
      const bkRetEl = document.getElementById('bkRet');
      if (!bkRetEl.dataset.dirty) bkRetEl.value = cfg.retention_days || 7;
      document.getElementById('bkMonthly').checked = cfg.keep_monthly !== false;
      const ret = cfg.retention_days || 7;
      document.getElementById('bkRetNote').textContent =
        `Se conservan los últimos ${ret} días; ${cfg.keep_monthly !== false ? 'el último de cada mes se guarda para siempre (MENSUAL).' : 'sin copias mensuales.'}`;
    } catch (e) {}
  } catch (e) {
    document.getElementById('bkList').innerHTML = '<div class="file-row">Error al cargar backups</div>';
  }
}
async function backupNow() {
  try {
    await sileo.promise(
      fetch(U('/api/backup-now'), { method: 'POST' }).then(async r => {
        const d = await r.json();
        if (!d.ok) throw new Error(d.msg || 'Error');
        return d.msg || 'Backup iniciado';
      }),
      { loading: 'Iniciando backup…', success: m => m, error: e => e.message });
  } catch (e) {}
  setTimeout(loadBackups, 1500);
}
async function restoreBackup(name) {
  if (!confirm(`¿RESTAURAR "${name}"?\n\nDetiene el servidor, guarda un pre-backup de seguridad y sobrescribe el mundo actual.`)) return;
  try {
    await sileo.promise(
      fetch(U('/api/restore'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(async r => {
        const d = await r.json();
        if (!d.ok) throw new Error(d.msg || 'Error');
        return d.msg || 'Restaurando…';
      }),
      { loading: `Restaurando ${name}…`, success: m => m, error: e => e.message });
  } catch (e) {}
  setTimeout(loadBackups, 2000);
}
function bkDownload(name) { window.open(U('/api/backup-download?name=' + encodeURIComponent(name)), '_blank'); }
async function saveBkCfg() {
  const body = { backup_enabled: document.getElementById('bkEn').checked,
    backup_time: document.getElementById('bkTime').value.trim(),
    backup_dir: document.getElementById('bkDir').value.trim(),
    retention_days: parseInt(document.getElementById('bkRet').value) || 7,
    keep_monthly: document.getElementById('bkMonthly').checked };
  try {
    const r = await (await fetch(U('/api/backup-cfg'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadBackups();
}
async function deleteBackup(name) {
  if (!confirm(`¿Eliminar el backup "${name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/backup-delete'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al eliminar'); }
  loadBackups();
}
setInterval(() => { const t = document.getElementById('tab-backups'); if (t && t.classList.contains('active')) loadBackups(); }, 5000);
setInterval(() => { const t = document.getElementById('tab-console'); if (t && t.classList.contains('active')) loadModeration(); }, 10000);
setInterval(() => { const t = document.getElementById('tab-history'); if (t && t.classList.contains('active')) loadHistory(); }, 10000);

async function refreshTps() {
  showToast('Muestreando TPS...');
  try {
    const r = await (await fetch(U('/api/tps'), { method: 'POST' })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error al muestrear'); }
}

async function changePw() {
  const cur = document.getElementById('pwCur').value;
  const nw = document.getElementById('pwNew').value;
  try {
    const r = await (await fetch(U('/api/password'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current: cur, new: nw }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    document.getElementById('pwCur').value = '';
    document.getElementById('pwNew').value = '';
    if (r.login) setTimeout(() => { location.href = '/login'; }, 1200);
    else loadServers();
  } catch (e) { showToast('Error', 'error'); }
}

const DC_LABELS = { up: 'Servidor en línea', down: 'Servidor detenido', tps: 'TPS bajo', backup: 'Backup fallido', join: 'Jugador entra', leave: 'Jugador sale' };
async function loadDiscord() {
  try {
    const d = await (await fetch(U('/api/discord'))).json();
    const c = (d && d.config) || {};
    const inp = document.getElementById('dcHook');
    if (inp && !inp.value) inp.value = c.webhook || '';
    const box = document.getElementById('dcEvents');
    const ev = c.events || {};
    box.innerHTML = Object.keys(DC_LABELS).map(k =>
      `<label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" data-ev="${k}"${ev[k] ? ' checked' : ''}> ${DC_LABELS[k]}</label>`
    ).join('');
  } catch (e) {}
}
async function saveDiscord() {
  const ev = {};
  document.querySelectorAll('#dcEvents input[data-ev]').forEach(el => ev[el.dataset.ev] = el.checked);
  try {
    const r = await (await fetch(U('/api/discord'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ webhook: document.getElementById('dcHook').value.trim(), events: ev }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
}
async function testDiscord() {
  try {
    const r = await (await fetch(U('/api/discord-test'), { method: 'POST' })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
}
function skWhen() {
  return document.getElementById('skMode').value === 'interval'
    ? { every_min: parseInt(document.getElementById('skWhen').value) || 60 }
    : { at: document.getElementById('skWhen').value.trim() || '04:00' };
}
async function loadSchedules() {
  try {
    const d = await (await fetch(U('/api/schedules'))).json();
    const box = document.getElementById('skList');
    const items = (d && d.tasks) || [];
    if (!items.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin tareas — el reinicio automático está desactivado hasta que crees una</span></div>'; return; }
    box.innerHTML = items.map(t => {
      const when = t.mode === 'interval' ? `cada ${t.every_min} min` : `diaria ${t.at}`;
      return `<div class="file-row">
        <span class="file-name">${t.name} <span class="chart-badge sub">${t.kind}</span><br><span style="font-size:11px;color:var(--text-dim)">${when} · ${t.enabled ? 'activa' : 'pausada'}</span></span>
        <span class="file-actions">
          <button class="term-tool-btn" title="Ejecutar ahora" onclick="skAct('run','${t.id}')">Ahora</button>
          <button class="term-tool-btn" title="Activar/pausar" onclick="skAct('toggle','${t.id}')">${t.enabled ? 'Pausar' : 'Activar'}</button>
          <button class="icon-btn danger" title="Eliminar" onclick="skAct('delete','${t.id}')">${BK_SVG_TRASH}</button>
        </span>
      </div>`;
    }).join('');
  } catch (e) {
    document.getElementById('skList').innerHTML = '<div class="file-row">Error al cargar tareas</div>';
  }
}
async function skCreate() {
  const body = { name: document.getElementById('skName').value.trim() || 'Tarea',
    kind: document.getElementById('skKind').value, mode: document.getElementById('skMode').value,
    payload: document.getElementById('skPayload').value.trim(),
    warn_min: parseInt(document.getElementById('skWarn').value) || 0, ...skWhen() };
  if (body.kind === 'restart' && !confirm('¿Crear tarea de REINICIO automático? (vendrá activada; puedes pausarla cuando quieras)')) return;
  try {
    const r = await (await fetch(U('/api/schedule'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'create', ...body }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}
async function skAct(action, id) {
  if (action === 'delete' && !confirm('¿Eliminar tarea?')) return;
  try {
    const r = await (await fetch(U('/api/schedule'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, id }) })).json();
    showToast(r.msg || 'OK');
  } catch (e) { showToast('Error'); }
  loadSchedules();
}

let MODS_CACHE = [];
let MODS_VIEW = [];
async function loadMods() {
  try {
    const d = await (await fetch(U('/api/mods'))).json();
    MODS_CACHE = (d && d.mods) || [];
    renderMods();
  } catch (e) {
    document.getElementById('modsList').innerHTML = '<div class="file-row">Error al cargar mods</div>';
  }
}
function modIsVerTok(t) {
  if (!t) return false;
  let hasDigit = false;
  for (const ch of t) {
    if (ch >= '0' && ch <= '9') { hasDigit = true; continue; }
    if (ch === '.' || ch === 'v' || ch === 'V') continue;
    return false;
  }
  return hasDigit;
}
function modIsLoaderWord(t) {
  const w = String(t).toLowerCase();
  return w === 'neoforge' || w === 'neo' || w === 'forge' || w === 'fabric' || w === 'quilt';
}
function modLoaderName(t) {
  const w = String(t).toLowerCase();
  if (w === 'neoforge' || w === 'neo') return 'NeoForge';
  if (w === 'forge') return 'Forge';
  if (w === 'fabric') return 'Fabric';
  if (w === 'quilt') return 'Quilt';
  return t;
}
function modIsWord(t) {
  if (!t) return false;
  for (const ch of t) {
    if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z'))) return false;
  }
  return true;
}
function modPretty(name) {
  let base = String(name);
  if (base.toLowerCase().endsWith('.jar')) base = base.slice(0, -4);
  let loader = null;
  if (base.charAt(0) === '[') {
    const j = base.indexOf(']');
    if (j > 1) { loader = base.slice(1, j).trim(); base = base.slice(j + 1).trim(); }
  }
  const toks = base.split(/[ _-]+/);
  if (!loader) {
    for (const t of toks) {
      if (modIsLoaderWord(t)) { loader = modLoaderName(t); break; }
    }
  }
  let ver = null;
  const vt = toks.slice();
  if (vt.length && modIsLoaderWord(vt[vt.length - 1])) vt.pop();
  if (vt.length) {
    const last = vt[vt.length - 1];
    if (modIsVerTok(last)) ver = last;
    else if (modIsWord(last) && vt.length > 1 && modIsVerTok(vt[vt.length - 2]))
      ver = vt[vt.length - 2] + ' ' + last;
  }
  return { loader, ver };
}
function renderMods() {
  const box = document.getElementById('modsList');
  const q = ((document.getElementById('modSearch') || {}).value || '').toLowerCase().trim();
  const on = MODS_CACHE.filter(m => m.enabled).length;
  const cc = document.getElementById('modCounts');
  if (cc) cc.innerHTML = `<span class="dot on"></span>${on} activos<span class="dot off" style="margin-left:8px"></span>${MODS_CACHE.length - on} apagados`;
  if (!MODS_CACHE.length) { MODS_VIEW = []; box.innerHTML = '<div class="file-row"><span class="file-name">Sin mods</span></div>'; return; }
  MODS_VIEW = MODS_CACHE.filter(m => !q || m.name.toLowerCase().includes(q));
  if (!MODS_VIEW.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin resultados para esa búsqueda</span></div>'; return; }
  const mb = m => m > 1048576 ? (m / 1048576).toFixed(1) + ' MB' : Math.round(m / 1024) + ' KB';
  box.innerHTML = MODS_VIEW.map((m, i) => {
    const p = modPretty(m.name);
    const chips = (p.loader ? `<span class="mchip ld">${escHtml(p.loader)}</span>` : '')
      + (p.ver ? `<span class="mchip ver">v${escHtml(p.ver.replace(/^v/i, ''))}</span>` : '');
    return `<div class="mod-row${m.enabled ? '' : ' is-off'}">
      <span class="file-icon mod-ico" title="${m.enabled ? 'Activo' : 'Desactivado'}">${SVG_BOX}</span>
      <span class="mod-main">
        <span class="mod-name" title="${escHtml(m.name)}">${escHtml(m.name)}</span>
        <span class="mod-meta">${chips}<span>${mb(m.size)}</span></span>
      </span>
      <span class="mod-state ${m.enabled ? 's-on' : 's-off'}">${m.enabled ? 'ON' : 'OFF'}</span>
      <button class="msw" role="switch" aria-checked="${m.enabled}" aria-label="Activar o desactivar ${escHtml(m.name)}" title="${m.enabled ? 'Desactivar' : 'Activar'} (requiere reinicio)" onclick="modToggleIdx(${i})"></button>
      <button class="icon-btn danger" title="Eliminar" onclick="modDelIdx(${i})">${SVG_TRASH}</button>
    </div>`;
  }).join('');
}
async function modToggleIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  const c = MODS_CACHE.find(x => x.name === m.name);
  if (!c) return;
  c.enabled = !c.enabled;
  renderMods();
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'toggle', name: m.name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok === false) { c.enabled = !c.enabled; renderMods(); }
  } catch (e) { c.enabled = !c.enabled; renderMods(); showToast('Error de conexión', 'error'); }
}
async function modDelIdx(i) {
  const m = MODS_VIEW[i];
  if (!m) return;
  if (!confirm(`¿Eliminar el mod "${m.name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', name: m.name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modToggle(name) { // compatibilidad
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'toggle', name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modDel(name) { // compatibilidad
  if (!confirm(`¿Eliminar el mod "${name}"?`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', name }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
async function modBulk(enable) {
  const n = MODS_CACHE.filter(m => m.enabled !== enable).length;
  if (!n) { showToast('Nada que cambiar', 'info'); return; }
  if (!confirm(`${enable ? 'Activar' : 'Desactivar'} ${n} mod(s)? Requiere reinicio del servidor.`)) return;
  try {
    const r = await (await fetch(U('/api/mod'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: enable ? 'enable_all' : 'disable_all' }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
  } catch (e) { showToast('Error', 'error'); }
  loadMods();
}
let SRV_CACHE = [];
async function loadServers() {
  const box = document.getElementById('srvList');
  if (!box) return;
  try {
    const d = await (await fetch(U('/api/servers'))).json();
    SRV_CACHE = (d && d.servers) || [];
    renderServers();
    const ps = document.getElementById('pwState');
    if (ps) ps.textContent = d.auth
      ? 'Protegido con contraseña. Para cambiarla escribe la actual y la nueva.'
      : 'Sin contraseña: cualquiera con acceso al panel puede entrar. Escribe solo la nueva para crearla.';
  } catch (e) {
    box.innerHTML = '<div class="file-row">Error al cargar servidores</div>';
  }
}
function renderServers() {
  const box = document.getElementById('srvList');
  if (!box) return;
  const cc = document.getElementById('srvCounts');
  const run = SRV_CACHE.filter(s => s.running).length;
  if (cc) cc.innerHTML = `<span class="dot on"></span>${run} en línea<span class="dot off" style="margin-left:8px"></span>${SRV_CACHE.length - run} apagados`;
  if (!SRV_CACHE.length) { box.innerHTML = '<div class="file-row"><span class="file-name">Sin servidores</span></div>'; return; }
  box.innerHTML = SRV_CACHE.map(s => `
    <div class="mod-row${s.running ? '' : ' is-off'}">
      <span class="file-icon mod-ico" title="${s.running ? 'En línea' : 'Apagado'}">${SVG_SRV}</span>
      <span class="mod-main">
        <span class="mod-name" title="${escHtml(s.server_dir)}">${escHtml(s.name)}${s.current ? ' <span class="mchip ld">ACTUAL</span>' : ''}</span>
        <span class="mod-meta"><span>${escHtml(s.server_dir)}</span><span>MC :${s.mc_port}</span></span>
      </span>
      <span class="mod-state ${s.running ? 's-on' : 's-off'}">${s.running ? 'ON' : 'OFF'}</span>
      <span class="file-actions">
        ${s.current ? '' : `<button class="term-tool-btn" onclick="srvOpen('${s.id}')">Abrir</button>`}
        <button class="icon-btn" title="Editar" onclick="srvShowForm('${s.id}')">${SVG_PENCIL}</button>
        <button class="icon-btn danger" title="Eliminar" onclick="srvDelete('${s.id}')">${SVG_TRASH}</button>
      </span>
    </div>`).join('');
}
function srvOpen(id) {
  location.href = '?server=' + encodeURIComponent(id) + location.hash;
}
function srvShowForm(id) {
  const f = document.getElementById('srvForm');
  const s = id ? SRV_CACHE.find(x => x.id === id) : null;
  document.getElementById('srvFormTitle').textContent = s ? `Editar '${s.name}'` : 'Añadir servidor';
  document.getElementById('srvEditId').value = s ? s.id : '';
  document.getElementById('srvName').value = s ? s.name : '';
  document.getElementById('srvDir').value = s ? s.server_dir : '';
  document.getElementById('srvMc').value = s ? s.mc_port : (SRV_CACHE.reduce((m, x) => Math.max(m, x.mc_port || 0), 25565) + 1);
  document.getElementById('srvRcon').value = s ? s.rcon_port : (SRV_CACHE.reduce((m, x) => Math.max(m, x.rcon_port || 0), 25575) + 1);
  document.getElementById('srvMem').value = s ? s.max_mem_gb : 4;
  f.style.display = 'block';
  f.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (!s) document.getElementById('srvName').focus();
}
function srvHideForm() {
  document.getElementById('srvForm').style.display = 'none';
  document.getElementById('srvEditId').value = '';
}
async function srvSubmit() {
  const id = document.getElementById('srvEditId').value;
  const body = {
    action: id ? 'update' : 'add',
    name: document.getElementById('srvName').value.trim(),
    server_dir: document.getElementById('srvDir').value.trim(),
    mc_port: parseInt(document.getElementById('srvMc').value, 10),
    rcon_port: parseInt(document.getElementById('srvRcon').value, 10),
    max_mem_gb: parseFloat(document.getElementById('srvMem').value)
  };
  if (id) body.id = id;
  try {
    const r = await (await fetch(U('/api/servers'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok !== false) srvHideForm();
  } catch (e) { showToast('Error de conexión', 'error'); }
  loadServers();
}
async function srvDelete(id) {
  const s = SRV_CACHE.find(x => x.id === id);
  if (!s) return;
  if (!confirm(`¿Quitar '${s.name}' del panel? No se borran sus archivos.`)) return;
  try {
    const r = await (await fetch(U('/api/servers'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', id }) })).json();
    showToast(r.msg || 'OK', r.ok === false ? 'error' : 'success');
    if (r.ok !== false) {
      if (s.current && r.switchTo) location.href = '?server=' + encodeURIComponent(r.switchTo) + location.hash;
      else loadServers();
    }
  } catch (e) { showToast('Error de conexión', 'error'); }
}
let SRV_BROWSE = '';
let SRV_BROWSE_DIRS = [];
function openSrvBrowser() {
  const cur = (document.getElementById('srvDir') || {}).value || '';
  document.getElementById('srvBrowseModal').style.display = 'flex';
  srvBrowseGo(cur.trim() || '~');
}
function srvBrowseClose() {
  document.getElementById('srvBrowseModal').style.display = 'none';
}
function srvBrowseCrumb(p) {
  const segs = String(p).split('/').filter(Boolean);
  let acc = '';
  let html = `<a onclick="srvBrowseGo('/');return false">/</a>`;
  segs.forEach(sg => {
    acc += '/' + sg;
    const target = acc;
    html += `<span style="color:var(--text-dim)"> / </span><a onclick="srvBrowseGoIdx('${target.replace(/'/g, '')}');return false">${escHtml(sg)}</a>`;
  });
  return html;
}
function srvBrowseGoIdx(path) { srvBrowseGo(path); }
async function srvBrowseGo(path) {
  const box = document.getElementById('srvBrowseList');
  try {
    const r = await (await fetch(U('/api/browse?path=' + encodeURIComponent(path)))).json();
    if (!r || r.ok === false) { showToast((r && r.msg) || 'No accesible', 'error'); return; }
    SRV_BROWSE = r.path;
    const base = r.path.endsWith('/') ? r.path.slice(0, -1) : r.path;
    SRV_BROWSE_DIRS = (r.dirs || []).map(d => base + '/' + d.name);
    document.getElementById('srvBrowsePath').innerHTML = srvBrowseCrumb(r.path);
    const dirs = r.dirs || [];
    box.innerHTML = dirs.length ? dirs.map((d, i) => `
      <div class="file-row" onclick="srvBrowseEnter(${i})" style="cursor:pointer">
        <span class="file-icon is-folder" title="Carpeta">${SVG_FOLDER}</span>
        <span class="file-name">${escHtml(d.name)}${d.ready ? ' <span class="mchip ld">server</span>' : ''}</span>
      </div>`).join('') : '<div class="file-row"><span class="file-name">Sin subcarpetas</span></div>';
  } catch (e) { showToast('Error de conexión', 'error'); }
}
function srvBrowseEnter(i) {
  const p = SRV_BROWSE_DIRS[i];
  if (p) srvBrowseGo(p);
}
function srvBrowseUp() {
  srvBrowseGo(SRV_BROWSE ? SRV_BROWSE + '/..' : '~');
}
function srvBrowsePick() {
  if (!SRV_BROWSE) return;
  document.getElementById('srvDir').value = SRV_BROWSE;
  srvBrowseClose();
  showToast('Carpeta seleccionada', 'success');
}

function durStep(id, delta, min, max) {
  const el = document.getElementById(id);
  el.dataset.dirty = 1;
  el.value = Math.max(min, Math.min(max, (parseInt(el.value) || 0) + delta));
}
// Sidebar por proximidad (estilo Rare UI)
document.querySelector('aside').addEventListener('mousemove', e => {
  document.querySelectorAll('.nav-item').forEach(el => {
    const r = el.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2);
    const dy = e.clientY - (r.top + r.height / 2);
    const dist = Math.hypot(dx, dy);
    el.style.setProperty('--prox', Math.max(0, 1 - dist / 220).toFixed(2));
  });
});
document.querySelector('aside').addEventListener('mouseleave', () => {
  document.querySelectorAll('.nav-item').forEach(el => el.style.setProperty('--prox', 0));
});
// Progreso de scroll del contenido
document.querySelector('main').addEventListener('scroll', e => {
  const m = e.target;
  const p = m.scrollHeight - m.clientHeight > 0 ? (m.scrollTop / (m.scrollHeight - m.clientHeight)) * 100 : 0;
  document.getElementById('scrollProgress').style.width = p + '%';
});
let heatAt = 0;
async function loadHeat() {
  if (Date.now() - heatAt < 60000) return;
  heatAt = Date.now();
  try {
    const d = await (await fetch(U('/api/activity?days=14'))).json();
    const days = (d && d.days) || [];
    const box = document.getElementById('heatMap');
    if (!days.length) { box.innerHTML = ''; return; }
    const mx = Math.max(1, ...days.map(x => x.joins));
    let joins = 0, bks = 0;
    box.innerHTML = days.map(x => {
      joins += x.joins;
      if (x.backups) bks++;
      const a = 0.1 + 0.9 * (x.joins / mx);
      const h = 22 + 52 * (x.joins / mx);
      return `<div class="heat-cell${x.backups ? ' has-bk' : ''}" style="height:${h}px; background:rgba(168,85,247,${a.toFixed(2)})" title="${x.date}: ${x.joins} joins${x.backups ? ` · ${x.backups} backup(s)` : ''}"></div>`;
    }).join('');
    document.getElementById('heatBadge').textContent = `${joins} joins · ${bks} días con backup`;
  } catch (e) {}
}

async function loadProps() {
  try {
    const res = await fetch(U('/api/file?path=server.properties'));
    const data = await res.json();
    document.getElementById('propsText').value = data.content || '';
  } catch (e) {}
}

async function saveProps() {
  const content = document.getElementById('propsText').value;
  try {
    const res = await fetch(U('/api/file'), {
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

/* ══════════════════════════════════════════════════
   PLAYER INSPECTOR (Ficha detallada & acciones rápidas)
══════════════════════════════════════════════════ */
let CURRENT_PM_PLAYER = '';
async function openPlayerModal(name) {
  CURRENT_PM_PLAYER = name;
  const modal = document.getElementById('playerModal');
  modal.style.display = 'flex';
  document.getElementById('pmName').textContent = name;
  document.getElementById('pmAvatar').src = 'https://mc-heads.net/body/' + encodeURIComponent(name) + '/120';
  document.getElementById('pmUuid').textContent = 'Consultando...';
  document.getElementById('pmHours').textContent = '…';
  document.getElementById('pmDeaths').textContent = '…';
  document.getElementById('pmMobKills').textContent = '…';
  document.getElementById('pmDist').textContent = '…';
  document.getElementById('pmCoords').textContent = '…';
  document.getElementById('pmLevel').textContent = '…';

  try {
    const r = await (await fetch(U('/api/player-info?name=' + encodeURIComponent(name)))).json();
    if (r.ok) {
      document.getElementById('pmUuid').textContent = r.uuid || 'Sin UUID registrado';
      document.getElementById('pmHours').textContent = r.hours_played != null ? r.hours_played + ' h' : '—';
      document.getElementById('pmDeaths').textContent = r.deaths != null ? r.deaths : '0';
      document.getElementById('pmMobKills').textContent = r.mob_kills != null ? r.mob_kills.toLocaleString() : '0';
      document.getElementById('pmDist').textContent = r.km_traveled != null ? r.km_traveled + ' km' : '—';
      document.getElementById('pmCoords').textContent = r.coords ? `X:${r.coords[0]} Y:${r.coords[1]} Z:${r.coords[2]}` : (r.level != null ? 'En línea' : 'Desconectado');
      document.getElementById('pmLevel').textContent = r.level != null ? r.level : '—';
    }
  } catch (e) {
    document.getElementById('pmUuid').textContent = 'Error al cargar estadísticas';
  }
}
function closePlayerModal() {
  document.getElementById('playerModal').style.display = 'none';
}
async function pmQuickCmd(act) {
  if (!CURRENT_PM_PLAYER) return;
  const p = CURRENT_PM_PLAYER;
  let cmd = '';
  if (act === 'heal') cmd = `effect give ${p} instant_health 1 255`;
  else if (act === 'feed') cmd = `effect give ${p} saturation 1 255`;
  else if (act === 'creative') cmd = `gamemode creative ${p}`;
  else if (act === 'survival') cmd = `gamemode survival ${p}`;
  else if (act === 'spawn') cmd = `spawnpoint ${p}`;
  else if (act === 'clearlag') {
    if (!confirm(`¿Vaciar todo el inventario de "${p}"?`)) return;
    cmd = `clear ${p}`;
  }
  if (!cmd) return;
  try {
    const r = await (await fetch(U('/api/cmd'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd }) })).json();
    showToast(r.msg || 'Comando enviado');
    addNotif(`Comando sobre ${p}: ${act}`, 'Ahora mismo', 'zap');
  } catch (e) {
    showToast('Error al enviar acción', 'error');
  }
}

/* ══════════════════════════════════════════════════
   MONITOR DE RED Y LATENCIA
══════════════════════════════════════════════════ */
async function refreshPingMetrics() {
  const elMc = document.getElementById('pingLocalMc');
  const elRcon = document.getElementById('pingLocalRcon');
  const elTun = document.getElementById('pingTunnel');
  if (elMc) elMc.textContent = '…';
  if (elRcon) elRcon.textContent = '…';
  if (elTun) elTun.textContent = '…';

  try {
    const r = await (await fetch(U('/api/network-ping'))).json();
    const paintPing = (el, subEl, ms, labelDefault) => {
      if (!el) return;
      if (ms == null) {
        el.textContent = 'Inactivo';
        el.style.color = 'var(--text-dim)';
      } else {
        el.textContent = ms + ' ms';
        el.style.color = ms < 15 ? '#34d399' : (ms < 60 ? '#fbbf24' : '#f87171');
      }
    };
    paintPing(elMc, document.getElementById('pingLocalMcSub'), r.local_mc_ms);
    paintPing(elRcon, document.getElementById('pingLocalRconSub'), r.local_rcon_ms);
    paintPing(elTun, document.getElementById('pingTunnelSub'), r.tunnel_ms);
    if (r.public_target) {
      const sub = document.getElementById('pingTunnelSub');
      if (sub) sub.textContent = r.public_target;
    }
  } catch (e) {}
}

/* ══════════════════════════════════════════════════
   SPARK PROFILER
══════════════════════════════════════════════════ */
async function sparkAction(act) {
  const box = document.getElementById('sparkResultBox');
  box.style.display = 'block';
  box.textContent = 'Ejecutando diagnóstico spark en el servidor (' + act + ')...';
  try {
    const r = await (await fetch(U('/api/spark'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: act }) })).json();
    box.textContent = r.msg || 'Completado';
    if (r.msg && r.msg.includes('https://spark.lucko.me/')) {
      const idx = r.msg.indexOf('https://spark.lucko.me/');
      const link = idx >= 0 ? r.msg.slice(idx).split(' ')[0].split(String.fromCharCode(10))[0] : '';
      if (link) {
        showToast('Reporte spark generado: ' + link, 'success');
        addNotif('Reporte Spark disponible: ' + link, 'Ahora', 'activity');
      }
    } else {
      showToast(r.ok ? 'Spark OK' : (r.msg || 'Error'), r.ok ? 'success' : 'error');
    }
  } catch (e) {
    box.textContent = 'Error al comunicar con spark.';
  }
}
function sparkQuickAction(act) {
  switchTab('metrics');
  setTimeout(() => sparkAction(act), 400);
}

/* ══════════════════════════════════════════════════
   MODRINTH EXPLORER (Buscador & Instalador 1-click)
══════════════════════════════════════════════════ */
function toggleModrinthView() {
  const s = document.getElementById('modrinthSection');
  s.style.display = s.style.display === 'none' ? 'block' : 'none';
  if (s.style.display === 'block') {
    const inp = document.getElementById('mrSearchInput');
    inp.focus();
    if (!document.getElementById('mrResults').dataset.loaded) {
      searchModrinth();
    }
  }
}
async function searchModrinth() {
  const q = document.getElementById('mrSearchInput').value.trim();
  const box = document.getElementById('mrResults');
  box.dataset.loaded = '1';
  box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px">Buscando en Modrinth para NeoForge 1.21.1...</div>';
  try {
    const r = await (await fetch(U('/api/modrinth/search?q=' + encodeURIComponent(q)))).json();
    if (!r.ok || !r.hits || !r.hits.length) {
      box.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px">No se encontraron mods para esa búsqueda.</div>';
      return;
    }
    box.innerHTML = r.hits.map(h => {
      const icon = h.icon || '/icon.svg';
      const safeTitle = escHtml(h.title);
      const safeDesc = escHtml(h.description || 'Sin descripción');
      const dlK = h.downloads > 1000000 ? (h.downloads / 1000000).toFixed(1) + 'M' : Math.round(h.downloads / 1000) + 'k';
      return `<div class="mr-card">
        <div style="display:flex; gap:12px; align-items:flex-start">
          <img src="${icon}" class="mr-icon" alt="" onerror="this.src='/icon.svg'">
          <div style="flex:1; min-width:0">
            <div style="font-weight:700; font-size:13.5px; color:#fff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${safeTitle}</div>
            <div style="font-size:11px; color:var(--text-dim)">por ${escHtml(h.author || 'Desconocido')} · ${dlK} descargas</div>
          </div>
        </div>
        <p style="font-size:11.5px; color:#cbd5e1; margin:10px 0 14px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden">${safeDesc}</p>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span class="mchip ld">NeoForge</span>
          <button class="cmd-btn" style="padding:4px 12px; font-size:11.5px" onclick="installModrinth('${escHtml(h.slug || h.id)}', '${safeTitle.replace(/'/g, "")}')">Instalar</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    box.innerHTML = '<div style="color:#f87171;font-size:12px;padding:12px">Error conectando con la API de Modrinth.</div>';
  }
}
async function installModrinth(slug, title) {
  showToast(`Descargando ${title} desde Modrinth...`);
  try {
    const r = await (await fetch(U('/api/modrinth/install'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, loader: 'neoforge', game_ver: '1.21.1' })
    })).json();
    showToast(r.msg || 'Instalado', r.ok ? 'success' : 'error');
    if (r.ok) {
      addNotif(`Mod instalado: ${title}`, 'Ahora', 'download');
      loadMods();
    }
  } catch (e) {
    showToast('Error al descargar mod', 'error');
  }
}

/* ══════════════════════════════════════════════════
   WORLD MANAGER & MCA REGION CLEANER
══════════════════════════════════════════════════ */
let WORLD_DATA = null;
let MCA_ACTIVE_DIM = 'overworld';
async function loadWorldManager() {
  const container = document.getElementById('mcaListContainer');
  if (container) container.innerHTML = '<div class="mca-row" style="color:var(--text-dim)">Cargando análisis del mundo...</div>';
  try {
    const d = await (await fetch(U('/api/world'))).json();
    if (!d || !d.exists) {
      if (container) container.innerHTML = '<div class="mca-row">La carpeta del mundo no existe en este servidor.</div>';
      return;
    }
    WORLD_DATA = d;
    const mb = bytes => bytes > 1073741824 ? (bytes / 1073741824).toFixed(2) + ' GB' : (bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB');
    
    (d.dimensions || []).forEach(dim => {
      if (dim.id === 'overworld') {
        document.getElementById('dimOwSize').textContent = mb(dim.size);
        document.getElementById('dimOwRegions').textContent = dim.regions_count + ' regiones';
      } else if (dim.id === 'nether') {
        document.getElementById('dimNetherSize').textContent = mb(dim.size);
        document.getElementById('dimNetherRegions').textContent = dim.regions_count + ' regiones';
      } else if (dim.id === 'end') {
        document.getElementById('dimEndSize').textContent = mb(dim.size);
        document.getElementById('dimEndRegions').textContent = dim.regions_count + ' regiones';
      }
    });

    renderMcaList();
  } catch (e) {
    if (container) container.innerHTML = '<div class="mca-row">Error al cargar datos del mundo.</div>';
  }
}
function selectMcaDim(dim) {
  MCA_ACTIVE_DIM = dim;
  document.querySelectorAll('#mcaDimSeg button').forEach(b => {
    b.classList.toggle('on', b.dataset.dim === dim || b.textContent.toLowerCase().includes(dim === 'overworld' ? 'over' : dim === 'nether' ? 'nether' : 'end'));
  });
  renderMcaList();
}
function renderMcaList() {
  const container = document.getElementById('mcaListContainer');
  if (!container || !WORLD_DATA) return;
  const dim = (WORLD_DATA.dimensions || []).find(d => d.id === MCA_ACTIVE_DIM);
  if (!dim || !dim.regions || !dim.regions.length) {
    container.innerHTML = '<div class="mca-row" style="color:var(--text-dim)">Sin archivos de región en esta dimensión.</div>';
    document.getElementById('mcaSelectedCount').textContent = '0 regiones seleccionadas';
    return;
  }
  const mb = bytes => bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB';
  container.innerHTML = dim.regions.map((r, i) => {
    const x0 = r.x * 512, x1 = (r.x + 1) * 512 - 1;
    const z0 = r.z * 512, z1 = (r.z + 1) * 512 - 1;
    return `<div class="mca-row">
      <input type="checkbox" class="mca-chk" data-file="${r.file}" onchange="updateMcaSelectionCount()">
      <span style="color:#e2e8f0; font-weight:700; width:130px">${r.file}</span>
      <span style="color:var(--text-dim); flex:1">Bloques: [X ${x0}..${x1}, Z ${z0}..${z1}]</span>
      <span style="color:#93c5fd">${mb(r.size)}</span>
    </div>`;
  }).join('');
  updateMcaSelectionCount();
}
function updateMcaSelectionCount() {
  const checked = document.querySelectorAll('.mca-chk:checked').length;
  document.getElementById('mcaSelectedCount').textContent = `${checked} regiones seleccionadas (${checked * 1024} chunks)`;
}
function toggleSelectAllMca(check) {
  document.querySelectorAll('.mca-chk').forEach(c => c.checked = check);
  updateMcaSelectionCount();
}
function calcMcaFromCoords() {
  const x = parseInt(document.getElementById('calcCoordX').value, 10);
  const z = parseInt(document.getElementById('calcCoordZ').value, 10);
  const res = document.getElementById('calcMcaResult');
  if (isNaN(x) || isNaN(z)) {
    res.textContent = 'Introduce X y Z';
    return;
  }
  const rx = Math.floor(x / 512);
  const rz = Math.floor(z / 512);
  res.textContent = `Región: r.${rx}.${rz}.mca (Chunks ${rx*32}..${rx*32+31}, ${rz*32}..${rz*32+31})`;
}
function selectCalculatedMca() {
  const x = parseInt(document.getElementById('calcCoordX').value, 10);
  const z = parseInt(document.getElementById('calcCoordZ').value, 10);
  if (isNaN(x) || isNaN(z)) return;
  const rx = Math.floor(x / 512);
  const rz = Math.floor(z / 512);
  const targetFn = `r.${rx}.${rz}.mca`;
  const chk = document.querySelector(`.mca-chk[data-file="${targetFn}"]`);
  if (chk) {
    chk.checked = true;
    chk.scrollIntoView({ behavior: 'smooth', block: 'center' });
    updateMcaSelectionCount();
    showToast(`Región ${targetFn} seleccionada`);
  } else {
    showToast(`El archivo ${targetFn} no existe en ${MCA_ACTIVE_DIM} (chunks no generados aún)`, 'info');
  }
}
async function deleteSelectedMca() {
  const files = Array.from(document.querySelectorAll('.mca-chk:checked')).map(c => c.dataset.file);
  if (!files.length) {
    showToast('Selecciona al menos una región', 'info');
    return;
  }
  if (!confirm(`⚠️ ATENCIÓN: ¿Seguro que deseas eliminar ${files.length} archivos .mca (${files.length * 1024} chunks) de ${MCA_ACTIVE_DIM.toUpperCase()}?\n\nLos terrenos se borrarán y se regenerarán limpios la próxima vez que alguien entre.`)) return;
  try {
    const r = await (await fetch(U('/api/world/mca'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete', dimension: MCA_ACTIVE_DIM, files })
    })).json();
    showToast(r.msg || 'Regiones eliminadas', r.ok ? 'success' : 'error');
    if (r.ok) {
      addNotif(`Borrado MCA: ${files.length} regiones en ${MCA_ACTIVE_DIM}`, 'Ahora', 'trash');
      loadWorldManager();
    }
  } catch (e) {
    showToast('Error de conexión', 'error');
  }
}
async function mcaResetDim(dim) {
  if (!confirm(`⚠️ ALERTA: ¿Deseas reiniciar por completo la dimensión ${dim.toUpperCase()}?\n\nToda construcción, portal o progreso en esa dimensión desaparecerá.`)) return;
  try {
    const r = await (await fetch(U('/api/world/mca'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'reset_dimension', dimension: dim })
    })).json();
    showToast(r.msg || 'Dimensión reiniciada', r.ok ? 'success' : 'error');
    if (r.ok) {
      addNotif(`Dimensión reiniciada: ${dim.toUpperCase()}`, 'Ahora', 'rotate-ccw');
      loadWorldManager();
    }
  } catch (e) {
    showToast('Error al reiniciar dimensión', 'error');
  }
}

/* ══════════════════════════════════════════════════
   FLOTA MULTI-SERVIDOR (Fleet View)
══════════════════════════════════════════════════ */
async function loadFleetView() {
  const box = document.getElementById('fleetCardsContainer');
  if (!box) return;
  try {
    const d = await (await fetch(U('/api/fleet'))).json();
    const srvs = (d && d.servers) || [];
    if (!srvs.length) {
      box.innerHTML = '<div class="scard" style="padding:16px">Sin servidores registrados.</div>';
      return;
    }
    box.innerHTML = srvs.map(s => `
      <div class="scard" style="padding:18px; display:flex; flex-direction:column; justify-content:space-between; ${s.current ? 'border-color:#a855f7; box-shadow:0 0 20px rgba(168,85,247,0.3)' : ''}">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px">
            <div>
              <div style="font-size:18px; font-weight:800; color:#fff">${escHtml(s.name)}${s.current ? ' <span class="mchip ld">ACTUAL</span>' : ''}</div>
              <div style="font-size:11.5px; color:var(--text-dim)">${escHtml(s.server_dir)}</div>
            </div>
            <span class="status-pill ${s.running ? 'RUNNING' : 'OFFLINE'}">
              <span class="status-pulse"></span>
              <span>${s.running ? 'ONLINE' : 'OFFLINE'}</span>
            </span>
          </div>
          <div style="display:flex; gap:16px; margin:14px 0; font-size:12.5px">
            <div><span style="color:var(--text-dim)">Puerto:</span> <b>:${s.mc_port}</b></div>
            <div><span style="color:var(--text-dim)">RAM Máx:</span> <b>${s.max_mem_gb} GB</b></div>
          </div>
        </div>
        <div style="display:flex; gap:8px; margin-top:10px">
          ${s.current ? '<button class="term-tool-btn" style="flex:1" disabled>Servidor en uso</button>' : `<button class="cmd-btn" style="flex:1" onclick="location.search='?server='+encodeURIComponent('${s.id}')">Conmutar a este</button>`}
          <button class="term-tool-btn" onclick="serverAction('${s.running ? 'restart' : 'start'}')">${s.running ? 'Reiniciar' : 'Iniciar'}</button>
        </div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="scard" style="padding:16px">Error al cargar flota de servidores.</div>';
  }
}

/* ══════════════════════════════════════════════════
   GLOBAL COMMAND PALETTE (Ctrl + K)
══════════════════════════════════════════════════ */
const PALETTE_ACTIONS = [
  { title: 'Consola del servidor', cat: 'Navegar', action: () => switchTab('console') },
  { title: 'Métricas del sistema & TPS', cat: 'Navegar', action: () => switchTab('metrics') },
  { title: 'Archivos y Mods', cat: 'Navegar', action: () => switchTab('files') },
  { title: 'Mundos y MCA Region Cleaner', cat: 'Navegar', action: () => switchTab('worlds') },
  { title: 'Copias de seguridad (Backups)', cat: 'Navegar', action: () => switchTab('backups') },
  { title: 'Tareas programadas', cat: 'Navegar', action: () => switchTab('tasks') },
  { title: 'Historial de conexiones', cat: 'Navegar', action: () => switchTab('history') },
  { title: 'Flota de servidores', cat: 'Navegar', action: () => switchTab('fleet') },
  { title: 'Configuración y Ajustes', cat: 'Navegar', action: () => switchTab('settings') },
  { title: 'Iniciar Servidor', cat: 'Control', action: () => serverAction('start') },
  { title: 'Reiniciar Servidor', cat: 'Control', action: () => serverAction('restart') },
  { title: 'Detener Servidor', cat: 'Control', action: () => serverAction('stop') },
  { title: 'Reload Servidor (Datapacks/Mods)', cat: 'Control', action: () => serverAction('reload') },
  { title: 'Hacer Backup ahora', cat: 'Backups', action: () => { switchTab('backups'); setTimeout(backupNow, 300); } },
  { title: 'Diagnóstico de Salud Spark', cat: 'Rendimiento', action: () => sparkQuickAction('health') },
  { title: 'Explorador de Mods Modrinth', cat: 'Mods', action: () => { switchTab('files'); setTimeout(toggleModrinthView, 300); } },
  { title: 'Pantalla completa de terminal', cat: 'Herramientas', action: () => toggleTermFullscreen() },
  { title: 'Copiar IP pública', cat: 'Herramientas', action: () => copyIp() },
  { title: 'Cerrar sesión', cat: 'Sesión', action: () => logout() }
];

let PALETTE_ACTIVE_IDX = 0;
let PALETTE_FILTERED = [];

function openCmdPalette() {
  document.getElementById('cmdPalette').style.display = 'flex';
  const inp = document.getElementById('cpInput');
  inp.value = '';
  renderCmdPalette();
  inp.focus();
}
function closeCmdPalette() {
  document.getElementById('cmdPalette').style.display = 'none';
}
function renderCmdPalette() {
  const q = document.getElementById('cpInput').value.toLowerCase().trim();
  PALETTE_FILTERED = PALETTE_ACTIONS.filter(a => !q || a.title.toLowerCase().includes(q) || a.cat.toLowerCase().includes(q));
  PALETTE_ACTIVE_IDX = 0;
  const list = document.getElementById('cpList');
  if (!PALETTE_FILTERED.length) {
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-dim);font-size:13px">Sin coincidencias</div>';
    return;
  }
  list.innerHTML = PALETTE_FILTERED.map((item, idx) => `
    <div class="cp-item${idx === PALETTE_ACTIVE_IDX ? ' active' : ''}" onclick="runPaletteItem(${idx})">
      <span>${escHtml(item.title)}</span>
      <span class="cp-badge">${escHtml(item.cat)}</span>
    </div>`).join('');
}
function handlePaletteKey(e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (PALETTE_FILTERED.length) {
      PALETTE_ACTIVE_IDX = (PALETTE_ACTIVE_IDX + 1) % PALETTE_FILTERED.length;
      updatePaletteSelection();
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (PALETTE_FILTERED.length) {
      PALETTE_ACTIVE_IDX = (PALETTE_ACTIVE_IDX - 1 + PALETTE_FILTERED.length) % PALETTE_FILTERED.length;
      updatePaletteSelection();
    }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    runPaletteItem(PALETTE_ACTIVE_IDX);
  } else if (e.key === 'Escape') {
    closeCmdPalette();
  }
}
function updatePaletteSelection() {
  document.querySelectorAll('.cp-item').forEach((el, idx) => {
    el.classList.toggle('active', idx === PALETTE_ACTIVE_IDX);
    if (idx === PALETTE_ACTIVE_IDX) el.scrollIntoView({ block: 'nearest' });
  });
}
function runPaletteItem(idx) {
  const item = PALETTE_FILTERED[idx];
  if (item && item.action) {
    closeCmdPalette();
    item.action();
  }
}
window.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    const p = document.getElementById('cmdPalette');
    if (p.style.display === 'flex') closeCmdPalette();
    else openCmdPalette();
  } else if (e.key === 'Escape') {
    closeCmdPalette();
    closePlayerModal();
    if (document.querySelector('.terminal-wrapper.fullscreen')) toggleTermFullscreen();
  }
});

/* ══════════════════════════════════════════════════
   TERMINAL FULLSCREEN & THEMES & EDITOR UPGRADES
══════════════════════════════════════════════════ */
function toggleTermFullscreen() {
  const w = document.querySelector('.terminal-wrapper');
  w.classList.toggle('fullscreen');
}
function setTermTheme(th) {
  const w = document.querySelector('.terminal-wrapper');
  ['matrix', 'monokai', 'tokyo', 'dracula'].forEach(t => w.classList.remove('term-theme-' + t));
  if (th !== 'default') {
    w.classList.add('term-theme-' + th);
  }
  try { localStorage.setItem('pkTermTheme', th); } catch(e) {}
}
(function restoreTermTheme() {
  try {
    const saved = localStorage.getItem('pkTermTheme');
    if (saved) {
      const sel = document.getElementById('termThemeSel');
      if (sel) sel.value = saved;
      setTermTheme(saved);
    }
  } catch(e) {}
})();

function fsUpdateEditorStats() {
  const ed = document.getElementById('fsEditor');
  const lines = ed.value.split(String.fromCharCode(10)).length;
  const chars = ed.value.length;
  document.getElementById('fsEditStats').textContent = `${lines} líneas · ${chars} caracteres`;
}
function fsHandleEditorTab(e) {
  if (e.key === 'Tab') {
    e.preventDefault();
    const el = e.target;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    el.value = el.value.substring(0, start) + '  ' + el.value.substring(end);
    el.selectionStart = el.selectionEnd = start + 2;
    fsUpdateEditorStats();
  }
}
let FS_FONT_SIZE = 12.5;
function fsChangeFontSize(delta) {
  FS_FONT_SIZE = Math.max(10, Math.min(22, FS_FONT_SIZE + delta));
  document.getElementById('fsEditor').style.fontSize = FS_FONT_SIZE + 'px';
}

/* ══════════════════════════════════════════════════
   CENTRO DE NOTIFICACIONES (Activity Feed)
══════════════════════════════════════════════════ */
let NOTIFS = [];
function addNotif(title, timeStr, iconKind) {
  NOTIFS.unshift({ title, time: timeStr || new Date().toLocaleTimeString(), icon: iconKind || 'info' });
  if (NOTIFS.length > 25) NOTIFS.pop();
  renderNotifs();
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'block';
}
function renderNotifs() {
  const list = document.getElementById('notifList');
  if (!list) return;
  if (!NOTIFS.length) {
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-dim);font-size:12px">Sin notificaciones pendientes</div>';
    return;
  }
  list.innerHTML = NOTIFS.map(n => `
    <div class="notif-item">
      <span style="color:#c084fc; font-size:14px">●</span>
      <div style="flex:1">
        <div>${escHtml(n.title)}</div>
        <div class="notif-time">${escHtml(n.time)}</div>
      </div>
    </div>`).join('');
}
function toggleNotifDropdown() {
  const d = document.getElementById('notifDropdown');
  const btn = document.getElementById('notifBtn');
  const open = d.style.display !== 'block';
  d.style.display = open ? 'block' : 'none';
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'none';
}
function clearNotifs() {
  NOTIFS = [];
  renderNotifs();
  const dot = document.getElementById('notifDot');
  if (dot) dot.style.display = 'none';
}
document.addEventListener('click', e => {
  const d = document.getElementById('notifDropdown');
  const btn = document.getElementById('notifBtn');
  if (d && d.style.display === 'block' && !d.contains(e.target) && !btn.contains(e.target)) {
    d.style.display = 'none';
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }
});

const TAB_HASH = { consola: 'console', metricas: 'metrics', sistema: 'metrics', archivos: 'files', mundos: 'worlds', mca: 'worlds', backups: 'backups', tareas: 'tasks', historial: 'history', jugadores: 'history', flota: 'fleet', servidores: 'fleet', configuracion: 'settings', ajustes: 'settings' };
const TAB_SLUG = { console: 'consola', metrics: 'metricas', files: 'archivos', worlds: 'mundos', backups: 'backups', tasks: 'tareas', history: 'historial', fleet: 'flota', settings: 'configuracion' };
function tabFromHash() {
  const h = (location.hash || '').replace(/^#/, '');
  const m = /^[a-z]+/.exec(h);
  const t = m ? (TAB_HASH[m[0]] || null) : null;
  if (t === 'files' && h.includes('/')) {
    try { fsPath = decodeURIComponent(h.split('/').slice(1).join('/')); }
    catch (e) { fsPath = h.split('/').slice(1).join('/'); }
  }
  return t;
}
function syncHash(name) {
  try {
    const want = '#' + (TAB_SLUG[name] || name);
    if ((location.hash || '').replace(/[^a-z]/g, '') !== want.slice(1)) history.replaceState(null, '', want);
  } catch (e) {}
}
window.addEventListener('hashchange', () => { const t = tabFromHash(); if (t) switchTab(t, true); });
setInterval(refreshStats, 2000);
setInterval(refreshConsole, 2500);
refreshStats();
refreshConsole();
(function initTab() {
  try {
    let t = tabFromHash();
    if (!t) {
      try {
        const s = localStorage.getItem('pkTab');
        if (s && KNOWN_TABS.includes(s)) {
          t = s;
          if (s === 'files') {
            const fp = localStorage.getItem('pkFs') || '';
            if (fp) fsPath = fp;
          }
        }
      } catch (e) {}
    }
    if (t) switchTab(t, true);
  } catch (e) {}
})();
window.addEventListener('load', () => {
  try {
    const t = tabFromHash();
    const cur = document.querySelector('.tab-content.active');
    if (t && (!cur || cur.id !== 'tab-' + t)) switchTab(t, true);
  } catch (e) {}
});
</script>
</body>
</html>
"""

MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB


def build_page(srv=None):
    """HTML con los valores del servidor (nombre, subtítulo, puertos, dir)."""
    srv = srv or S()
    p = HTML_PAGE
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
    }
    for old, new in repl.items():
        p = p.replace(old, new)
    if len(SERVERS) > 1:
        opts = "".join(
            f'<option value="{s.id}"{" selected" if s.id == srv.id else ""}>{s.name}</option>'
            for s in SERVERS.values())
        switcher = (
            '<div class="server-switcher" style="padding:10px 20px;border-bottom:1px solid var(--border-color)">'
            '<select id="srvSel" onchange="location.search=\'?server=\'+encodeURIComponent(this.value)" '
            'style="width:100%;background:var(--bg-terminal);border:1px solid var(--border-color);'
            'border-radius:8px;padding:8px 10px;color:#e2e8f0;font-size:13px">'
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
                        mca_files.append({"file": fn, "x": rx, "z": rz, "size": sz})
                    except Exception:
                        pass
        total_size += d_size
        dim_results.append({
            "id": d["id"],
            "label": d["label"],
            "exists": os.path.isdir(rp),
            "size": d_size,
            "regions_count": len(mca_files),
            "regions": sorted(mca_files, key=lambda r: (r["x"], r["z"]))[:200]
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
    for fn in region_files:
        fn = os.path.basename(fn)
        if not re.match(r"^r\.-?\d+\.-?\d+\.mca$", fn):
            continue
        fp = os.path.join(target_dir, fn)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
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
