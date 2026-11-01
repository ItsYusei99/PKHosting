#!/usr/bin/env python3
"""
PKHosting — panel web instalable para servidores Minecraft Java (punto de entrada).

Código repartido en el paquete `pkhosting/`:
  core.py     configuración, autenticación, clase Server, RCON, detección de procesos
  ops.py      métricas, jugadores, arranque/parada, backups, Discord, tareas programadas
  content.py  consola, plantillas estáticas, mods y Modrinth
  world.py    NBT/MCA, mapa de terreno, poda y restauración de chunks
  tools.py    fichas de jugador, ping, Spark, flota, subida de archivos
Aquí quedan la página de login, el servidor HTTP y el arranque.

Configuración en ~/.config/pkhosting/config.json (ver config.example.json).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkhosting.tools import *  # noqa: F401,F403,E402
from pkhosting import core, ops, content, world, tools  # noqa: F401,E402  (accesibles como panel.core, etc.)

# ══════════════════════════════════════════════════════════════════
# SERVIDOR HTTP API
# ══════════════════════════════════════════════════════════════════

LOGIN_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PKHosting — Acceso</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<style>
@font-face{font-family:"Pixelify Sans";src:url("/static/pixelify.ttf") format("truetype");font-weight:400 700;font-display:swap}
:root{color-scheme:dark;--bg:#0e0c12;--sunken:#09080c;--surface:#16131c;--surface-2:#1d1925;--line:#2a2434;--line-strong:#3b3448;
--text:#ece8f4;--text-2:#b9b0ca;--text-3:#8f86a4;--ac:#a855f7;--ac2:#7c3aed;--ac-text:#c084fc;--bad:#f05252;--hi:rgba(255,255,255,.08);--lo:rgba(0,0,0,.45)}
html[data-theme=light]{color-scheme:light;--bg:#f3f0e8;--sunken:#e9e4d8;--surface:#fffdf8;--surface-2:#f6f2e9;--line:#ddd5c5;--line-strong:#c9bfac;
--text:#1c1824;--text-2:#4a4256;--text-3:#665d73;--ac-text:#7c3aed;--bad:#c81e1e;--hi:rgba(255,255,255,.9);--lo:rgba(60,45,20,.18)}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
background-size:32px 32px;background-position:-1px -1px}
body::before{content:"";position:fixed;inset:0;background:radial-gradient(60% 50% at 50% 40%,transparent,var(--bg) 75%);pointer-events:none}
.card{position:relative;width:min(380px,100%);padding:30px 28px 26px;background:var(--surface);border:2px solid #120a1c;
box-shadow:inset 0 0 0 2px rgba(168,85,247,.55),0 24px 60px rgba(0,0,0,.45);border-radius:6px}
html[data-theme=light] .card{border-color:#d8cfe8;box-shadow:inset 0 0 0 2px rgba(124,58,237,.35),0 24px 60px rgba(60,45,20,.18)}
.login-logo{width:56px;height:56px;margin:0 auto 14px}
.login-logo img{width:100%;height:100%;display:block;image-rendering:pixelated}
h1{text-align:center;font:600 28px/1.1 "Pixelify Sans",ui-monospace,monospace;letter-spacing:.01em}
h1 span{color:var(--ac-text)}
p{text-align:center;font-size:13px;color:var(--text-2);margin:6px 0 20px}
input{width:100%;min-height:40px;padding:0 12px;margin-bottom:10px;font:inherit;color:var(--text);background:var(--sunken);
border:1px solid var(--line-strong);border-radius:5px;outline:none;box-shadow:inset 0 2px 0 rgba(0,0,0,.18)}
input::placeholder{color:var(--text-3)}
input:focus{border-color:var(--ac);box-shadow:0 0 0 3px rgba(168,85,247,.22)}
#code{font:600 18px ui-monospace,monospace;letter-spacing:.4em;text-align:center}
button{width:100%;min-height:40px;font:600 14px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#fff;cursor:pointer;background:var(--ac2);
border:1px solid #4c1d95;border-radius:5px;box-shadow:inset 0 1px 0 rgba(255,255,255,.25),inset 0 -3px 0 rgba(0,0,0,.3)}
button:hover{background:#8b4cf0}
button:active{transform:translateY(1px);box-shadow:inset 0 1px 0 rgba(255,255,255,.25),inset 0 -1px 0 rgba(0,0,0,.3)}
button:disabled{opacity:.6;cursor:wait}
#err{color:var(--bad);font-size:12.5px;min-height:18px;margin-top:10px;text-align:center}
.foot{margin-top:14px;text-align:center;font-size:11.5px;color:var(--text-3)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
</style>
<script>try{var t=localStorage.getItem('pkTheme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');document.documentElement.dataset.theme=t}catch(e){}</script>
</head><body>
<main class="card">
<div class="login-logo"><img src="/icon.svg" alt=""></div>
<h1 aria-label="PKHosting">PK<span>Hosting</span></h1><p id="hint">Introduce la contraseña del panel</p>
<form id="step1" autocomplete="on">
<label for="pw" class="sr">Contraseña</label>
<input type="password" id="pw" name="password" placeholder="Contraseña" autocomplete="current-password" required autofocus>
<button type="submit" id="go1">Entrar</button></form>
<form id="step2" autocomplete="off" style="display:none">
<label for="code" class="sr">Código de verificación de 6 dígitos</label>
<input type="text" id="code" name="code" placeholder="000000" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required>
<button type="submit" id="go2">Verificar</button></form>
<div id="err" role="alert" aria-live="assertive"></div>
<div class="foot">Panel de servidores de Minecraft</div></main>
<script>
let preTok = '';
const err = document.getElementById('err');
async function send(body, btn){
  btn.disabled = true; err.textContent = '';
  try {
    const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (r.status === 429) { err.textContent = d.msg || 'Demasiados intentos. Espera unos minutos.'; return null; }
    if (!r.ok && !d.msg) { err.textContent = 'Error del servidor (' + r.status + ')'; return null; }
    if (!d.ok) { err.textContent = d.msg || 'Error'; return null; }
    return d;
  } catch (e) {
    err.textContent = 'No se pudo conectar con el panel';
    return null;
  } finally { btn.disabled = false; }
}
document.getElementById('step1').addEventListener('submit', async e => {
  e.preventDefault();
  const d = await send({password: document.getElementById('pw').value}, document.getElementById('go1'));
  if (!d) return;
  if (d.totp) {
    preTok = d.pre;
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
    document.getElementById('hint').textContent = 'Introduce el código de tu app de autenticación';
    document.getElementById('code').focus();
    return;
  }
  location.reload();
});
document.getElementById('step2').addEventListener('submit', async e => {
  e.preventDefault();
  const d = await send({pre: preTok, code: document.getElementById('code').value}, document.getElementById('go2'));
  if (d) location.reload();
});
</script></body></html>"""

