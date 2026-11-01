"""Consola, plantillas estáticas, backups (config) y gestión de mods / Modrinth."""
from pkhosting.ops import *  # noqa: F401,F403

CONSOLE_TAIL_BYTES = 256 * 1024
CONSOLE_MAX_NEW = 2000


def _console_file(srv):
    for fpath in (srv.child_log, srv.log_file):
        try:
            if os.path.getsize(fpath) > 0:
                return fpath
        except OSError:
            continue
    return None


def read_console_lines(limit=250):
    """Últimas líneas de la consola (solo se lee el final del archivo)."""
    return read_console_since(None, None, limit)["lines"]


def read_console_since(since, src, limit=250):
    """Lectura incremental: devuelve las líneas nuevas desde el offset `since` (bytes).
    Si no hay offset, el archivo cambió/rotó o el offset es inválido → reset con las últimas `limit`."""
    fpath = _console_file(S())
    if not fpath:
        return {"lines": [], "offset": 0, "src": "", "reset": True}
    name = os.path.basename(fpath)
    try:
        size = os.path.getsize(fpath)
        reset = since is None or src != name or since < 0 or since > size
        start = max(0, size - CONSOLE_TAIL_BYTES) if reset else since
        with open(fpath, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
    except OSError:
        return {"lines": [], "offset": 0, "src": name, "reset": True}
    if reset:
        if start > 0:  # empezamos a mitad del archivo: descartar la línea cortada
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        lines = data.decode("utf-8", "replace").splitlines()[-limit:]
        return {"lines": lines, "offset": size, "src": name, "reset": True}
    end = data.rfind(b"\n")  # solo líneas completas; el resto se lee en el siguiente sondeo
    if end < 0:
        return {"lines": [], "offset": since, "src": name, "reset": False}
    lines = data[:end].decode("utf-8", "replace").splitlines()[-CONSOLE_MAX_NEW:]
    return {"lines": lines, "offset": since + end + 1, "src": name, "reset": False}

# ══════════════════════════════════════════════════════════════════
# PLANTILLA HTML/CSS/JS (tema Liquid Glass)
# ══════════════════════════════════════════════════════════════════



MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB


STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
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
    esc = html.escape  # nombre/subtítulo/ruta/id vienen de config y van dentro de HTML
    name, subtitle, sdir, sid = esc(srv.name), esc(srv.subtitle), esc(srv.server_dir), esc(srv.id)
    repl = {
        "__NAME__": name,
        "__SUBTITLE__": subtitle,
        "__MCPORT__": str(srv.mc_port),
        "__IP__": local_ip,
        "__DIR__": sdir,
        "__CORES__": str(os.cpu_count() or "?"),
        "__SRV__": sid,
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
            f'<option value="{html.escape(s.id)}"{" selected" if s.id == srv.id else ""}>{html.escape(s.name)}</option>'
            for s in list(SERVERS.values()))
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


def _updates_state(srv):
    st = getattr(srv, "updates_state", None)
    if st is None:
        st = srv.updates_state = {"running": False, "updated": 0}
    return st


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
                            "size": os.path.getsize(os.path.join(mods, name)),
                            "prev": os.path.isfile(os.path.join(mods, name[:-9] + ".prev"))})
            elif name.endswith(".jar"):
                out.append({"name": name, "enabled": True,
                            "size": os.path.getsize(os.path.join(mods, name)),
                            "prev": os.path.isfile(os.path.join(mods, name + ".prev"))})
    except FileNotFoundError:
        pass
    return out


_MOD_TOML_MODID = re.compile(r'^\s*modId\s*=\s*"([^"]+)"', re.M)
_MOD_TOML_VER = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.M)
_MOD_TOML_NAME = re.compile(r'^\s*displayName\s*=\s*"([^"]+)"', re.M)


