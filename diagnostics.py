#!/usr/bin/env python3
"""
PKHosting — diagnóstico del servidor (solo stdlib).

- Crash-reports: listado, análisis de causa y mod sospechoso.
- Detección de bucle de crash (ventana deslizante).
- user_jvm_args.txt: lectura/validación/escritura con presets.
- server.properties: esquema tipado, lectura/escritura preservando comentarios.
- Logs: lectura filtrada por nivel y texto.
"""
import collections
import datetime
import gzip
import io
import os
import re
import shutil
import time

# ─────────────────────────── Crash-reports ───────────────────────────
CRASH_NAME_RE = re.compile(r"^crash-(\d{4}-\d{2}-\d{2})_(\d{2})\.(\d{2})\.(\d{2})-(fml|server|client)\.txt$")
_TRANSFORMER_RE = re.compile(r"TRANSFORMER/([a-z0-9_\-\.]+)@")
_IGNORED_MODS = {"minecraft", "neoforge", "forge", "fml", "java", "mixinextras", "mixin"}

HINTS = [
    (re.compile(r"ConcurrentModificationException"),
     "Carrera de hilos al construir mods en paralelo (típico de Create). "
     "Añade -Djava.util.concurrent.ForkJoinPool.common.parallelism=1 en los JVM args."),
    (re.compile(r"InvalidInjectionException.*non-static callback method.*static method"),
     "Un mixin declara su handler como método de instancia pero el objetivo es static: "
     "el handler debe ser `static` en el mod que lo define."),
    (re.compile(r"InvalidInjectionException|MixinApplyError|MixinTransformerError"),
     "Falló la aplicación de un mixin: el mod indicado es incompatible con otro mod o con "
     "esta versión del objetivo. Actualízalo o desactívalo."),
    (re.compile(r"VerifyError"),
     "Bytecode inválido tras aplicar un mixin/transformador: casi siempre un mod mal compilado "
     "para esta versión."),
    (re.compile(r"OutOfMemoryError: Java heap space"),
     "Sin memoria de heap: sube -Xmx (con margen sobre la RAM libre) o reduce mods/distancia de simulación."),
    (re.compile(r"OutOfMemoryError: Metaspace"),
     "Metaspace agotado: sube -XX:MaxMetaspaceSize (p. ej. 768M o 1G con muchos mods)."),
    (re.compile(r"OutOfMemoryError"),
     "Memoria insuficiente del proceso Java."),
    (re.compile(r"ServerHangWatchdog"),
     "El tick del servidor se quedó bloqueado y el watchdog lo mató. Revisa los mods que "
     "aparecen en la pila (chunk loading, colisiones, redstone masiva) o sube max-tick-time."),
    (re.compile(r"Could not load super class .*client|net/minecraft/client"),
     "Un mod solo-cliente está en la carpeta mods/ del servidor. Quítalo."),
    (re.compile(r"Missing or unsupported mandatory dependencies|requires .* (version|or above)", re.I),
     "Falta una dependencia obligatoria o su versión no coincide. Mira el detalle del mod."),
    (re.compile(r"UnsupportedClassVersionError"),
     "El mod/Java usa una versión de bytecode distinta: revisa que Java sea 21 para 1.21.1."),
    (re.compile(r"Address already in use|BindException"),
     "El puerto del servidor ya está en uso: otra instancia sigue viva o el puerto está ocupado."),
]


def crash_dir(server_dir):
    return os.path.join(server_dir, "crash-reports")


