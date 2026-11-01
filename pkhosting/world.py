"""Mundo: NBT/MCA, mapa de terreno, poda y restauración de chunks."""
from pkhosting.content import *  # noqa: F401,F403

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

    def _skip(self, tid):
        """Avanza el cursor sin construir objetos (para escaneos rápidos)."""
        if tid == 1:
            self.o += 1
        elif tid == 2:
            self.o += 2
        elif tid in (3, 5):
            self.o += 4
        elif tid in (4, 6):
            self.o += 8
        elif tid == 7:
            self.o += 4 + max(0, struct.unpack(">i", self.d[self.o:self.o + 4])[0])
        elif tid == 8:
            n = struct.unpack(">H", self.d[self.o:self.o + 2])[0]
            self.o += 2 + n
        elif tid == 9:
            et = self.d[self.o]
            n = max(0, struct.unpack(">i", self.d[self.o + 1:self.o + 5])[0])
            self.o += 5
            for _ in range(n):
                self._skip(et)
        elif tid == 10:
            while True:
                st = self.d[self.o]
                self.o += 1
                if st == 0:
                    return
                n = struct.unpack(">H", self.d[self.o:self.o + 2])[0]
                self.o += 2 + n
                self._skip(st)
        elif tid == 11:
            n = max(0, struct.unpack(">i", self.d[self.o:self.o + 4])[0])
            self.o += 4 + 4 * n
        elif tid == 12:
            n = max(0, struct.unpack(">i", self.d[self.o:self.o + 4])[0])
            self.o += 4 + 8 * n
        else:
            raise ValueError(f"TAG desconocido: {tid}")
        if self.o > len(self.d):
            raise ValueError("NBT truncado")


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


def _invalidates_world(fn):
    """Tras editar el mundo, descarta el resumen y el tamaño cacheados."""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        finally:
            get_world_summary.cache_clear()
            get_world_size_gb.cache_clear()
    wrapper.__name__ = fn.__name__
    return wrapper


@_invalidates_world
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
                if not os.path.exists(bak):  # conservar la copia original entre borrados sucesivos
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


def _nbt_root_scan(data, wanted):
    """Lee solo las claves top-level pedidas (rápido, sin parsear secciones)."""
    r = _NBTReader(data)
    if r._u8() != 10:
        raise ValueError("Raíz NBT no es Compound")
    r._str()
    out = {}
    while True:
        tid = r._u8()
        if tid == 0:
            return out
        nm = r._str()
        if nm in wanted:
            if nm in out:
                r._skip(tid)
            else:
                out[nm] = r._payload(tid)
        else:
            r._skip(tid)


def _mca_chunk_root(fp, cx, cz):
    """Root parcial del chunk: InhabitedTime, xPos, zPos (None si ausente)."""
    try:
        with open(fp, "rb") as f:
            f.seek(4 * ((cz & 31) * 32 + (cx & 31)))
            entry = struct.unpack(">I", f.read(4))[0]
            loc = entry >> 8
            if not loc:
                return None
            f.seek(loc * 4096)
            length = struct.unpack(">I", f.read(4))[0]
            if length <= 1 or length > 64 * 1024 * 1024:
                return None
            ctype = f.read(1)[0]
            payload = f.read(length - 1)
        raw = (zlib.decompress(payload) if ctype == 2 else
               gzip.decompress(payload) if ctype == 1 else payload)
        return _nbt_root_scan(raw, {"InhabitedTime", "xPos", "zPos"})
    except Exception:
        return None


def level_spawn(server_dir, world_name):
    """Spawn del overworld desde level.dat (None si no se puede leer)."""
    try:
        with open(os.path.join(server_dir, world_name, "level.dat"), "rb") as f:
            data = _nbt_load(gzip.decompress(f.read()))
        d = data.get("Data", {}) if isinstance(data, dict) else {}
        return (int(d.get("SpawnX", 0)), int(d.get("SpawnZ", 0)))
    except Exception:
        return None


_PRUNE = {}
_prune_lock = threading.Lock()


def _prune_state(sid):
    with _prune_lock:
        st = _PRUNE.get(sid)
        if st is None:
            st = _PRUNE[sid] = {"running": False, "progress": "", "result": None, "error": None}
        return st


