#!/usr/bin/env python3
"""
PKHosting — sistema de backups (mundo + configs del servidor).

  backup.py --run            backup programado (systemd timer) + purga
  backup.py --prune          solo purga según retención
  backup.py --list           lista backups
  backup.py --restore FILE   restaura (pide confirmación, detiene el server)

Retención: se borran los backups de más de `retention_days`, excepto el ÚLTIMO
de cada mes natural, que se conserva para siempre (12 al año).

Formato: pkhosting-YYYYMMDD-HHMMSS[-etiqueta].tar.gz
Contenido: <world>/ + server.properties + *.json (whitelist/ops/bans) +
           user_jvm_args.txt, con rutas relativas a server_dir.

Solo stdlib. Lo usa panel.py (import) y el timer systemd (CLI).
"""
import datetime as _dt
import fcntl as _fcntl
import json as _json
import os as _os
import re as _re
import shutil as _shutil
import socket as _sock
import struct as _struct
import sys as _sys
import tarfile as _tarfile
import time as _time

FNAME_RE = _re.compile(r"^pkhosting-(\d{8})-(\d{6})(-[A-Za-z0-9_-]+)?\.tar\.gz$")
TIME_FMT = "%Y%m%d-%H%M%S"


def _now_tag():
    return _dt.datetime.now().strftime(TIME_FMT)


def _parse_time(name):
    m = FNAME_RE.match(_os.path.basename(name))
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1) + "-" + m.group(2), TIME_FMT)
    except Exception:
        return None


def list_backups(backup_dir):
    """[{name, size_b, mtime, month, monthly}] ordenados por fecha asc."""
    items = []
    try:
        names = _os.listdir(backup_dir)
    except Exception:
        return []
    for n in names:
        if not FNAME_RE.match(n):
            continue
        full = _os.path.join(backup_dir, n)
        try:
            st = _os.stat(full)
        except Exception:
            continue
        t = _parse_time(n)
        items.append({"name": n, "size_b": st.st_size, "mtime": int(st.st_mtime),
                      "month": t.strftime("%Y-%m") if t else "?"})
    items.sort(key=lambda x: x["mtime"])
    keepers = monthly_keepers([i["name"] for i in items])
    for i in items:
        i["monthly"] = i["name"] in keepers
    return items


def monthly_keepers(names):
    """Último backup de cada mes natural → se conserva siempre."""
    best = {}
    for n in names:
        t = _parse_time(n)
        if not t:
            continue
        k = t.strftime("%Y-%m")
        if k not in best or t > _parse_time(best[k]):
            best[k] = n
    return set(best.values())


def prune_backups(backup_dir, retention_days=7, keep_monthly=True, min_keep=3):
    """Borra backups viejos. Devuelve (conservados, borrados).
    Siempre se conservan los `min_keep` más recientes (si los backups fallan
    durante días, la retención no debe vaciar la carpeta)."""
    items = list_backups(backup_dir)
    cutoff = _time.time() - retention_days * 86400
    keepers = monthly_keepers([i["name"] for i in items]) if keep_monthly else set()
    if min_keep > 0:
        keepers |= {i["name"] for i in items[-min_keep:]}
    kept, deleted = [], []
    for i in items:
        t = _parse_time(i["name"])
        age_ref = t.timestamp() if t else i["mtime"]  # misma referencia que los "mensuales"
        if i["name"] in keepers:
            kept.append(i["name"])
        elif age_ref < cutoff:
            try:
                _os.remove(_os.path.join(backup_dir, i["name"]))
                deleted.append(i["name"])
            except Exception as e:
                kept.append(f"{i['name']} (error: {e})")
        else:
            kept.append(i["name"])
    return kept, deleted


def check_writable(backup_dir):
    try:
        _os.makedirs(backup_dir, exist_ok=True)
        probe = _os.path.join(backup_dir, ".writetest")
        with open(probe, "w") as f:
            f.write("ok")
        _os.remove(probe)
        return True, ""
    except Exception as e:
        return False, str(e)