def _read_text(path, limit=400_000):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def _crash_time(name, path):
    m = CRASH_NAME_RE.match(name)
    if m:
        try:
            d = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S")
            return d.timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _parse_mod_block(mid, body):
    lines = body.split("\n")
    msg, detail, ver, fil = "", [], "", ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("\tFailure message:"):
            msg = ln.split(":", 1)[1].strip()
            i += 1
            while i < len(lines) and lines[i].startswith("\t\t"):
                if lines[i].strip():
                    detail.append(lines[i].strip())
                i += 1
            continue
        if ln.startswith("\tMod version:"):
            ver = ln.split(":", 1)[1].strip()
        elif ln.startswith("\tMod file:"):
            fil = os.path.basename(ln.split(":", 1)[1].strip())
        i += 1
    det = " ".join(detail[:2])[:240]
    culprit = ""
    m = re.search(r"from [^()]*\(([a-z0-9_\-]+)\) has failed", msg)
    if m:
        culprit = m.group(1)
    if msg.startswith("Mod ") and " requires " in msg:
        det = msg[4:]
        if detail:
            det += " — " + detail[0]
    if not mid:
        m2 = re.search(r"\(([a-z0-9_\-]+)\) has failed|^Mod ([a-z0-9_\-]+) requires", msg)
        if m2:
            mid = m2.group(1) or m2.group(2)
    return {"id": mid, "message": msg, "detail": det, "version": ver, "file": fil,
            "culprit": culprit if culprit and culprit != mid else ""}


def analyze_crash(text):
    """Extrae causa, mods implicados y pistas legibles de un crash-report."""
    out = {"description": "", "exception": "", "mods": [], "involved": [], "hints": [], "kind": "otro"}
    m = re.search(r"^Description:\s*(.+)$", text, re.M)
    if m:
        out["description"] = m.group(1).strip()
    if "Mod loading failures" in out["description"] or "Mod loading has failed" in text[:3000]:
        out["kind"] = "arranque"
    elif "Watching Server" in out["description"]:
        out["kind"] = "watchdog"
    elif out["description"]:
        out["kind"] = "ejecución"

    # Bloques de fallo de carga de mods
    for blk in re.finditer(r"-- Mod loading issue(?: for: ([^\s-]+))? --\s*\nDetails:\s*\n(.*?)(?=\n--|\Z)", text, re.S):
        mid, body = blk.group(1) or "", blk.group(2)
        out["mods"].append(_parse_mod_block(mid, body))
    if not out["mods"]:
        m = re.search(r"Failure message:\s*(.+)", text)
        if m:
            out["mods"].append({"id": "", "message": m.group(1).strip(), "detail": "",
                                "version": "", "file": "", "culprit": ""})

    # Excepción principal (se salta el envoltorio genérico de FML)
    for m in re.finditer(r"^((?:[\w$]+\.)+[\w$]*(?:Error|Exception)\b[^\n]*)$", text, re.M):
        line = m.group(1).strip()
        if "ModLoadingCrashException" in line:
            continue
        out["exception"] = line[:300]
        break

    # Mods implicados en la pila (conteo de frames por mod)
    cnt = collections.Counter()
    for mm in _TRANSFORMER_RE.finditer(text):
        mid = mm.group(1)
        if mid not in _IGNORED_MODS:
            cnt[mid] += 1
    out["involved"] = [{"id": k, "frames": v} for k, v in cnt.most_common(6)]

    hay = text[:60000]
    seen = set()
    for rx, hint in HINTS:
        if rx.search(hay) and hint not in seen:
            seen.add(hint)
            out["hints"].append(hint)
    return out


def _crash_summary(a):
    if a["mods"]:
        m = a["mods"][0]
        who = m["culprit"] or m["id"] or "?"
        det = m["detail"] or m["message"]
        txt = f"{who}: {det}"
    elif a["exception"]:
        txt = a["exception"]
    else:
        txt = a["description"]
    return re.sub(r"\s+", " ", txt)[:200]


def _suspect(a):
    if a["mods"]:
        return a["mods"][0]["culprit"] or a["mods"][0]["id"]
    return a["involved"][0]["id"] if a["involved"] else ""