def _mod_toml_meta(path):
    """Lee modId/version/displayName de META-INF/neoforge.mods.toml sin dependencias."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            toml_name = next((n for n in names if n.endswith("neoforge.mods.toml")), None)
            if not toml_name:
                return []
            text = z.read(toml_name).decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for blk in re.split(r"(?m)^\[\[mods\]\]", text)[1:]:
        mid = _MOD_TOML_MODID.search(blk)
        if not mid:
            continue
        ver = _MOD_TOML_VER.search(blk)
        nm = _MOD_TOML_NAME.search(blk)
        out.append({"id": mid.group(1),
                    "version": (ver.group(1) if ver else ""),
                    "title": (nm.group(1) if nm else "")})
    return out


def mod_updates_cache_file(srv):
    return os.path.join(srv.data_dir, "mod-updates.json")


def _updates_load(srv):
    try:
        with open(mod_updates_cache_file(srv)) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _updates_save(srv, d):
    try:
        os.makedirs(srv.data_dir, exist_ok=True)
        tmp = mod_updates_cache_file(srv) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, mod_updates_cache_file(srv))
    except Exception:
        pass


def _mr_get(path, timeout=10):
    req = urllib.request.Request("https://api.modrinth.com" + path,
                                 headers={"User-Agent": "PKHosting-Panel/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _same_version(cur, latest, cur_file, latest_file):
    cur, latest = (cur or "").strip(), (latest or "").strip()
    if not cur or not latest:
        return False
    if latest == cur or latest.startswith(cur) or cur.startswith(latest):
        return True
    return bool(cur_file and latest_file and cur_file == latest_file)


def mod_update_check(srv, mod_id, current, loader="neoforge", game_ver="1.21.1", cur_file=None):
    """Devuelve {latest, date, url, filename} o {error}. Solo stdlib."""
    try:
        proj = _mr_get(f"/v2/project/{urllib.parse.quote(mod_id)}")
    except Exception:
        return {"error": "no está en Modrinth"}
    slug = proj.get("slug") or mod_id
    try:
        vers = _mr_get(f"/v2/project/{urllib.parse.quote(slug)}/version"
                       f"?loaders=%5B%22{loader}%22%5D&game_versions=%5B%22{game_ver}%22%5D")
    except Exception as e:
        return {"error": str(e)[:120]}
    if not vers:
        return {"error": "sin versiones compatibles"}
    latest = vers[0]
    jar = next((f for f in latest.get("files", []) if f.get("filename", "").endswith(".jar")), None)
    if not jar:
        return {"error": "sin .jar en la última versión"}
    lv = latest.get("version_number", "")
    return {"latest": lv, "date": (latest.get("date_published", "") or "")[:10],
            "url": jar.get("url"), "filename": jar.get("filename"),
            "same": _same_version(current, lv, cur_file, jar.get("filename"))}


_updates_lock = threading.Lock()


def refresh_mod_updates(srv):
    """Recorre los mods con modId conocido (caché 6 h). Pensado para hilo de fondo."""
    try:
        _refresh_mod_updates(srv)
    finally:  # un error no debe dejar "running" atascado en True
        with _updates_lock:
            _updates_state(srv).update(running=False, updated=time.time())
        _ctx.srv = None


def _refresh_mod_updates(srv):
    _ctx.srv = srv
    mods_dir = os.path.join(srv.server_dir, "mods")
    cache = _updates_load(srv)
    now = time.time()
    try:
        entries = list_mods()
    except Exception:
        entries = []
    for m in entries:
        if not m.get("enabled"):
            continue
        try:
            meta = _mod_toml_meta(os.path.join(mods_dir, m["name"]))
        except Exception:
            continue
        for mm in meta:
            mid = mm["id"]
            hit = cache.get(mid)
            if hit and hit.get("algo") == 2 and now - hit.get("checked", 0) < 6 * 3600:
                continue
            try:
                res = mod_update_check(srv, mid, mm.get("version"), cur_file=m["name"])
            except Exception as e:
                res = {"error": str(e)[:120]}
            res.update(current=mm.get("version", ""), title=mm.get("title", ""),
                       file=m["name"], checked=now, algo=2)
            cache[mid] = res
            time.sleep(0.2)
    _updates_save(srv, cache)


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
            # Sin respaldo "cualquier versión": instalaría un jar de otro loader/versión de MC.
            return False, f"No hay versiones de este mod para {loader} {game_ver}"

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
        
        dl_url = target_file.get("url") or ""
        dl_fn = os.path.basename(str(target_file.get("filename") or ""))  # nombre remoto: nunca como ruta
        if not dl_fn.endswith(".jar") or dl_fn.startswith("."):
            return False, "Nombre de archivo inválido en Modrinth"
        if not dl_url.startswith("https://cdn.modrinth.com/"):
            return False, "URL de descarga no permitida"
        dest_path = os.path.join(mods_dir, dl_fn)
        tmp_path = dest_path + ".part"
        req_dl = urllib.request.Request(dl_url, headers={"User-Agent": "PKHosting-Panel/2.0"})
        try:
            with urllib.request.urlopen(req_dl, timeout=30) as resp, open(tmp_path, "wb") as out_f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
            os.replace(tmp_path, dest_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return True, f"Mod '{dl_fn}' instalado correctamente en mods/ (requiere reinicio)"
    except Exception as e:
        return False, f"Error descargando mod: {e}"


__all__ = [n for n in dir() if not n.startswith("__")]