def _prune_run(sid, dim_id, max_min, radius_blocks):
    st = _prune_state(sid)
    try:
        data, err = mca_scan_prune(dim_id, max_min, radius_blocks,
                                   progress=lambda p: st.update(progress=p))
        with _prune_lock:
            st.update(result=data, error=err)
    except Exception as e:
        with _prune_lock:
            st.update(error=str(e)[:200])
    finally:
        with _prune_lock:
            st.update(running=False)


def mca_scan_prune(dim_id, max_min=30, radius_blocks=512, cap=20000, progress=None):
    """Candidatos a poda: InhabitedTime < max_min y fuera del radio de spawn."""
    srv = S()
    wdir, dim_paths = mca_world_dirs(srv)
    target_dir = dim_paths.get(dim_id)
    if not target_dir or not os.path.isdir(target_dir):
        return None, "Dimensión no encontrada"
    spawn = level_spawn(srv.server_dir, srv.world_name or "world") or (0, 0)
    ref = (spawn[0] // 16, spawn[1] // 16) if dim_id == "overworld" else (0, 0)
    keep = max(0, int(radius_blocks)) // 16
    limit_ticks = max(0, float(max_min)) * 1200
    cands, per_file = [], {}
    scanned = present = 0
    try:
        files = sorted(f for f in os.listdir(target_dir) if re.match(r"^r\.-?\d+\.-?\d+\.mca$", f))
    except OSError as e:
        return None, str(e)
    nfiles = len(files)
    for fn in files:
        m = re.match(r"^r\.(-?\d+)\.(-?\d+)\.mca$", fn)
        rx, rz = int(m.group(1)), int(m.group(2))
        fp = os.path.join(target_dir, fn)
        try:
            with open(fp, "rb") as f:
                head = f.read(4096)
            if len(head) < 4096:
                continue
        except OSError:
            continue
        scanned += 1
        if progress and scanned % 25 == 0:
            progress(f"{scanned}/{nfiles} archivos")
        for i in range(1024):
            if not (struct.unpack(">I", head[4 * i:4 * i + 4])[0] >> 8):
                continue
            present += 1
            lx, lz = i % 32, i // 32
            gx, gz = rx * 32 + lx, rz * 32 + lz
            if max(abs(gx - ref[0]), abs(gz - ref[1])) * 16 <= keep:
                continue
            root = _mca_chunk_root(fp, lx, lz)
            ticks = root.get("InhabitedTime", 0) if root else 0
            try:
                ticks = int(ticks)
            except (TypeError, ValueError):
                ticks = 0
            if ticks < limit_ticks and len(cands) < cap:
                cands.append({"file": fn, "cx": lx, "cz": lz,
                              "inhabited_min": round(ticks / 1200.0, 1)})
                per_file[fn] = per_file.get(fn, 0) + 1
    return {"candidates": cands, "truncated": len(cands) >= cap,
            "scanned_files": scanned, "present_chunks": present,
            "spawn": list(spawn), "radius_blocks": keep * 16,
            "per_file": per_file}, None


def _mca_copy_chunk(dst_fp, src_fp, idx):
    """Copia la entrada idx de src a dst (anexa sectores al final). False si ausente."""
    with open(src_fp, "rb") as f:
        f.seek(4 * idx)
        entry = struct.unpack(">I", f.read(4))[0]
        if not (entry >> 8):
            return False
        f.seek((entry >> 8) * 4096)
        length = struct.unpack(">I", f.read(4))[0]
        if length <= 1 or length > 64 * 1024 * 1024:
            return False
        blob = f.read(1 + length - 1)
    need = (4 + len(blob) + 4095) // 4096
    with open(dst_fp, "r+b") as f:
        f.seek(0, os.SEEK_END)
        sector = (f.tell() + 4095) // 4096
        f.seek(sector * 4096)
        f.write(struct.pack(">I", len(blob)))
        f.write(blob)
        pad = sector * 4096 + need * 4096 - f.tell()
        if pad:
            f.write(b"\x00" * pad)
        f.seek(4 * idx)
        f.write(struct.pack(">I", (sector << 8) | need))
        f.seek(4096 + 4 * idx)
        f.write(struct.pack(">I", int(time.time())))
    return True


@_invalidates_world
def mca_restore_chunks(dim_id, chunks):
    """Restaura chunks sueltos desde los .bak (requiere servidor detenido)."""
    srv = S()
    if _srv_running(srv) or find_running_mc_pid() is not None:
        return False, "Detén el servidor antes de restaurar chunks"
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
        return False, "Sin chunks válidos para restaurar"
    restored, missing = 0, []
    tile_dd = os.path.join(srv.data_dir, "tiles", dim_id)
    for fn, idxs in sorted(by_file.items()):
        fp = os.path.join(target_dir, fn)
        bak = fp + ".bak"
        if not os.path.isfile(bak):
            missing.append(fn)
            continue
        if not os.path.isfile(fp):
            try:
                shutil.copyfile(bak, fp)
            except Exception as e:
                return False, f"Error recreando {fn}: {e}"
        ok = True
        for i in sorted(idxs):
            try:
                if _mca_copy_chunk(fp, bak, i):
                    restored += 1
            except Exception:
                ok = False
        if ok:
            m = re.match(r"^r\.(-?\d+)\.(-?\d+)\.mca$", fn)
            if m:
                for tp in (os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.png"),
                           os.path.join(tile_dd, f"r.{m.group(1)}.{m.group(2)}.{TILE_PX}.png")):
                    try:
                        if os.path.isfile(tp):
                            os.remove(tp)
                    except Exception:
                        pass
    msg = f"Se restauraron {restored} chunks desde .bak en {dim_id.upper()}."
    if missing:
        msg += f" Sin .bak: {', '.join(missing)}."
    return (restored > 0), msg


def player_positions():
    """Posiciones desde playerdata/*.dat + usercache.json + online por query."""
    srv = S()
    try:
        with open(os.path.join(srv.server_dir, "usercache.json"), encoding="utf-8") as f:
            names = {e.get("uuid", "").replace("-", ""): e.get("name", "?")
                     for e in json.load(f) if isinstance(e, dict)}
    except Exception:
        names = {}
    online = set()
    try:
        if find_running_mc_pid() is not None:
            _, _, online = query_mc_players()[:3]
    except Exception:
        pass
    out = []
    pd = os.path.join(srv.server_dir, srv.world_name or "world", "playerdata")
    try:
        files = [f for f in os.listdir(pd) if f.endswith(".dat")]
    except OSError:
        files = []
    for fn in files:
        uuid = fn[:-4].replace("-", "")
        try:
            with open(os.path.join(pd, fn), "rb") as f:
                data = _nbt_load(gzip.decompress(f.read()))
            pos = data.get("Pos") if isinstance(data, dict) else None
            dim = data.get("Dimension") if isinstance(data, dict) else None
        except Exception:
            pos, dim = None, None
        known = uuid in names
        name = names.get(uuid, uuid[:8])
        entry = {"name": name, "known": known, "online": name in online, "seen": None,
                 "dim": dim if isinstance(dim, str) else "", "x": None, "y": None, "z": None}
        try:
            entry["seen"] = os.path.getmtime(os.path.join(pd, fn))
        except OSError:
            pass
        if isinstance(pos, list) and len(pos) == 3:
            try:
                entry.update(x=round(float(pos[0]), 1), y=round(float(pos[1]), 1), z=round(float(pos[2]), 1))
            except (TypeError, ValueError):
                pass
        out.append(entry)
    out.sort(key=lambda e: (not e["online"], not e["known"], e["name"].lower()))
    return out


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


@_ttl_cached(15)  # lee la cabecera de cada región: no repetirlo en cada vista
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
                        rx, rz = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
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


@_invalidates_world
def mca_delete_regions(dim_id, region_files):
    srv = S()
    if _srv_running(srv):
        return False, "Detén el servidor antes de borrar regiones (los archivos del mundo están en uso)"
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
        msg += f" Hubo {len(errors)} errores: {'; '.join(errors[:3])}"
    return (deleted > 0 or not errors), msg


@_invalidates_world
def mca_reset_dimension(dim_id):
    srv = S()
    if _srv_running(srv):
        return False, "Detén el servidor antes de reiniciar una dimensión"
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


__all__ = [n for n in dir() if not n.startswith("__")]