def crash_list(server_dir, limit=60):
    d = crash_dir(server_dir)
    try:
        names = [n for n in os.listdir(d) if CRASH_NAME_RE.match(n)]
    except OSError:
        return []
    names.sort(reverse=True)
    out = []
    for n in names[:limit]:
        p = os.path.join(d, n)
        try:
            a = analyze_crash(_read_text(p, 120_000))
            out.append({"name": n, "time": _crash_time(n, p), "size": os.path.getsize(p),
                        "kind": a["kind"], "summary": _crash_summary(a),
                        "suspect": _suspect(a)})
        except OSError:
            continue
    return out


def crash_detail(server_dir, name):
    if not CRASH_NAME_RE.match(name or ""):
        return None
    p = os.path.join(crash_dir(server_dir), name)
    if not os.path.isfile(p):
        return None
    text = _read_text(p)
    a = analyze_crash(text)
    a.update({"name": name, "time": _crash_time(name, p), "summary": _crash_summary(a),
              "suspect": _suspect(a),
              "text": text[:200_000]})
    return a


def crash_delete(server_dir, name):
    if not CRASH_NAME_RE.match(name or ""):
        return False
    p = os.path.join(crash_dir(server_dir), name)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def newest_crash(server_dir, since=0.0):
    """Nombre del crash-report más reciente posterior a `since` (timestamp) o None."""
    d = crash_dir(server_dir)
    try:
        best = None
        for n in os.listdir(d):
            if not CRASH_NAME_RE.match(n):
                continue
            t = _crash_time(n, os.path.join(d, n))
            if t > since and (best is None or t > best[0]):
                best = (t, n)
        return best[1] if best else None
    except OSError:
        return None


