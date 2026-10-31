import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backup  # noqa: E402
import panel  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_extra_keys_survive_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"panel_password_hash": "a$b", "bind": "0.0.0.0",
                           "servers": [{"id": "x"}], "mc_port": 25570}, f)
            old = panel.CONFIG_FILE
            panel.CONFIG_FILE = path
            try:
                cfg = panel.load_config()
            finally:
                panel.CONFIG_FILE = old
        self.assertEqual(cfg["panel_password_hash"], "a$b")
        self.assertEqual(cfg["bind"], "0.0.0.0")
        self.assertEqual(cfg["servers"], [{"id": "x"}])
        self.assertEqual(cfg["mc_port"], 25570)


class AuthTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = panel.hash_password("secreto123")
        self.assertTrue(panel.verify_password("secreto123", h))
        self.assertFalse(panel.verify_password("otra", h))
        self.assertFalse(panel.verify_password("x", "basura"))

    def test_login_rate_limit(self):
        ip = "203.0.113.9"
        panel.login_ok(ip)
        for _ in range(panel.LOGIN_MAX_FAILS):
            self.assertFalse(panel.login_blocked(ip))
            panel.login_failed(ip)
        self.assertTrue(panel.login_blocked(ip))
        panel.login_ok(ip)
        self.assertFalse(panel.login_blocked(ip))


class ZipTest(unittest.TestCase):
    def _zip(self, d, entries):
        zp = os.path.join(d, "a.zip")
        with zipfile.ZipFile(zp, "w") as z:
            for name, data in entries:
                z.writestr(name, data)
        return zp

    def test_zip_slip_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "dest")
            os.makedirs(dest)
            zp = self._zip(d, [("../evil.txt", "x"), ("/abs.txt", "x"), ("ok/a.txt", "hi")])
            panel.safe_extract_zip(zp, dest)
            self.assertFalse(os.path.exists(os.path.join(d, "evil.txt")))
            self.assertTrue(os.path.isfile(os.path.join(dest, "ok", "a.txt")))

    def test_zip_bomb_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            zp = self._zip(d, [("a.txt", "x")])
            old = panel.MAX_UNZIP_BYTES
            panel.MAX_UNZIP_BYTES = 0
            try:
                with self.assertRaises(ValueError):
                    panel.safe_extract_zip(zp, d)
            finally:
                panel.MAX_UNZIP_BYTES = old


class BackupTest(unittest.TestCase):
    def test_verify_detects_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.tar.gz")
            with tarfile.open(p, "w:gz") as t:
                info = tarfile.TarInfo("x")
                data = b"hello" * 1000
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            backup.verify_archive(p)
            with open(p, "rb") as f:
                raw = bytearray(f.read())
            raw[20] ^= 0xFF
            with open(p, "wb") as f:
                f.write(raw)
            with self.assertRaises(Exception):
                backup.verify_archive(p)

    def test_retention_keeps_monthly_and_recent(self):
        with tempfile.TemporaryDirectory() as d:
            names = ["pkhosting-20240110-040000.tar.gz",  # viejo, no último del mes
                     "pkhosting-20240128-040000.tar.gz",  # último de enero → se queda
                     "pkhosting-20240201-040000.tar.gz"]  # viejo, único de febrero → se queda
            old = time.time() - 400 * 86400
            for n in names:
                p = os.path.join(d, n)
                open(p, "wb").close()
                os.utime(p, (old, old))
            kept, deleted = backup.prune_backups(d, retention_days=7, keep_monthly=True)
            self.assertEqual(deleted, ["pkhosting-20240110-040000.tar.gz"])
            self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
