"""Operación del servidor: estado, métricas, jugadores, arranque/parada, backups, Discord y tareas."""
from pkhosting.core import *  # noqa: F401,F403

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
            for srv in list(SERVERS.values()):
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
            for srv in list(SERVERS.values()):
                _ctx.srv = srv
                try:
                    _conn_scan(srv)
                finally:
                    _ctx.srv = None
        except Exception:
            pass
        time.sleep(15)




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
        if len(lines) > 10100:
            with open(_hist_file(srv), "w") as f:
                f.writelines(lines[-10080:])
    except Exception:
        pass


HIST_MAX_POINTS = 1500


def _downsample(items):
    n = len(items)
    if n <= HIST_MAX_POINTS:
        return items
    step = (n + HIST_MAX_POINTS - 1) // HIST_MAX_POINTS
    return items[::step]


def history_read(srv, limit=10080):
    try:
        with open(_hist_file(srv)) as f:
            lines = f.readlines()[-limit:]
        items = _downsample([json.loads(l) for l in lines if l.strip()])
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
            for srv in list(SERVERS.values()):
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
            for srv in list(SERVERS.values()):
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
            os.makedirs(os.path.dirname(s.child_log), exist_ok=True)   # servidor nuevo: aún sin logs/
            try:  # rotación simple: el log del hijo crecía sin límite
                if os.path.getsize(s.child_log) > 5 * 1024 * 1024:
                    os.replace(s.child_log, s.child_log + ".1")
            except OSError:
                pass
            with open(s.child_log, "ab", buffering=0) as logf:
                s.proc = subprocess.Popen(
                    s.start_cmd, cwd=s.server_dir,
                    stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                    close_fds=True, start_new_session=True  # sobrevive si el panel se detiene
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
        _ctx.srv = srv0
        for _ in range(120):  # esperar a que el mundo termine de guardarse y el proceso salga
            if find_running_mc_pid() is None:
                break
            time.sleep(1)
        else:
            return  # no se apagó a tiempo: no lanzar una segunda instancia
        time.sleep(1)
        start_server_action(auto=auto)
    threading.Thread(target=_delayed, daemon=True).start()
    return True, "Reiniciando servidor..."


_countdowns = {}  # id de servidor -> {"running": bool, "cancel": Event}
_countdown_lock = threading.Lock()


def _countdown_of(srv):
    return _countdowns.setdefault(srv.id, {"running": False, "cancel": None})


def countdown_restart_action(minutes):
    """Reinicio con avisos en el chat (N min, luego 30, 10 y 5…1 s), save-all flush y apagado."""
    s = S()
    if find_running_mc_pid() is None:
        return restart_server_action()
    with _countdown_lock:
        cd = _countdown_of(s)
        if cd["running"]:
            return False, "Ya hay un reinicio programado"
        cancel = threading.Event()
        cd.update(running=True, cancel=cancel)
    total = max(1, min(60, int(minutes))) * 60
    marks = sorted({m for m in [60 * k for k in range(1, total // 60 + 1)] + [30, 10, 5, 4, 3, 2, 1]
                    if m <= total}, reverse=True)

    def _label(sec):
        return f"{sec // 60} min" if sec >= 60 else f"{sec} s"

    def _run():
        _ctx.srv = s
        try:
            t0 = time.time()
            for rem in marks:
                if cancel.wait(max(0.0, t0 + total - rem - time.time())):
                    send_command_action("say Reinicio cancelado")
                    return
                send_command_action(f"say Reinicio del servidor en {_label(rem)}")
            send_command_action("save-all flush")
            time.sleep(3)
            stop_server_action(force=False)
            for _ in range(180):
                if find_running_mc_pid() is None:
                    break
                time.sleep(1)
            start_server_action()
        finally:
            with _countdown_lock:
                cd.update(running=False, cancel=None)
            _ctx.srv = None

    threading.Thread(target=_run, daemon=True).start()
    return True, f"Reinicio con aviso de {minutes} min en marcha (cancelable)"


def cancel_countdown_action():
    with _countdown_lock:
        cd = _countdown_of(S())
        ev = cd.get("cancel")
        if not cd["running"] or ev is None:
            return False, "No hay ningún reinicio programado"
        ev.set()
    return True, "Reinicio cancelado"


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
        "crash_blocked": srv.crash_blocked,
        "crash_recent": len(srv.crash_guard.recent()),
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
                                    send_fn=_send, running_fn=_running,
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
            for srv in list(SERVERS.values()):
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


__all__ = [n for n in dir() if not n.startswith("__")]