def backup_sources(server_dir, world_name):
    """Archivos a incluir (rutas relativas a server_dir)."""
    rels = []
    w = _os.path.join(server_dir, world_name)
    if _os.path.isdir(w):
        for dp, _, fns in _os.walk(w):
            for fn in fns:
                full = _os.path.join(dp, fn)
                rels.append(_os.path.relpath(full, server_dir))
    for extra in ("server.properties", "user_jvm_args.txt", "whitelist.json",
                  "ops.json", "banned-players.json", "banned-ips.json"):
        if _os.path.isfile(_os.path.join(server_dir, extra)):
            rels.append(extra)
    return rels


def _rcon_pw(pass_files):
    for p in pass_files:
        try:
            with open(p) as f:
                pw = f.read().strip()
                if pw:
                    return pw
        except Exception:
            continue
    return None


def rcon_send(host, port, password, cmd, timeout=5):
    try:
        s = _sock.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)

        def pkt(pid, ptype, payload):
            body = _struct.pack("<ii", pid, ptype) + payload.encode() + b"\x00\x00"
            return _struct.pack("<i", len(body)) + body

        def rd():
            ln = _struct.unpack("<i", s.recv(4))[0]
            data = b""
            while len(data) < ln:
                data += s.recv(ln - len(data))
            rid, _ = _struct.unpack("<ii", data[:8])
            return rid, data[8:-2].decode("utf-8", "replace")

        s.sendall(pkt(1, 3, password))
        rid, _ = rd()
        if rid == -1:
            s.close()
            return False, "auth RCON fallida"
        s.sendall(pkt(2, 2, cmd))
        _, resp = rd()
        s.close()
        return True, resp
    except Exception as e:
        return False, str(e)


def verify_archive(path):
    """Lee el tar.gz completo (valida CRC de gzip y estructura). Lanza si está corrupto."""
    with _tarfile.open(path, "r:gz") as tf:
        for m in tf:
            if m.isfile():
                f = tf.extractfile(m)
                while f.read(1 << 20):
                    pass