def clean_stop_in_log(log_path, since_bytes=65536):
    """True si el final de latest.log muestra un apagado ordenado."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - since_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return bool(re.search(r"Stopping (the )?server|Closing Server", tail))


class CrashLoopGuard:
    """Cuenta caídas inesperadas en una ventana y decide si se puede reintentar."""

    def __init__(self, max_crashes=3, window_s=600):
        self.max_crashes = max_crashes
        self.window_s = window_s
        self.times = collections.deque(maxlen=50)

    def configure(self, max_crashes, window_s):
        self.max_crashes = max(1, int(max_crashes))
        self.window_s = max(60, int(window_s))

    def record(self, now=None):
        self.times.append(now if now is not None else time.time())

    def recent(self, now=None):
        now = now if now is not None else time.time()
        return [t for t in self.times if now - t <= self.window_s]

    def looping(self, now=None):
        return len(self.recent(now)) >= self.max_crashes

    def reset(self):
        self.times.clear()


# ─────────────────────────── JVM args ───────────────────────────
JVM_PRESETS = {
    "aikar": {
        "name": "Aikar (G1GC, recomendado)",
        "flags": [
            "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
            "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC",
            "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
            "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
            "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
            "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem",
            "-XX:MaxTenuringThreshold=1", "-Dusing.aikars.flags=https://mcflags.emc.gs",
            "-Daikar.new.flags=true"],
    },
    "zgc": {
        "name": "ZGC generacional (Java 21, baja latencia)",
        "flags": ["-XX:+UseZGC", "-XX:+ZGenerational", "-XX:+DisableExplicitGC",
                  "-XX:+AlwaysPreTouch", "-XX:+PerfDisableSharedMem"],
    },
    "light": {
        "name": "Ligero (pocos mods / poca RAM)",
        "flags": ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
                  "-XX:+DisableExplicitGC", "-XX:+PerfDisableSharedMem"],
    },
}
SERIAL_LOAD_FLAG = "-Djava.util.concurrent.ForkJoinPool.common.parallelism=1"
_MEM_RE = re.compile(r"^-(Xm[sx])(\d+(?:\.\d+)?)([KkMmGg]?)$")


def mem_to_mb(num, unit):
    n = float(num)
    u = (unit or "").upper()
    if u == "G":
        return n * 1024
    if u == "K":
        return n / 1024
    if u == "M" or u == "":
        return n if u == "M" else n / (1024 * 1024)
    return n


def jvm_args_path(server_dir):
    return os.path.join(server_dir, "user_jvm_args.txt")


def system_ram_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def parse_jvm_args(text):
    info = {"xms_mb": None, "xmx_mb": None, "serial_load": False, "flags": 0}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        info["flags"] += 1
        m = _MEM_RE.match(s)
        if m:
            mb = mem_to_mb(m.group(2), m.group(3))
            info["xms_mb" if m.group(1) == "Xms" else "xmx_mb"] = int(mb)
        if s.startswith("-Djava.util.concurrent.ForkJoinPool.common.parallelism="):
            try:
                info["serial_load"] = int(s.split("=")[1]) == 1
            except ValueError:
                pass
    return info


def jvm_read(server_dir):
    p = jvm_args_path(server_dir)
    try:
        text = _read_text(p, 100_000)
    except OSError:
        text = ""
    return {"text": text, "exists": os.path.isfile(p), "info": parse_jvm_args(text),
            "system_ram_mb": system_ram_mb(),
            "presets": {k: {"name": v["name"], "flags": v["flags"]} for k, v in JVM_PRESETS.items()},
            "serial_flag": SERIAL_LOAD_FLAG}


def jvm_validate(text):
    """Devuelve (ok, errores, avisos)."""
    errs, warns = [], []
    if len(text) > 100_000:
        return False, ["Archivo demasiado grande"], warns
    xms = xmx = None
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not s.startswith("-") and not s.startswith("@"):
            errs.append(f"Línea {i}: debe empezar por '-' ({s[:40]})")
            continue
        if re.search(r"[;&|`$<>]", s):
            errs.append(f"Línea {i}: caracteres no permitidos ({s[:40]})")
        m = _MEM_RE.match(s)
        if s.startswith("-Xm") and not m:
            errs.append(f"Línea {i}: memoria inválida ({s})")
        if m:
            mb = mem_to_mb(m.group(2), m.group(3))
            if m.group(1) == "Xms":
                xms = mb
            else:
                xmx = mb
    if xmx is None:
        warns.append("No hay -Xmx: Java usará el valor por defecto (1/4 de la RAM).")
    if xms and xmx and xms > xmx:
        errs.append(f"-Xms ({int(xms)} MB) no puede ser mayor que -Xmx ({int(xmx)} MB)")
    ram = system_ram_mb()
    if xmx and ram and xmx > ram * 0.9:
        warns.append(f"-Xmx ({int(xmx)} MB) deja menos de un 10 % de la RAM del sistema ({ram} MB).")
    if re.search(r"UseG1GC", text) and re.search(r"UseZGC", text):
        errs.append("No se pueden combinar G1GC y ZGC")
    return (not errs), errs, warns


def jvm_write(server_dir, text):
    ok, errs, warns = jvm_validate(text)
    if not ok:
        return False, "; ".join(errs), warns
    p = jvm_args_path(server_dir)
    if os.path.isfile(p):
        shutil.copy2(p, p + ".bak")
    if text and not text.endswith("\n"):
        text += "\n"
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
    return True, "Guardado (requiere reiniciar el servidor)", warns


# ─────────────────────────── server.properties ───────────────────────────
def _b(desc, default="false", restart=False):
    return {"type": "bool", "desc": desc, "default": default, "restart": restart}


def _i(desc, lo, hi, default, restart=False):
    return {"type": "int", "desc": desc, "min": lo, "max": hi, "default": default, "restart": restart}


def _e(desc, options, default, restart=False):
    return {"type": "enum", "desc": desc, "options": options, "default": default, "restart": restart}


def _s(desc, default="", restart=False, secret=False):
    return {"type": "str", "desc": desc, "default": default, "restart": restart, "secret": secret}


PROPS_GROUPS = [
    ("Jugabilidad", ["gamemode", "difficulty", "hardcore", "pvp", "allow-flight", "force-gamemode",
                     "spawn-protection", "player-idle-timeout", "allow-nether", "spawn-monsters",
                     "spawn-animals", "spawn-npcs", "generate-structures", "enable-command-block",
                     "function-permission-level", "op-permission-level"]),
    ("Mundo", ["level-name", "level-seed", "level-type", "max-world-size", "view-distance",
               "simulation-distance", "max-build-height", "initial-enabled-packs"]),
    ("Red y acceso", ["server-port", "server-ip", "max-players", "online-mode", "white-list",
                      "enforce-whitelist", "motd", "network-compression-threshold",
                      "prevent-proxy-connections", "enforce-secure-profile", "hide-online-players",
                      "enable-status", "rate-limit"]),
    ("Rendimiento", ["max-tick-time", "entity-broadcast-range-percentage", "sync-chunk-writes",
                     "use-native-transport", "pause-when-empty-seconds"]),
    ("RCON y query", ["enable-rcon", "rcon.port", "rcon.password", "enable-query", "query.port",
                      "broadcast-rcon-to-ops", "broadcast-console-to-ops"]),
]

PROPS_SCHEMA = {
    "gamemode": _e("Modo de juego por defecto", ["survival", "creative", "adventure", "spectator"], "survival"),
    "difficulty": _e("Dificultad", ["peaceful", "easy", "normal", "hard"], "easy"),
    "hardcore": _b("Hardcore: baneo al morir", "false", True),
    "pvp": _b("Permitir PvP", "true"),
    "allow-flight": _b("Permitir volar (necesario con algunos mods)", "false"),
    "force-gamemode": _b("Forzar el modo de juego al entrar", "false"),
    "spawn-protection": _i("Radio de protección del spawn (0 = sin protección)", 0, 1000, 16),
    "player-idle-timeout": _i("Minutos de inactividad antes de expulsar (0 = nunca)", 0, 10000, 0),
    "allow-nether": _b("Permitir el Nether", "true"),
    "spawn-monsters": _b("Generar monstruos", "true"),
    "spawn-animals": _b("Generar animales", "true"),
    "spawn-npcs": _b("Generar aldeanos", "true"),
    "generate-structures": _b("Generar estructuras", "true", True),
    "enable-command-block": _b("Habilitar bloques de comandos", "false"),
    "function-permission-level": _i("Nivel de permisos de las funciones", 1, 4, 2),
    "op-permission-level": _i("Nivel de permisos de los operadores", 0, 4, 4),
    "level-name": _s("Nombre de la carpeta del mundo", "world", True),
    "level-seed": _s("Semilla del mundo", "", True),
    "level-type": _s("Tipo de mundo (minecraft:normal, minecraft:flat...)", "minecraft:normal", True),
    "max-world-size": _i("Radio máximo del mundo en bloques", 1, 29999984, 29999984),
    "view-distance": _i("Distancia de renderizado (chunks)", 3, 32, 10),
    "simulation-distance": _i("Distancia de simulación (chunks)", 3, 32, 10),
    "max-build-height": _i("Altura máxima de construcción", 64, 2032, 256),
    "initial-enabled-packs": _s("Datapacks activados al crear el mundo", "vanilla", True),
    "server-port": _i("Puerto del servidor", 1, 65535, 25565, True),
    "server-ip": _s("IP de enlace (vacío = todas)", "", True),
    "max-players": _i("Máximo de jugadores", 1, 100000, 20),
    "online-mode": _b("Verificar cuentas con Mojang (apágalo solo con proxy)", "true", True),
    "white-list": _b("Activar whitelist", "false"),
    "enforce-whitelist": _b("Expulsar a quien no esté en la whitelist al activarla", "false"),
    "motd": _s("Mensaje del servidor en la lista", "A Minecraft Server"),
    "network-compression-threshold": _i("Umbral de compresión de red (-1 = sin compresión)", -1, 65535, 256, True),
    "prevent-proxy-connections": _b("Bloquear conexiones por proxy/VPN", "false"),
    "enforce-secure-profile": _b("Exigir chat firmado", "true", True),
    "hide-online-players": _b("Ocultar la lista de jugadores en el estado", "false"),
    "enable-status": _b("Responder a los pings de la lista de servidores", "true"),
    "rate-limit": _i("Paquetes por segundo máximos por jugador (0 = sin límite)", 0, 100000, 0),
    "max-tick-time": _i("ms máximos por tick antes de matar el servidor (-1 = desactivado)", -1, 2147483647, 60000),
    "entity-broadcast-range-percentage": _i("Rango de envío de entidades (%)", 10, 1000, 100),
    "sync-chunk-writes": _b("Escritura síncrona de chunks (más seguro, más lento)", "true", True),
    "use-native-transport": _b("Optimización de red nativa de Linux", "true", True),
    "pause-when-empty-seconds": _i("Segundos sin jugadores antes de pausar el servidor (-1 = nunca)", -1, 2147483647, 60),
    "enable-rcon": _b("Activar RCON (el panel lo necesita)", "false", True),
    "rcon.port": _i("Puerto RCON", 1, 65535, 25575, True),
    "rcon.password": _s("Contraseña RCON", "", True, True),
    "enable-query": _b("Activar protocolo query", "false", True),
    "query.port": _i("Puerto query", 1, 65535, 25565, True),
    "broadcast-rcon-to-ops": _b("Mostrar a los OP los comandos RCON", "true"),
    "broadcast-console-to-ops": _b("Mostrar a los OP los comandos de consola", "true"),
}
_PROP_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*=(.*)$")


def props_path(server_dir):
    return os.path.join(server_dir, "server.properties")


def _props_parse(text):
    vals = {}
    for line in text.splitlines():
        m = _PROP_LINE.match(line.strip())
        if m and not line.lstrip().startswith("#"):
            vals[m.group(1)] = m.group(2).strip()
    return vals


def props_read(server_dir):
    try:
        text = _read_text(props_path(server_dir), 200_000)
    except OSError:
        text = ""
    vals = _props_parse(text)
    groups = []
    used = set()
    for gname, keys in PROPS_GROUPS:
        items = []
        for k in keys:
            sch = PROPS_SCHEMA.get(k)
            if not sch:
                continue
            used.add(k)
            v = vals.get(k)
            item = dict(sch)
            item["key"] = k
            item["present"] = v is not None
            item["value"] = "" if sch.get("secret") else (v if v is not None else sch["default"])
            item["has_secret"] = bool(sch.get("secret") and v)
            items.append(item)
        groups.append({"name": gname, "items": items})
    extra = []
    for k, v in vals.items():
        if k in used or k in PROPS_SCHEMA:
            continue
        extra.append({"key": k, "type": "str", "desc": "", "default": "", "restart": False,
                      "value": v, "present": True, "secret": "password" in k})
        if "password" in k:
            extra[-1]["value"] = ""
    if extra:
        groups.append({"name": "Otras", "items": extra})
    return {"groups": groups}


def props_validate(key, value):
    sch = PROPS_SCHEMA.get(key)
    v = str(value).strip()
    if "\n" in v or "\r" in v:
        return None, "Valor no válido"
    if not sch:
        return v, None
    t = sch["type"]
    if t == "bool":
        if v.lower() not in ("true", "false"):
            return None, f"{key}: debe ser true/false"
        return v.lower(), None
    if t == "int":
        try:
            n = int(v)
        except ValueError:
            return None, f"{key}: debe ser un número entero"
        if n < sch["min"] or n > sch["max"]:
            return None, f"{key}: fuera de rango ({sch['min']}–{sch['max']})"
        return str(n), None
    if t == "enum":
        if v.lower() not in sch["options"]:
            return None, f"{key}: opciones válidas {', '.join(sch['options'])}"
        return v.lower(), None
    return v, None


def props_write(server_dir, changes):
    """Aplica {clave: valor} preservando comentarios, orden y claves desconocidas."""
    p = props_path(server_dir)
    try:
        text = _read_text(p, 400_000)
    except OSError:
        text = ""
    clean, errs = {}, []
    for k, v in (changes or {}).items():
        if not _PROP_LINE.match(f"{k}=x"):
            errs.append(f"Clave inválida: {k}")
            continue
        sch = PROPS_SCHEMA.get(k)
        if sch and sch.get("secret") and str(v).strip() == "":
            continue
        nv, e = props_validate(k, v)
        if e:
            errs.append(e)
        else:
            clean[k] = nv
    if errs:
        return False, "; ".join(errs), []
    if not clean:
        return True, "Sin cambios", []
    old = _props_parse(text)
    changed = [k for k, v in clean.items() if old.get(k) != v]
    if not changed:
        return True, "Sin cambios", []
    lines = text.splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = _PROP_LINE.match(line.strip())
        if m and not line.lstrip().startswith("#") and m.group(1) in clean:
            k = m.group(1)
            lines[i] = f"{k}={clean[k]}"
            done.add(k)
    for k in clean:
        if k not in done and k in changed:
            lines.append(f"{k}={clean[k]}")
    if os.path.isfile(p):
        shutil.copy2(p, p + ".bak")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, p)
    needs = [k for k in changed if PROPS_SCHEMA.get(k, {}).get("restart", False)]
    return True, f"{len(changed)} propiedad(es) guardada(s)", needs


# ─────────────────────────── Logs ───────────────────────────
_LOG_LEVEL_RE = re.compile(r"^\[[^\]]*\]\s*\[[^\]/]*/(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\]")
LEVEL_RANK = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}


def logs_dir(server_dir):
    return os.path.join(server_dir, "logs")


def logs_list(server_dir):
    d = logs_dir(server_dir)
    out = []
    try:
        for n in os.listdir(d):
            if not (n.endswith(".log") or n.endswith(".log.gz")):
                continue
            if n == "panel-child.log":
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    out.sort(key=lambda x: (x["name"] != "latest.log", -x["mtime"]))
    return out


def _tail_text(path, max_bytes):
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            data = f.read()
        return data[-max_bytes:].decode("utf-8", "replace")
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", "replace")


def logs_read(server_dir, name="latest.log", level="ALL", query="", limit=600,
              max_bytes=3_000_000, regex=False):
    allowed = {x["name"] for x in logs_list(server_dir)}
    if name not in allowed:
        return None
    text = _tail_text(os.path.join(logs_dir(server_dir), name), max_bytes)
    entries = []
    cur = None
    for line in text.splitlines():
        m = _LOG_LEVEL_RE.match(line)
        if m:
            cur = {"level": m.group(1), "lines": [line]}
            entries.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
        else:
            cur = {"level": "INFO", "lines": [line]}
            entries.append(cur)
    min_rank = LEVEL_RANK.get((level or "ALL").upper(), -1)
    rx = None
    q = (query or "").strip()
    if q and regex:
        try:
            rx = re.compile(q, re.I)
        except re.error:
            rx = None
    out = []
    for e in entries:
        if min_rank >= 0 and LEVEL_RANK.get(e["level"], 2) < min_rank:
            continue
        body = "\n".join(e["lines"])
        if q:
            if rx is not None:
                if not rx.search(body):
                    continue
            elif q.lower() not in body.lower():
                continue
        out.append({"level": e["level"], "text": body})
    total = len(out)
    counts = collections.Counter(o["level"] for o in out)
    return {"name": name, "entries": out[-limit:], "total": total, "shown": min(total, limit),
            "counts": dict(counts), "truncated": total > limit}
