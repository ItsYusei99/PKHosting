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
            old = panel.core.CONFIG_FILE
            panel.core.CONFIG_FILE = path
            try:
                cfg = panel.load_config()
            finally:
                panel.core.CONFIG_FILE = old
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
        panel._login_ok(ip)
        for _ in range(panel.LOGIN_MAX_FAILS):
            self.assertEqual(panel._login_wait(ip), 0)
            panel._login_fail_hit(ip)
        self.assertGreater(panel._login_wait(ip), 0)
        panel._login_ok(ip)
        self.assertEqual(panel._login_wait(ip), 0)


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
            old = panel.tools.MAX_UNZIP_BYTES
            panel.tools.MAX_UNZIP_BYTES = 0
            try:
                with self.assertRaises(ValueError):
                    panel.safe_extract_zip(zp, d)
            finally:
                panel.tools.MAX_UNZIP_BYTES = old


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
            kept, deleted = backup.prune_backups(d, retention_days=7, keep_monthly=True, min_keep=0)
            self.assertEqual(deleted, ["pkhosting-20240110-040000.tar.gz"])
            self.assertEqual(len(kept), 2)

    def test_retention_min_keep_protects_recent(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ["pkhosting-20240101-040000.tar.gz", "pkhosting-20240102-040000.tar.gz"]:
                open(os.path.join(d, n), "wb").close()
            kept, deleted = backup.prune_backups(d, retention_days=1, keep_monthly=False, min_keep=2)
            self.assertEqual(deleted, [])
            self.assertEqual(len(kept), 2)

    def _make_world(self, root):
        w = os.path.join(root, "world", "region")
        os.makedirs(w)
        with open(os.path.join(w, "r.0.0.mca"), "wb") as f:
            f.write(b"A" * 100)
        with open(os.path.join(root, "server.properties"), "w") as f:
            f.write("motd=x\n")

    def test_backup_restore_roundtrip_replaces_world(self):
        with tempfile.TemporaryDirectory() as d:
            srv, bdir = os.path.join(d, "srv"), os.path.join(d, "bk")
            os.makedirs(srv)
            self._make_world(srv)
            ok, msg = backup.run_backup(srv, "world", bdir)
            self.assertTrue(ok, msg)
            self.assertFalse([n for n in os.listdir(bdir) if n.endswith(".partial")])
            name = next(n for n in os.listdir(bdir) if n.startswith("pkhosting-") and n.endswith(".tar.gz"))
            # región creada DESPUÉS del backup: no debe sobrevivir a la restauración
            extra = os.path.join(srv, "world", "region", "r.9.9.mca")
            with open(extra, "wb") as f:
                f.write(b"NEW")
            ok, msg = backup.restore_backup(srv, bdir, name, pre_backup=True)
            self.assertTrue(ok, msg)
            self.assertFalse(os.path.exists(extra))
            self.assertTrue(os.path.isfile(os.path.join(srv, "world", "region", "r.0.0.mca")))
            self.assertFalse([n for n in os.listdir(srv) if "pre-restore" in n])

    def test_backup_lock_blocks_concurrent_run(self):
        with tempfile.TemporaryDirectory() as d:
            srv, bdir = os.path.join(d, "srv"), os.path.join(d, "bk")
            os.makedirs(srv)
            self._make_world(srv)
            lk = backup._acquire_lock(bdir)
            try:
                ok, msg = backup.run_backup(srv, "world", bdir)
            finally:
                lk.close()
            self.assertFalse(ok)
            self.assertIn("en curso", msg)

    def test_backup_aborts_when_freeze_fails_on_running_server(self):
        with tempfile.TemporaryDirectory() as d:
            srv, bdir = os.path.join(d, "srv"), os.path.join(d, "bk")
            os.makedirs(srv)
            self._make_world(srv)
            ok, msg = backup.run_backup(srv, "world", bdir, send_fn=lambda c: (False, "sin rcon"),
                                        running_fn=lambda: True)
            self.assertFalse(ok)
            self.assertIn("congelar", msg)
            ok, _ = backup.run_backup(srv, "world", bdir, send_fn=lambda c: (False, "sin rcon"),
                                      running_fn=lambda: False)
            self.assertTrue(ok)


class ServerStateTest(unittest.TestCase):
    def test_saving_backup_config_keeps_runtime_state(self):
        srv = panel.Server({"id": "t-state", "server_dir": tempfile.gettempdir(),
                            "data_dir": tempfile.mkdtemp()})
        lock, state = srv.backup_lock, srv.backup_state
        state.update(running=True, job="run")
        srv._apply_file_overrides()  # lo que hace backup_cfg_set al guardar
        self.assertIs(srv.backup_lock, lock)
        self.assertIs(srv.backup_state, state)
        self.assertTrue(srv.backup_state["running"])


class HardeningTest(unittest.TestCase):
    def test_invalid_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            for bad in ("{no es json", "[1, 2]", json.dumps({"mc_port": "abc"})):
                with open(path, "w") as f:
                    f.write(bad)
                old = panel.core.CONFIG_FILE
                panel.core.CONFIG_FILE = path
                try:
                    with self.assertRaises(SystemExit):
                        panel.load_config()
                finally:
                    panel.core.CONFIG_FILE = old

    def test_content_disposition_strips_header_injection(self):
        hdr = panel._content_disposition('a"b\r\nSet-Cookie: x=1.txt')
        self.assertNotIn("\r", hdr)
        self.assertNotIn("\n", hdr)
        self.assertEqual(hdr.count('"'), 2)

    def test_totp_code_cannot_be_reused(self):
        secret = "JBSWY3DPEHPK3PXP"
        now = int(time.time() // 30)
        code = panel.totp_code(secret, now)
        panel._totp_last["ctr"] = 0
        self.assertTrue(panel.totp_verify(secret, code, once=True))
        self.assertFalse(panel.totp_verify(secret, code, once=True))
        self.assertTrue(panel.totp_verify(secret, code))  # sin once sigue valiendo (setup/desactivar)

    def test_download_filter_and_player_name(self):
        self.assertFalse(panel.get_player_details(panel.S(), "a b; stop")["ok"])

    def test_sessions_dropped_except_current(self):
        a, b = panel.new_session(), panel.new_session()
        panel.drop_other_sessions(a)
        self.assertTrue(panel.valid_session(a))
        self.assertFalse(panel.valid_session(b))


if __name__ == "__main__":
    unittest.main()