def _acquire_lock(backup_dir):
    """Lock exclusivo entre el panel y el timer (flock). None si ya hay otro trabajo."""
    try:
        _os.makedirs(backup_dir, exist_ok=True)
        f = open(_os.path.join(backup_dir, ".lock"), "w")
        _fcntl.flock(f, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return f
    except (BlockingIOError, OSError):
        return None


def _port_open(port, host="127.0.0.1"):
    try:
        with _sock.create_connection((host, int(port)), timeout=1.5):
            return True
    except Exception:
        return False


def _tree_bytes(server_dir, rels):
    total = 0
    for r in rels:
        try:
            total += _os.path.getsize(_os.path.join(server_dir, r))
        except OSError:
            pass
    return total


def run_backup(server_dir, world_name, backup_dir, send_fn=None,
               tag=None, retention_days=7, keep_monthly=True, log=None,
               progress=None, running_fn=None):
    """Backup completo + purga (con lock compartido panel/timer).
    send_fn(cmd)->(ok,resp) o None (sin freeze). running_fn()->bool indica si el
    servidor corre: si corre y no se puede congelar el guardado, se aborta.
    progress(pct 0-100, stage) se llama en cada etapa (para la barra del panel)."""
    lk = _acquire_lock(backup_dir)
    if lk is None:
        return False, "ya hay otro backup o restauración en curso"
    try:
        return _run_backup_locked(server_dir, world_name, backup_dir, send_fn, tag,
                                  retention_days, keep_monthly, log, progress, running_fn)
    finally:
        lk.close()


def _run_backup_locked(server_dir, world_name, backup_dir, send_fn=None,
                       tag=None, retention_days=7, keep_monthly=True, log=None,
                       progress=None, running_fn=None):
    def say(m):
        (log or print)(f"[backup] {m}", flush=True)

    def prog(pct, stage):
        try:
            if progress:
                progress(int(pct), stage)
        except Exception:
            pass

    prog(2, "preparando")
    ok, err = check_writable(backup_dir)
    if not ok:
        return False, f"destino no escribible ({backup_dir}): {err}"
    rels = backup_sources(server_dir, world_name)
    if not rels:
        return False, "nada que respaldar (¿world_name correcto?)"
    try:
        free = _shutil.disk_usage(backup_dir).free
        need = max(1024**3, int(_tree_bytes(server_dir, rels) * 0.6))
        if free < need:
            return False, f"sin espacio libre en destino ({free/1e9:.1f} GB libres, ~{need/1e9:.1f} GB necesarios)"
    except Exception:
        pass

    frozen = False
    if send_fn:
        prog(4, "congelando guardado del mundo")
        say("congelando guardado (save-off + save-all flush)...")
        ok_off, resp_off = send_fn("save-off")
        if ok_off:
            frozen = True
            send_fn("save-all flush")  # por RCON bloquea hasta terminar de guardar
            _time.sleep(2)
        elif running_fn is None or running_fn():
            return False, f"no se pudo congelar el guardado del mundo ({resp_off}); backup abortado"
        else:
            say("servidor apagado: backup sin congelar")
    else:
        prog(4, "listando archivos")

    name = f"pkhosting-{_now_tag()}{('-' + tag) if tag else ''}.tar.gz"
    # tag con guion podría romper FNAME_RE en prune si tiene mayúsculas: normalizar
    name = _re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if not FNAME_RE.match(name):
        name = f"pkhosting-{_now_tag()}.tar.gz"
    dest = _os.path.join(backup_dir, name)
    part = dest + ".partial"  # no aparece en listados hasta estar verificado
    try:
        say(f"comprimiendo {len(rels)} archivos → {name} ...")
        prog(8, f"comprimiendo 0/{len(rels)} archivos")
        skipped = 0
        n = len(rels)
        step = max(1, n // 200)  # ~200 actualizaciones como máximo
        with _tarfile.open(part, "w:gz", compresslevel=3) as tf:
            for i, r in enumerate(rels, 1):
                try:
                    tf.add(_os.path.join(server_dir, r), arcname=r)
                except (FileNotFoundError, NotADirectoryError, _tarfile.TarError, OSError):
                    # Mundo vivo: el server crea/borra temporales a mitad del tar
                    skipped += 1
                if i % step == 0 or i == n:
                    prog(8 + int(80 * i / n), f"comprimiendo {i}/{n} archivos")
        if skipped:
            say(f"omitidos {skipped} temporales que el server borró a mitad del backup")
        size = _os.path.getsize(part) / 1e9
        prog(90, f"verificando ({size:.2f} GB)")
        verify_archive(part)
        _os.replace(part, dest)
        say(f"OK {name} ({size:.2f} GB)")
    except Exception as e:
        for p in (part, dest):
            try:
                _os.remove(p)
            except Exception:
                pass
        return False, f"error comprimiendo: {e}"
    finally:
        if frozen:
            send_fn("save-on")

    prog(93, "aplicando retención")
    kept, deleted = prune_backups(backup_dir, retention_days, keep_monthly)
    say(f"purga: {len(deleted)} borrados, {len(kept)} conservados")
    prog(100, "completado")
    return True, f"{name} ({size:.2f} GB), purga: {len(deleted)} borrados"


def restore_backup(server_dir, backup_dir, name, stop_fn=None, start_fn=None,
                   is_running_fn=None, pre_backup=True, retention_days=7,
                   keep_monthly=True, log=None, progress=None):
    """Restaura un backup: stop → (pre-backup) → apartar mundo → extraer → start.
    El mundo actual se aparta antes de extraer, para no mezclar chunks nuevos y viejos."""
    def say(m):
        (log or print)(f"[restore] {m}", flush=True)

    def prog(pct, stage):
        try:
            if progress:
                progress(int(pct), stage)
        except Exception:
            pass

    prog(2, "validando")
    if "/" in name or "\\" in name or ".." in name or not FNAME_RE.match(name):
        return False, "nombre de backup inválido"
    src = _os.path.join(backup_dir, name)
    if not _os.path.isfile(src):
        return False, "backup no encontrado"
    try:
        verify_archive(src)
    except Exception as e:
        return False, f"el backup está dañado, no se restaura: {e}"

    lk = _acquire_lock(backup_dir)
    if lk is None:
        return False, "ya hay otro backup o restauración en curso"
    try:
        if stop_fn:
            prog(5, "deteniendo servidor")
            say("deteniendo servidor...")
            ok, msg = stop_fn()
            say(f"stop: {msg}")
        if is_running_fn:
            for _ in range(60):
                if not is_running_fn():
                    break
                prog(8, "esperando apagado del servidor")
                _time.sleep(2)
            if is_running_fn():
                return False, "el servidor no se detuvo a tiempo, abortado"
        prog(12, "servidor detenido")
        world = _world_of(server_dir, src)
        if pre_backup:
            # Captura de seguridad del estado actual (no congela: el server ya paró)
            say("backup de seguridad previo...")

            def pre_prog(p, s):
                prog(12 + int(p * 0.43), "pre-backup: " + s)  # 12 → 55
            ok, msg = _run_backup_locked(server_dir, world, backup_dir, send_fn=None,
                                         tag="pre-restore", retention_days=retention_days,
                                         keep_monthly=keep_monthly, log=log, progress=pre_prog)
            say(f"pre-restore: {msg}")
            if not ok:
                return False, f"abortado (falló pre-backup): {msg}"

        cur = _os.path.join(server_dir, world)
        aside = None
        if _os.path.isdir(cur):
            aside = f"{cur}.pre-restore-{_now_tag()}"
            _os.rename(cur, aside)
            say(f"mundo actual apartado en {_os.path.basename(aside)}")
        try:
            say(f"extrayendo {name} ...")
            with _tarfile.open(src, "r:gz") as tf:
                members = tf.getmembers()
                n = len(members) or 1
                step = max(1, n // 100)
                for i, m in enumerate(members, 1):
                    tf.extract(m, path=server_dir, filter="data")
                    if i % step == 0 or i == n:
                        prog(55 + int(35 * i / n), f"extrayendo {i}/{n}")
            prog(92, "verificando extracción")
            say("extracción OK")
        except Exception as e:
            # revertir: quitar lo extraído a medias y devolver el mundo original
            if aside:
                _shutil.rmtree(cur, ignore_errors=True)
                try:
                    _os.rename(aside, cur)
                except OSError:
                    pass
            return False, f"error extrayendo (mundo original restaurado): {e}"
        if aside and pre_backup:
            _shutil.rmtree(aside, ignore_errors=True)  # ya existe el pre-backup verificado
        elif aside:
            say(f"el mundo anterior sigue en {_os.path.basename(aside)}")
    finally:
        lk.close()
    if start_fn:
        prog(95, "arrancando servidor")
        say("arrancando servidor...")
        ok, msg = start_fn()
        say(f"start: {msg}")
    prog(100, "completado")
    return True, f"restaurado {name}"


def _world_of(server_dir, src):
    """Infiere world_name desde el contenido del tar (primer dir de nivel 1)."""
    try:
        with _tarfile.open(src, "r:gz") as tf:
            tops = set()
            for m in tf.getmembers():
                parts = m.name.split("/")
                if len(parts) > 1:
                    tops.add(parts[0])
            for t in sorted(tops):  # orden estable
                if _os.path.isdir(_os.path.join(server_dir, t)):
                    return t
            return sorted(tops)[0] if tops else "world"
    except Exception:
        return "world"


# ── CLI (systemd timer) ──────────────────────────────────────────
CONFIG_DIR = _os.path.expanduser("~/.config/pkhosting")


def _load_servers():
    """[(cfg, rcon_password)] de config.json: la lista servers[] o el servidor único."""
    base = {"server_dir": "~/minecraft-server", "world_name": "world", "mc_port": 25565,
            "rcon_port": 25575, "backup_enabled": False, "backup_dir": "~/mc-backups",
            "retention_days": 7, "keep_monthly": True}
    try:
        with open(_os.path.join(CONFIG_DIR, "config.json")) as f:
            raw = _json.load(f)
    except Exception:
        raw = {}
    base.update({k: v for k, v in raw.items() if k != "servers"})
    entries = raw.get("servers") if isinstance(raw.get("servers"), list) and raw.get("servers") else [{"id": "main"}]
    out = []
    for e in entries:
        cfg = dict(base)
        cfg.update(e)
        sid = str(cfg.get("id", "main"))
        data_dir = cfg.get("data_dir") or _os.path.join(CONFIG_DIR, "servers", sid)
        try:  # backup.json por servidor (lo que se cambia desde el panel)
            with open(_os.path.join(data_dir, "backup.json")) as f:
                ov = _json.load(f)
            if isinstance(ov, dict):
                cfg.update({k: ov[k] for k in ("backup_dir", "backup_time", "retention_days",
                                               "keep_monthly", "backup_enabled") if k in ov})
        except Exception:
            pass
        cfg["id"] = sid
        cfg["server_dir"] = _os.path.expanduser(cfg["server_dir"])
        cfg["backup_dir"] = _os.path.expanduser(cfg["backup_dir"])
        pw = _rcon_pw([cfg.get("rcon_pass_file") or "", _os.path.join(data_dir, "rcon-password"),
                       _os.path.join(CONFIG_DIR, "rcon-password"),
                       _os.path.expanduser("~/.config/mc-panel-rcon")])
        out.append((cfg, pw))
    return out


def _sender(cfg, pw):
    def send(cmd):
        if not pw:
            return False, "sin RCON"
        return rcon_send("127.0.0.1", int(cfg.get("rcon_port", 25575)), pw, cmd)
    return send


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    servers = _load_servers()
    cmd = argv[1]
    rc = 0
    if cmd == "--run":
        force = "--force" in argv
        for cfg, pw in servers:
            if not cfg.get("backup_enabled") and not force:
                print(f"SKIP {cfg['id']}: backup desactivado (usa --force para ignorarlo)")
                continue
            ok, msg = run_backup(
                cfg["server_dir"], cfg.get("world_name", "world"), cfg["backup_dir"],
                send_fn=_sender(cfg, pw), retention_days=int(cfg.get("retention_days", 7)),
                keep_monthly=bool(cfg.get("keep_monthly", True)),
                running_fn=lambda c=cfg: _port_open(c.get("mc_port", 25565)))
            print(("OK " if ok else "FAIL ") + f"{cfg['id']}: {msg}")
            rc = rc or (0 if ok else 1)
        return rc
    cfg, pw = servers[0]
    server_dir, bdir = cfg["server_dir"], cfg["backup_dir"]
    ret, keepm = int(cfg.get("retention_days", 7)), bool(cfg.get("keep_monthly", True))
    send = _sender(cfg, pw)
    if cmd == "--prune":
        kept, deleted = prune_backups(bdir, ret, keepm)
        print(f"conservados={len(kept)} borrados={len(deleted)}")
        for n in deleted:
            print("  -", n)
        return 0
    if cmd == "--list":
        for i in list_backups(bdir):
            tag = " MENSUAL" if i["monthly"] else ""
            print(f"{i['name']}  {i['size_b']/1e9:.2f} GB{tag}")
        return 0
    if cmd == "--restore":
        if len(argv) < 3:
            print("uso: backup.py --restore <archivo> [--yes] [--no-start]")
            return 2
        if "--yes" not in argv:
            print("Esto DETIENE el servidor y sobrescribe el mundo.")
            if input(f"¿Restaurar {argv[2]}? [s/N]: ").lower() not in ("s", "si", "y", "yes"):
                print("cancelado")
                return 0
        import subprocess as _sp

        def _start():
            try:
                _sp.Popen(cfg.get("start_cmd", ["bash", "start.sh"]), cwd=server_dir,
                          stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                          start_new_session=True)
                return True, "servidor iniciado"
            except Exception as e:
                return False, f"no se pudo iniciar: {e}"
        ok, msg = restore_backup(
            server_dir, bdir, argv[2],
            stop_fn=lambda: send("stop"),
            is_running_fn=lambda: _port_open(cfg.get("mc_port", 25565)),
            start_fn=None if "--no-start" in argv else _start,
            retention_days=ret, keep_monthly=keepm)
        print(("OK " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    _sys.exit(main(_sys.argv))
