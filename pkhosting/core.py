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
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
import zipfile
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
    except FileNotFoundError:
        user = {}
    except Exception as e:
        # Fallar cerrado: arrancar con valores por defecto desactivaría la contraseña.
        raise SystemExit(f"[pkhosting] config inválida en {CONFIG_FILE}: {e}")
    if not isinstance(user, dict):
        raise SystemExit(f"[pkhosting] {CONFIG_FILE} debe ser un objeto JSON")
    cfg.update(user)
    try:  # la config guarda el hash, el secreto TOTP y el webhook: solo el dueño
        if os.path.exists(CONFIG_FILE) and os.stat(CONFIG_FILE).st_mode & 0o077:
            os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    for k, typ in (("panel_port", int), ("mc_port", int), ("rcon_port", int), ("max_mem_gb", float)):
        try:
            typ(cfg[k])
        except (TypeError, ValueError):
            raise SystemExit(f"[pkhosting] {CONFIG_FILE}: '{k}' tiene un valor inválido ({cfg[k]!r})")
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
    _write_config(raw)


def _write_config(raw):
    """Escritura atómica con permisos 0600 (contiene hash, TOTP y webhook)."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
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


def totp_enabled():
    return bool(CFG.get("totp_enabled")) and bool(CFG.get("totp_secret"))


def _b32(secret):
    import base64 as _b64
    return _b64.b32decode((secret or "").strip().upper().replace(" ", ""))


def totp_code(secret, counter):
    import base64 as _b64
    try:
        key = _b32(secret)
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        off = digest[-1] & 0x0F
        num = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
        return f"{num % 1000000:06d}"
    except Exception:
        return None


def totp_verify(secret, code, window=1, once=False):
    """once=True rechaza reutilizar un código (o uno anterior) ya aceptado en login."""
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6:
        return False
    now = int(time.time() // 30)
    for delta in range(-window, window + 1):
        ctr = now + delta
        if hmac.compare_digest(totp_code(secret, ctr) or "", code):
            if once:
                with _totp_lock:
                    if ctr <= _totp_last["ctr"]:
                        return False
                    _totp_last["ctr"] = ctr
            return True
    return False


_totp_pending = None
_totp_lock = threading.Lock()
_totp_last = {"ctr": 0}
_pre_tokens = {}


def _pre_issue():
    tok = secrets.token_urlsafe(24)
    with _totp_lock:
        _pre_tokens[tok] = time.time() + 300
    return tok


def _pre_consume(tok):
    with _totp_lock:
        exp = _pre_tokens.pop(tok, 0)
        return bool(exp and exp > time.time())


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


def drop_other_sessions(keep):
    """Cierra todas las sesiones salvo la actual (al cambiar la contraseña)."""
    with _sessions_lock:
        for k in [k for k in _sessions if k != keep]:
            _sessions.pop(k, None)


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # raíz del repo: diagnostics, backup
import diagnostics as dg

CFG = load_config()

PORT = int(CFG.get("panel_port", 8000))
RCON_HOST = "127.0.0.1"


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
        self._init_runtime_state()
        self._apply_file_overrides()

    def _init_runtime_state(self):
        """Estado en memoria; se crea una sola vez (no debe resetearse al guardar config)."""
        self.metrics = collections.deque(maxlen=40)
        self.cpu_prev = None
        self.tps_lock = threading.Lock()
        self.tps_cache = {"ts": 0, "data": {"ok": False}}
        self.tps_sampling = False
        self.backup_lock = threading.Lock()
        self.backup_state = {"running": False, "job": None, "msg": "",
                             "updated": 0, "pct": 0, "stage": ""}
        self.sessions_cache = {"ts": 0, "names": [], "data": {}}

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
    """True si el servidor corre, aunque lo haya lanzado otro proceso (p. ej. tras
    reiniciar el panel): hijo propio vivo o un java con cwd == server_dir."""
    try:
        if s.proc is not None and s.proc.poll() is None:
            return True
    except Exception:
        pass
    try:
        return find_running_mc_pid(s, strict=True) is not None
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
    _write_config(raw)


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
    for s in list(SERVERS.values()):
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


def _ttl_cached(ttl):
    """Cachea por servidor (S().id) el resultado de una consulta cara durante `ttl` segundos."""
    def deco(fn):
        cache, lock = {}, threading.Lock()

        def wrapper(*a, **k):
            key, now = S().id, time.time()
            with lock:
                hit = cache.get(key)
                if hit and now - hit[0] < ttl:
                    return hit[1]
            val = fn(*a, **k)
            with lock:
                cache[key] = (now, val)
            return val
        wrapper.cache_clear = lambda: (lock.acquire(), cache.clear(), lock.release())
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


@_ttl_cached(10)
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


_pid_cache = {}   # id de servidor -> último pid de java encontrado


def find_running_mc_pid(srv=None, strict=False):
    """PID del java del servidor. Reutiliza el pid anterior si sigue vivo y en el mismo
    directorio (evita recorrer /proc en cada sondeo); si no, escanea."""
    s = srv or S()
    root = os.path.realpath(s.server_dir)
    cached = _pid_cache.get(s.id)
    if cached:
        try:
            if (os.path.realpath(f"/proc/{cached}/cwd") == root and
                    os.path.basename(os.readlink(f"/proc/{cached}/exe")).startswith("java")):
                return cached
        except OSError:
            pass
        _pid_cache.pop(s.id, None)
    pid = _scan_mc_pid(s, root, strict)
    if pid is not None:
        _pid_cache[s.id] = pid
    return pid


def _scan_mc_pid(s, root, strict=False):
    """Escaneo de /proc: coincide cwd con server_dir (Vanilla/Forge/Fabric/NeoForge).
    Excluye lanzadores de cliente. strict=True omite la heurística de respaldo."""
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
    if strict:
        return None
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

@_ttl_cached(60)
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

@_ttl_cached(1.5)  # varias pestañas del navegador comparten una sola consulta SLP
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


__all__ = [n for n in dir() if not n.startswith("__")]
