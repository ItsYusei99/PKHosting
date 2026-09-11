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


def prune_backups(backup_dir, retention_days=7, keep_monthly=True):
    """Borra backups viejos. Devuelve (conservados, borrados)."""
    items = list_backups(backup_dir)
    cutoff = _time.time() - retention_days * 86400
    keepers = monthly_keepers([i["name"] for i in items]) if keep_monthly else set()
    kept, deleted = [], []
    for i in items:
        if i["name"] in keepers:
            kept.append(i["name"])
        elif i["mtime"] < cutoff:
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


def run_backup(server_dir, world_name, backup_dir, send_fn=None,
               tag=None, retention_days=7, keep_monthly=True, log=None):
    """Backup completo + purga. send_fn(cmd)->(ok,resp) o None (sin freeze)."""
    def say(m):
        (log or print)(f"[backup] {m}", flush=True)

    ok, err = check_writable(backup_dir)
    if not ok:
        return False, f"destino no escribible ({backup_dir}): {err}"
    try:
        free = _shutil.disk_usage(backup_dir).free
        if free < 1024**3:
            return False, f"sin espacio libre en destino ({free/1e9:.1f} GB)"
    except Exception:
        pass

    if send_fn:
        say("congelando guardado (save-off + save-all flush)...")
        send_fn("save-off")
        send_fn("save-all flush")
        _time.sleep(5)

    name = f"pkhosting-{_now_tag()}{('-' + tag) if tag else ''}.tar.gz"
    # tag con guion podría romper FNAME_RE en prune si tiene mayúsculas: normalizar
    name = _re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if not FNAME_RE.match(name):
        name = f"pkhosting-{_now_tag()}.tar.gz"
    dest = _os.path.join(backup_dir, name)
    try:
        rels = backup_sources(server_dir, world_name)
        if not rels:
            return False, "nada que respaldar (¿world_name correcto?)"
        say(f"comprimiendo {len(rels)} archivos → {name} ...")
        skipped = 0
        with _tarfile.open(dest, "w:gz", compresslevel=3) as tf:
            for r in rels:
                try:
                    tf.add(_os.path.join(server_dir, r), arcname=r)
                except (FileNotFoundError, NotADirectoryError, _tarfile.TarError, OSError):
                    # Mundo vivo: el server crea/borra temporales a mitad del tar
                    skipped += 1
        if skipped:
            say(f"omitidos {skipped} temporales que el server borró a mitad del backup")
        size = _os.path.getsize(dest) / 1e9
        say(f"OK {name} ({size:.2f} GB)")
    except Exception as e:
        try:
            _os.remove(dest)
        except Exception:
            pass
        return False, f"error comprimiendo: {e}"
    finally:
        if send_fn:
            send_fn("save-on")

    kept, deleted = prune_backups(backup_dir, retention_days, keep_monthly)
    say(f"purga: {len(deleted)} borrados, {len(kept)} conservados")
    return True, f"{name} ({size:.2f} GB), purga: {len(deleted)} borrados"


def restore_backup(server_dir, backup_dir, name, stop_fn=None, start_fn=None,
                   is_running_fn=None, pre_backup=True, retention_days=7,
                   keep_monthly=True, log=None):
    """Restaura un backup: stop → (pre-backup) → extraer → start."""
    def say(m):
        (log or print)(f"[restore] {m}", flush=True)

    if "/" in name or "\\" in name or ".." in name or not FNAME_RE.match(name):
        return False, "nombre de backup inválido"
    src = _os.path.join(backup_dir, name)
    if not _os.path.isfile(src):
        return False, "backup no encontrado"

    if stop_fn:
        say("deteniendo servidor...")
        ok, msg = stop_fn()
        say(f"stop: {msg}")
    if is_running_fn:
        for _ in range(60):
            if not is_running_fn():
                break
            _time.sleep(2)
        if is_running_fn():
            return False, "el servidor no se detuvo a tiempo, abortado"
    if pre_backup:
        # Captura de seguridad del estado actual (no congela: el server ya paró)
        say("backup de seguridad previo...")
        ok, msg = run_backup(server_dir, _world_of(server_dir, src),
                             backup_dir, send_fn=None, tag="pre-restore",
                             retention_days=retention_days,
                             keep_monthly=keep_monthly, log=log)
        say(f"pre-restore: {msg}")
        if not ok:
            return False, f"abortado (falló pre-backup): {msg}"
    try:
        say(f"extrayendo {name} ...")
        with _tarfile.open(src, "r:gz") as tf:
            tf.extractall(path=server_dir, filter="data")
        say("extracción OK")
    except Exception as e:
        return False, f"error extrayendo: {e}"
    if start_fn:
        say("arrancando servidor...")
        ok, msg = start_fn()
        say(f"start: {msg}")
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
            for t in tops:
                if _os.path.isdir(_os.path.join(server_dir, t)):
                    return t
            return sorted(tops)[0] if tops else "world"
    except Exception:
        return "world"


# ── CLI (systemd timer) ──────────────────────────────────────────
def _load_cfg():
    d = _os.path.expanduser("~/.config/pkhosting")
    cfg = {"server_dir": "~/minecraft-server", "world_name": "world",
           "mc_port": 25565, "rcon_port": 25575}
    try:
        cfg.update(_json.load(open(_os.path.join(d, "config.json"))))
    except Exception:
        pass
    cfg["server_dir"] = _os.path.expanduser(cfg["server_dir"])
    pw = _rcon_pw([_os.path.join(d, "rcon-password"),
                   _os.path.expanduser("~/.config/mc-panel-rcon")])
    return cfg, pw


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cfg, pw = _load_cfg()
    server_dir = cfg["server_dir"]
    world = cfg.get("world_name", "world")
    bdir = _os.path.expanduser(cfg.get("backup_dir", "~/mc-backups"))
    ret = int(cfg.get("retention_days", 7))
    keepm = bool(cfg.get("keep_monthly", True))

    def send(cmd):
        if not pw:
            return False, "sin RCON"
        return rcon_send("127.0.0.1", int(cfg.get("rcon_port", 25575)), pw, cmd)

    cmd = argv[1]
    if cmd == "--run":
        ok, msg = run_backup(server_dir, world, bdir, send_fn=send,
                             retention_days=ret, keep_monthly=keepm)
        print(("OK " if ok else "FAIL ") + msg)
        return 0 if ok else 1
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
            print("uso: backup.py --restore <archivo> [--yes]")
            return 2
        if "--yes" not in argv:
            print("Esto DETIENE el servidor y sobrescribe el mundo.")
            if input(f"¿Restaurar {argv[2]}? [s/N]: ").lower() not in ("s", "si", "y", "yes"):
                print("cancelado")
                return 0
        import subprocess as _sp
        ok, msg = restore_backup(
            server_dir, bdir, argv[2],
            stop_fn=lambda: send("stop"), start_fn=None,
            retention_days=ret, keep_monthly=keepm)
        print(("OK " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    _sys.exit(main(_sys.argv))
