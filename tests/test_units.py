"""Pruebas unitarias de funciones críticas: TOTP, rutas seguras, tareas programadas, RCON, historial."""
import datetime
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import panel  # noqa: E402


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _server(tmp, **extra):
    cfg = {"id": "t-" + os.path.basename(tmp), "server_dir": tmp, "data_dir": os.path.join(tmp, "data")}
    cfg.update(extra)
    return panel.Server(cfg)


class UsingServer(unittest.TestCase):
    """Fija el servidor activo del hilo (S()) durante la prueba."""

    def use(self, srv):
        panel._ctx.srv = srv
        self.addCleanup(setattr, panel._ctx, "srv", None)


class TotpTest(unittest.TestCase):
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ASCII "12345678901234567890" en base32 (RFC 6238)

    def test_rfc6238_vectors(self):
        # tiempos 59 s y 1111111109 s → contadores 1 y 37037036 (SHA-1, 6 dígitos)
        self.assertEqual(panel.totp_code(self.SECRET, 1), "287082")
        self.assertEqual(panel.totp_code(self.SECRET, 37037036), "081804")

    def test_bad_secret_returns_none(self):
        self.assertIsNone(panel.totp_code("no es base32 !!", 1))

    def test_verify_accepts_adjacent_window_only(self):
        now = int(time.time() // 30)
        self.assertTrue(panel.totp_verify(self.SECRET, panel.totp_code(self.SECRET, now - 1)))
        self.assertFalse(panel.totp_verify(self.SECRET, panel.totp_code(self.SECRET, now - 5)))
        self.assertFalse(panel.totp_verify(self.SECRET, "12345"))      # longitud incorrecta
        self.assertFalse(panel.totp_verify(self.SECRET, "abcdef"))     # no numérico


class SafePathTest(UsingServer):
    def test_blocks_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(os.path.join(d, "srv"))
            os.makedirs(os.path.join(root, "mods"))
            outside = os.path.join(d, "secreto.txt")
            _write(outside, "x")
            os.symlink(outside, os.path.join(root, "enlace"))
            os.symlink(d, os.path.join(root, "carpeta-fuera"))
            self.use(_server(root))
            self.assertEqual(panel.safe_fs_path(""), root)
            self.assertEqual(panel.safe_fs_path("mods"), os.path.join(root, "mods"))
            self.assertIsNone(panel.safe_fs_path("../secreto.txt"))
            self.assertIsNone(panel.safe_fs_path("mods/../../secreto.txt"))
            self.assertIsNone(panel.safe_fs_path("enlace"))              # symlink a un archivo de fuera
            self.assertIsNone(panel.safe_fs_path("carpeta-fuera/secreto.txt"))
            # ruta absoluta: se interpreta relativa a la raíz del servidor, nunca al sistema
            self.assertEqual(panel.safe_fs_path("/etc/passwd"), os.path.join(root, "etc", "passwd"))

    def test_prefix_lookalike_directory_is_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(os.path.join(d, "srv"))
            os.makedirs(root)
            os.makedirs(os.path.join(d, "srv-otro"))
            self.use(_server(root))
            self.assertIsNone(panel.safe_fs_path("../srv-otro"))


class TaskDueTest(unittest.TestCase):
    def test_disabled_never_due(self):
        self.assertFalse(panel._task_due({"enabled": False, "mode": "interval", "every_min": 5}, time.time()))

    def test_interval_respects_period_and_minimum(self):
        now = time.time()
        t = {"enabled": True, "mode": "interval", "every_min": 60, "last_run": now - 59 * 60}
        self.assertFalse(panel._task_due(t, now))
        t["last_run"] = now - 61 * 60
        self.assertTrue(panel._task_due(t, now))
        # el mínimo es de 5 min aunque se pida 1
        t2 = {"enabled": True, "mode": "interval", "every_min": 1, "last_run": now - 120}
        self.assertFalse(panel._task_due(t2, now))

    def test_daily_runs_once_after_the_hour(self):
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        target = today + 4 * 3600
        t = {"enabled": True, "mode": "daily", "at": "04:00", "last_run": 0}
        self.assertFalse(panel._task_due(t, target - 60))      # aún no es la hora
        self.assertTrue(panel._task_due(t, target + 60))       # pasó la hora y no corrió hoy
        t["last_run"] = target + 30
        self.assertFalse(panel._task_due(t, target + 3600))    # ya corrió hoy

    def test_invalid_time_is_not_due(self):
        self.assertFalse(panel._task_due({"enabled": True, "mode": "daily", "at": "mañana"}, time.time()))


def _fake_rcon(password, response, results):
    """Servidor RCON mínimo: autentica y responde a un comando."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def rd(conn):
        hdr = conn.recv(4)
        (ln,) = struct.unpack("<i", hdr)
        data = b""
        while len(data) < ln:
            data += conn.recv(ln - len(data))
        rid, rtype = struct.unpack("<ii", data[:8])
        return rid, rtype, data[8:-2].decode()

    def pkt(rid, rtype, text):
        body = struct.pack("<ii", rid, rtype) + text.encode() + b"\x00\x00"
        return struct.pack("<i", len(body)) + body

    def run():
        conn, _ = srv.accept()
        with conn:
            rid, rtype, pw = rd(conn)
            results.append(("auth", rtype, pw))
            if pw != password:
                conn.sendall(pkt(-1, 2, ""))
                return
            conn.sendall(pkt(rid, 2, ""))
            rid, rtype, cmd = rd(conn)
            results.append(("cmd", rtype, cmd))
            conn.sendall(pkt(rid, 0, response))
    threading.Thread(target=run, daemon=True).start()
    return srv


class RconTest(UsingServer):
    def _setup(self, tmp, password_on_server, client_password, response="There are 0 of 20 players"):
        pwfile = os.path.join(tmp, "rcon-password")
        _write(pwfile, client_password + "\n")
        results = []
        fake = _fake_rcon(password_on_server, response, results)
        self.addCleanup(fake.close)
        self.use(_server(tmp, rcon_port=fake.getsockname()[1], rcon_pass_file=pwfile))
        return results

    def test_command_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            results = self._setup(d, "secreto", "secreto")
            ok, resp = panel.rcon_send("list")
            self.assertTrue(ok, resp)
            self.assertEqual(resp, "There are 0 of 20 players")
            self.assertEqual(results, [("auth", 3, "secreto"), ("cmd", 2, "list")])  # tipos 3 (auth) y 2 (comando)

    def test_wrong_password_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d, "secreto", "otra")
            ok, resp = panel.rcon_send("list")
            self.assertFalse(ok)
            self.assertIn("auth", resp)

    def test_missing_password(self):
        with tempfile.TemporaryDirectory() as d:
            self.use(_server(d, rcon_pass_file=os.path.join(d, "no-existe")))
            ok, resp = panel.rcon_send("list")
            self.assertFalse(ok)


class ResetPasswordCliTest(unittest.TestCase):
    def test_reset_password_writes_new_hash_and_disables_2fa(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            cfg_dir = os.path.join(home, ".config", "pkhosting")
            os.makedirs(cfg_dir)
            cfg_file = os.path.join(cfg_dir, "config.json")
            _write(cfg_file, json.dumps({"panel_password_hash": panel.hash_password("vieja-clave-1"),
                                         "totp_enabled": True, "totp_secret": "JBSWY3DPEHPK3PXP", "mc_port": 25570}))
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            r = subprocess.run([sys.executable, os.path.join(root, "panel.py"), "--reset-password"],
                               input="nueva-clave-99\nnueva-clave-99\n", text=True, capture_output=True,
                               env=dict(os.environ, HOME=home), cwd=home, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(cfg_file) as f:
                cfg = json.load(f)
            self.assertTrue(panel.verify_password("nueva-clave-99", cfg["panel_password_hash"]))
            self.assertFalse(cfg["totp_enabled"])
            self.assertEqual(cfg["mc_port"], 25570)          # el resto de la config se conserva
            self.assertEqual(oct(os.stat(cfg_file).st_mode & 0o777), "0o600")


class HistoryTest(unittest.TestCase):
    def test_downsample_keeps_bounded_size(self):
        items = list(range(panel.HIST_MAX_POINTS * 3 + 7))
        out = panel._downsample(items)
        self.assertLessEqual(len(out), panel.HIST_MAX_POINTS)
        self.assertEqual(out[0], 0)
        small = list(range(10))
        self.assertEqual(panel._downsample(small), small)


if __name__ == "__main__":
    unittest.main()
