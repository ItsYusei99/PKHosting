"""Pruebas de humo HTTP: arranca panel.py de verdad (HOME temporal, puerto libre) y lo ejercita."""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import panel  # noqa: E402  (solo para hash_password)

PASSWORD = "clave-de-prueba-123"
XSS_NAME = '<img src=x onerror=alert(1)>Srv'


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PanelProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = cls.tmp.name
        cls.port = _free_port()
        srv_dir = os.path.join(home, "srv")
        os.makedirs(os.path.join(srv_dir, "world", "region"))
        with open(os.path.join(srv_dir, "server.properties"), "w") as f:
            f.write("motd=test\n")
        cfg_dir = os.path.join(home, ".config", "pkhosting")
        os.makedirs(cfg_dir)
        cfg = {"server_name": XSS_NAME, "server_dir": srv_dir, "world_name": "world",
               "panel_port": cls.port, "mc_port": _free_port(), "rcon_port": _free_port(),
               "panel_password_hash": panel.hash_password(PASSWORD)}
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump(cfg, f)
        env = dict(os.environ, HOME=home)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "panel.py")], cwd=home, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.25)
        else:
            cls.proc.kill()
            raise RuntimeError("el panel no arrancó")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    # -- utilidades --
    def _opener(self, login=False):
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        if login:
            r = self._req(op, "/api/login", {"password": PASSWORD})
            self.assertTrue(json.loads(r[1])["ok"])
        return op

    def _req(self, op, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with op.open(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    # -- pruebas --
    def test_requires_login(self):
        op = self._opener()
        code, body, _ = self._req(op, "/api/stats")
        self.assertEqual(code, 401)
        code, body, _ = self._req(op, "/")
        self.assertEqual(code, 200)
        self.assertIn('id="step1"', body)  # página de login, no el panel

    def test_wrong_password_rejected(self):
        code, body, _ = self._req(self._opener(), "/api/login", {"password": "incorrecta"})
        self.assertEqual(code, 401)

    def test_stats_after_login(self):
        code, body, _ = self._req(self._opener(login=True), "/api/stats")
        self.assertEqual(code, 200)
        self.assertIn("status", json.loads(body))

    def test_server_name_is_escaped_in_html(self):
        code, body, hdr = self._req(self._opener(login=True), "/")
        self.assertEqual(code, 200)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img src=x", body)
        csp = hdr.get("Content-Security-Policy", "")
        self.assertNotIn("googleapis", csp)

    def test_cross_origin_post_rejected(self):
        op = self._opener(login=True)
        code, _, _ = self._req(op, "/api/cmd", {"cmd": "list"}, {"Origin": "http://evil.example"})
        self.assertEqual(code, 403)

    def _raw_post(self, op, content_length, path="/api/cmd"):
        jar = [h for h in op.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)][0].cookiejar
        tok = next(c.value for c in jar if c.name == "pksess")
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(f"POST {path} HTTP/1.0\r\nHost: x\r\nCookie: pksess={tok}\r\n"
                  f"Content-Length: {content_length}\r\n\r\n".encode())
        data = s.recv(200)
        s.close()
        return data

    def test_big_body_rejected_before_reading(self):
        # se declara 3 MB y no se envía nada: el rechazo debe llegar sin esperar el cuerpo
        self.assertIn(b"413", self._raw_post(self._opener(login=True), 3 * 1024 * 1024))

    def test_unauthenticated_post_is_401_without_reading_body(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /api/cmd HTTP/1.0\r\nHost: x\r\nContent-Length: 99999999\r\n\r\n")
        self.assertIn(b"401", s.recv(200))
        s.close()

    def test_invalid_content_length(self):
        self.assertIn(b"400", self._raw_post(self._opener(login=True), "abc"))

    def test_browse_limited_to_home(self):
        code, body, _ = self._req(self._opener(login=True), "/api/browse?path=/etc")
        self.assertEqual(code, 403)

    def test_player_name_validated(self):
        code, body, _ = self._req(self._opener(login=True), "/api/player-info?name=a%20b%3Bstop")
        self.assertFalse(json.loads(body)["ok"])

    def test_static_assets_and_login_form(self):
        op = self._opener(login=True)
        for path in ("/static/app.js", "/static/app.css"):
            code, _, _ = self._req(op, path)
            self.assertEqual(code, 200, path)

    def test_download_header_has_no_injection(self):
        op = self._opener(login=True)
        code, _, hdr = self._req(op, "/api/download?path=server.properties")
        self.assertEqual(code, 200)
        self.assertEqual(hdr["Content-Disposition"], 'attachment; filename="server.properties"')

    def test_world_edit_blocked_paths(self):
        op = self._opener(login=True)
        code, body, _ = self._req(op, "/api/world/mca", {"action": "reset_dimension", "dimension": "overworld"})
        self.assertFalse(json.loads(body).get("ok"))


if __name__ == "__main__":
    unittest.main()