# ── Límite de intentos de login (por IP, en memoria) ──
_login_fails = {}
_login_guard = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 600
LOGIN_LOCK_S = 300


def _login_wait(ip):
    with _login_guard:
        rec = _login_fails.get(ip)
    if rec and rec["until"] > time.time():
        return int(rec["until"] - time.time()) + 1
    return 0


def _login_fail_hit(ip):
    now = time.time()
    with _login_guard:
        rec = _login_fails.get(ip)
        if not rec or now - rec["first"] > LOGIN_WINDOW_S:
            rec = {"first": now, "count": 0, "until": 0}
        rec["count"] += 1
        if rec["count"] >= LOGIN_MAX_FAILS:
            rec = {"first": now, "count": 0, "until": now + LOGIN_LOCK_S}
        _login_fails[ip] = rec


def _login_ok(ip):
    with _login_guard:
        _login_fails.pop(ip, None)


def _content_disposition(name):
    """Cabecera de descarga con el nombre saneado (sin comillas ni saltos de línea)."""
    safe = "".join(c for c in str(name) if c.isprintable() and c not in '"\\/;')
    return f'attachment; filename="{safe or "descarga"}"'


MAX_JSON_BODY = 2 * 1024 * 1024
PUBLIC_PATHS = ("/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/icon.svg",
                "/static/pixelify.ttf")   # la fuente la usa también la página de login


class PKHostingPanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; "
                         "font-src 'self' data:; "
                         "img-src 'self' data: https://mc-heads.net https://cdn.modrinth.com; "
                         "connect-src 'self' https://spark.lucko.me; "
                         "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
        super().end_headers()

    def _secure_flag(self):
        if CFG.get("ssl_cert") or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return "; Secure"
        return ""

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
        if urlparse(self.path).path in ("/", "/index.html"):
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

    def _stream_file(self, path, name):
        """Envía un archivo en bloques de 1 MB (no lo carga entero en memoria)."""
        try:
            f = open(path, "rb")
        except OSError:
            return self.send_json({"ok": False, "msg": "no se pudo leer"}, code=500)
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.send_header("Content-Disposition", _content_disposition(name))
            self.end_headers()
            try:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
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
                if ext in STATIC_RAW:
                    with open(os.path.join(STATIC_DIR, name), "rb") as f:
                        body = f.read()
                else:
                    body = render_static(name).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", STATIC_TYPES[ext])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400" if ext in STATIC_RAW else "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/stats":
            return self.send_json(get_server_stats_data())

        if u.path == "/api/console":
            qs = parse_qs(u.query)
            try:
                since = int(qs["since"][0]) if "since" in qs else None
            except ValueError:
                since = None
            return self.send_json(read_console_since(since, (qs.get("src") or [""])[0], 250))

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
            return self._stream_file(target, os.path.basename(target))
            return

        if u.path == "/api/browse":
            qs = parse_qs(u.query)
            raw = ((qs.get("path", [""]) or [""])[0] or "").strip() or "~"
            home = os.path.realpath(os.path.expanduser("~"))
            p = os.path.realpath(os.path.expanduser(raw))
            if p != home and not p.startswith(home + os.sep):
                return self.send_json({"ok": False, "msg": "Solo se puede explorar dentro de tu carpeta personal"}, code=403)
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
            parent = os.path.dirname(p) if p != home else home
            return self.send_json({"ok": True, "path": p, "parent": parent,
                                   "dirs": dirs, "home": os.path.expanduser("~")})

        if u.path == "/api/servers":
            cur = S().id
            return self.send_json({"servers": [
                {"id": s.id, "name": s.name, "mc_port": s.mc_port,
                 "rcon_port": s.rcon_port, "max_mem_gb": s.max_mem_gb,
                 "server_dir": s.server_dir, "running": _srv_running(s),
                 "current": s.id == cur} for s in list(SERVERS.values())],
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
            return self._stream_file(os.path.join(dg.logs_dir(S().server_dir), name), name)

        if u.path == "/api/mods":
            return self.send_json({"ok": True, "mods": list_mods()})

        if u.path == "/api/mod-updates":
            srv = S()
            st = _updates_state(srv)
            if parse_qs(u.query).get("refresh"):
                if not st["running"]:
                    st["running"] = True
                    threading.Thread(target=refresh_mod_updates, args=(srv,), daemon=True).start()
            cache = _updates_load(srv)
            avail = [dict(v, id=k) for k, v in cache.items()
                     if not v.get("error") and not v.get("same", True) and v.get("latest")]
            unknown = sum(1 for v in cache.values() if v.get("error"))
            return self.send_json({"ok": True, "running": st["running"], "updated": st["updated"],
                                   "available": sorted(avail, key=lambda x: x.get("title") or x["id"]),
                                   "checked": len(cache), "unknown": unknown})

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
            return self._stream_file(target, name)
            return

        if u.path == "/api/backup-cfg":
            return self.send_json({"ok": True, "config": backup_cfg_get(S())})

        if u.path == "/api/uiconfig":
            return self.send_json({"ok": True, "quick_commands":
                CFG.get("quick_commands") or ["say ¡Hola!", "list", "save-all"]})

        if u.path == "/api/history":
            try:
                lim = max(60, min(10080, int(parse_qs(u.query).get("limit", ["10080"])[0])))
            except (TypeError, ValueError):
                lim = 10080
            return self.send_json({"ok": True, **history_read(S(), lim)})

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

        if u.path == "/api/world/export":
            qs = parse_qs(u.query)
            dim = (qs.get("dimension", ["overworld"])[0] or "overworld").strip()
            fn = (qs.get("file", [""])[0] or "").strip()
            if not re.match(r"^r\.-?\d+\.-?\d+\.mca$", fn):
                return self.send_json({"ok": False, "msg": "Archivo inválido"}, code=400)
            _, dim_paths = mca_world_dirs(S())
            target_dir = dim_paths.get(dim)
            fp = os.path.join(target_dir, fn) if target_dir else None
            if not fp or not os.path.isfile(fp):
                return self.send_json({"ok": False, "msg": "No existe"}, code=404)
            return self._stream_file(fp, f"{dim}-{fn}")
            return

        if u.path == "/api/world/players":
            return self.send_json({"ok": True, "players": player_positions()})

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
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            return self.send_json({"ok": False, "msg": "Origen no permitido"}, code=403)
        if u.path == "/api/upload":
            # binario: no pre-leer el cuerpo como texto
            if u.path not in PUBLIC_PATHS and self._need_auth():
                return
            if not self._select_server(u):
                return self.send_json({"error": "servidor desconocido"}, code=404)
            qs = parse_qs(u.query)
            return handle_upload(self, qs.get("path", [""])[0])
        # Auth antes de leer el cuerpo y con tope de tamaño (evita DoS por memoria)
        if u.path not in PUBLIC_PATHS and self._need_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send_json({"ok": False, "msg": "Content-Length inválido"}, code=400)
        if length < 0:
            return self.send_json({"ok": False, "msg": "Content-Length inválido"}, code=400)
        if length > (4096 if u.path == "/api/login" else MAX_JSON_BODY):
            return self.send_json({"ok": False, "msg": "Cuerpo demasiado grande"}, code=413)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if u.path == "/api/login":
            ip = self.client_address[0]
            wait = _login_wait(ip)
            if wait:
                return self.send_json({"ok": False, "msg": f"Demasiados intentos. Espera {wait} s"}, code=429)
            pre, code = payload.get("pre", ""), payload.get("code", "")
            if pre and totp_enabled():
                if _pre_consume(pre) and totp_verify(CFG.get("totp_secret"), code, once=True):
                    tok = new_session()
                    _login_ok(ip)
                    body = json.dumps({"ok": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Set-Cookie", f"pksess={tok}; HttpOnly; Path=/; SameSite=Lax{self._secure_flag()}")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                _login_fail_hit(ip)
                time.sleep(1)
                return self.send_json({"ok": False, "msg": "Código incorrecto"}, code=401)
            pw = payload.get("password", "")
            if auth_enabled() and verify_password(pw, CFG.get("panel_password_hash", "")):
                if totp_enabled():
                    return self.send_json({"ok": True, "totp": True, "pre": _pre_issue()})
                tok = new_session()
                _login_ok(ip)
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"pksess={tok}; HttpOnly; Path=/; SameSite=Lax{self._secure_flag()}")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if auth_enabled():
                _login_fail_hit(ip)
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
            drop_other_sessions(self._cookie_token())
            return self.send_json({"ok": True, "msg": "Contraseña actualizada"})

        if u.path == "/api/totp":
            action = payload.get("action", "")
            if action == "status":
                return self.send_json({"ok": True, "enabled": totp_enabled()})
            if action == "setup":
                sec = __import__("base64").b32encode(secrets.token_bytes(20)).decode().rstrip("=")
                with _totp_lock:
                    globals()["_totp_pending"] = sec
                uri = (f"otpauth://totp/PKHosting:admin?secret={sec}"
                       f"&issuer=PKHosting&algorithm=SHA1&digits=6&period=30")
                return self.send_json({"ok": True, "secret": sec, "uri": uri})
            if action == "enable":
                with _totp_lock:
                    pend = globals()["_totp_pending"]
                if not pend:
                    return self.send_json({"ok": False, "msg": "Genera primero el secreto"}, code=400)
                if not totp_verify(pend, payload.get("code", "")):
                    return self.send_json({"ok": False, "msg": "Código incorrecto, revisa la hora"}, code=400)
                CFG["totp_secret"] = pend
                CFG["totp_enabled"] = True
                save_panel_setting("totp_secret", pend)
                save_panel_setting("totp_enabled", True)
                with _totp_lock:
                    globals()["_totp_pending"] = None
                return self.send_json({"ok": True, "msg": "Segundo factor activado"})
            if action == "disable":
                if not totp_verify(CFG.get("totp_secret"), payload.get("code", "")):
                    if not verify_password(payload.get("current", ""), CFG.get("panel_password_hash", "")):
                        return self.send_json({"ok": False, "msg": "Código o contraseña incorrectos"}, code=400)
                CFG["totp_enabled"] = False
                save_panel_setting("totp_enabled", False)
                return self.send_json({"ok": True, "msg": "Segundo factor desactivado"})
            return self.send_json({"ok": False, "msg": "Acción inválida"}, code=400)

        if u.path == "/api/start":
            ok, msg = start_server_action()
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/stop":
            ok, msg = stop_server_action(force=False)
            return self.send_json({"ok": ok, "msg": msg})

        if u.path == "/api/restart":
            try:
                minutes = max(0, min(60, int(payload.get("minutes", 0) or 0)))
            except (TypeError, ValueError):
                minutes = 0
            if minutes:
                ok, msg = countdown_restart_action(minutes)
            else:
                ok, msg = restart_server_action()
            return self.send_json({"ok": ok, "msg": msg, "cancellable": bool(_countdown_of(S())["running"])})

        if u.path == "/api/restart/cancel":
            ok, msg = cancel_countdown_action()
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
            elif action == "prune_preview":
                st = _prune_state(S().id)
                if st["running"]:
                    return self.send_json({"ok": True, "running": True,
                                             "progress": st.get("progress", "")})
                try:
                    mm = max(0, min(100000, float(payload.get("max_min", 30))))
                    rr = max(0, min(100000, int(payload.get("radius", 512))))
                except (TypeError, ValueError):
                    return self.send_json({"ok": False, "msg": "Valores inválidos"}, code=400)
                with _prune_lock:
                    st.update(running=True, progress="iniciando", result=None, error=None)
                threading.Thread(target=_prune_run, args=(S().id, dim, mm, rr),
                                   daemon=True).start()
                return self.send_json({"ok": True, "running": True, "progress": "iniciando"})
            elif action == "prune_status":
                st = _prune_state(S().id)
                out = {"ok": True, "running": st["running"], "progress": st.get("progress", "")}
                if st.get("error"):
                    out.update(done=True, error=st["error"])
                elif st.get("result") is not None:
                    out.update(done=True, **st["result"])
                return self.send_json(out)
            elif action == "restore_chunks":
                ok, msg = mca_restore_chunks(dim, payload.get("chunks", []))
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
            if action in ("update", "rollback"):
                if find_running_mc_pid() is not None:
                    return self.send_json({"ok": False, "msg": "Detén el servidor para actualizar o revertir mods"}, code=400)
                base = os.path.basename(name)
                if not base or "/" in name or "\\" in name or ".." in name:
                    return self.send_json({"ok": False, "msg": "Nombre inválido"}, code=400)
                on_f = os.path.join(mods, base if base.endswith(".jar") else base + ".jar")
                prev_f = on_f + ".prev"
                try:
                    if action == "rollback":
                        if not os.path.isfile(prev_f):
                            return self.send_json({"ok": False, "msg": "No hay copia anterior"}, code=404)
                        if os.path.isfile(on_f):
                            os.replace(on_f, on_f + ".tmp")
                            os.replace(prev_f, on_f)
                            os.replace(on_f + ".tmp", prev_f)
                        else:
                            os.replace(prev_f, on_f)
                        _updates_save(S(), {})
                        return self.send_json({"ok": True, "msg": f"{base} revertido (requiere reinicio)"})
                    cache = _updates_load(S())
                    meta = _mod_toml_meta(on_f) if os.path.isfile(on_f) else []
                    cand = next((dict(v, id=k) for k, v in cache.items()
                                 if v.get("file") == os.path.basename(on_f) and v.get("url")), None)
                    if cand is None and meta:
                        r = mod_update_check(S(), meta[0]["id"], meta[0].get("version"))
                        if r.get("url"):
                            cand = dict(r, id=meta[0]["id"])
                    if not cand or not cand.get("url"):
                        return self.send_json({"ok": False, "msg": "Sin actualización disponible"}, code=404)
                    if not str(cand["url"]).startswith("https://cdn.modrinth.com/"):
                        return self.send_json({"ok": False, "msg": "URL de descarga no permitida"}, code=400)
                    req = urllib.request.Request(cand["url"], headers={"User-Agent": "PKHosting-Panel/2.0"})
                    tmp = on_f + ".dl"
                    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
                    if os.path.isfile(on_f):
                        if os.path.isfile(prev_f):
                            os.remove(prev_f)
                        os.replace(on_f, prev_f)
                    os.replace(tmp, on_f)
                    _updates_save(S(), {})
                    return self.send_json({"ok": True, "msg": f"{base} actualizado a {cand.get('latest', '')} (copia anterior guardada, requiere reinicio)"})
                except Exception as e:
                    try:
                        if os.path.isfile(on_f + ".dl"):
                            os.remove(on_f + ".dl")
                    except Exception:
                        pass
                    return self.send_json({"ok": False, "msg": str(e)[:200]}, code=500)
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

_workers_started = False


def start_workers():
    """Hilos de fondo (TPS, conexiones, historial, métricas, tareas). Se arrancan desde
    __main__ y no al importar el módulo (importarlo, p. ej. en tests, no lanza hilos)."""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    for fn in (tps_worker, connections_worker, history_worker, metrics_worker, scheduler_worker):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


def _reset_password_cli():
    """Restablece la contraseña del panel (y desactiva el 2FA) sin arrancar el servidor web."""
    import getpass
    pw = getpass.getpass("Nueva contraseña del panel (mín. 8 caracteres): ")
    if len(pw) < 8:
        print("Mínimo 8 caracteres")
        return 1
    if getpass.getpass("Repite la contraseña: ") != pw:
        print("No coinciden")
        return 1
    save_panel_setting("panel_password_hash", hash_password(pw))
    if totp_enabled():
        save_panel_setting("totp_enabled", False)
        print("2FA desactivado (vuelve a activarlo desde Configuración).")
    print("Contraseña actualizada. Reinicia el panel para aplicarla.")
    return 0


if __name__ == "__main__":
    if "--reset-password" in sys.argv:
        raise SystemExit(_reset_password_cli())
    start_workers()
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
