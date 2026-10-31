"""Fichas de jugador, ping de red, Spark, flota y subida/descompresión de archivos."""
from pkhosting.world import *  # noqa: F401,F403

def get_player_details(srv, player_name):
    if not PLAYER_NAME_RE.match(player_name or ""):
        return {"ok": False, "msg": "Nombre de jugador inválido"}
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
        for candidate in (wname, "world"):
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


MAX_UNZIP_BYTES = 2 * 1024 * 1024 * 1024
MAX_UNZIP_FILES = 20000


def safe_extract_zip(zp, dest_dir):
    """Extrae un zip rechazando Zip-Slip, symlinks y zip bombs."""
    import zipfile as _zf
    root = os.path.realpath(dest_dir)
    with _zf.ZipFile(zp) as z:
        infos = z.infolist()
        if len(infos) > MAX_UNZIP_FILES:
            raise ValueError("zip con demasiados archivos")
        if sum(i.file_size for i in infos) > MAX_UNZIP_BYTES:
            raise ValueError("zip descomprimido excede el límite de 2 GB")
        for m in infos:
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # symlink
            target = os.path.realpath(os.path.join(root, m.filename))
            if target != root and not target.startswith(root + os.sep):
                continue
            z.extract(m, root)


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
        done = []
        for fname in saved:
            if not fname.lower().endswith(".zip"):
                continue
            zp = os.path.join(dest_dir, fname)
            try:
                safe_extract_zip(zp, dest_dir)
                os.remove(zp)
                done.append(fname)
            except Exception as e:
                return handler.send_json({"ok": False, "msg": f"Subido pero falló unzip de {fname}: {e}"}, code=500)
        if done:
            msg += f" · descomprimido(s): {', '.join(done)}"
    return handler.send_json({"ok": True, "msg": msg})


__all__ = [n for n in dir() if not n.startswith("__")]
