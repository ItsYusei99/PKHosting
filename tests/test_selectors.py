"""Contrato frontend: cada id que el JS busca con getElementById existe en el HTML
(o se genera en alguna plantilla JS), para que un rediseño no deje código huérfano."""
import glob
import os
import re
import unittest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SelectorContract(unittest.TestCase):
    def test_ids_usados_por_js_existen(self):
        html = _read(os.path.join(STATIC, "index.html"))
        js = {p: _read(p) for p in glob.glob(os.path.join(STATIC, "*.js"))}
        declared = set(re.findall(r'id=["\']([A-Za-z0-9_-]+)["\']', html))
        for src in js.values():
            declared |= set(re.findall(r'id=\\?["\']([A-Za-z0-9_-]+)\\?["\']', src))
            declared |= set(re.findall(r'\.id\s*=\s*[\'"]([A-Za-z0-9_-]+)[\'"]', src))
        missing = {}
        for path, src in js.items():
            for m in re.finditer(r'getElementById\(\s*[\'"]([A-Za-z0-9_-]+)[\'"]\s*\)', src):
                if m.group(1) not in declared:
                    missing.setdefault(m.group(1), os.path.basename(path))
        self.assertEqual(missing, {}, "ids que el JS busca y no existen en el HTML")

    def test_pestanas_tienen_contenido(self):
        html = _read(os.path.join(STATIC, "index.html"))
        tabs = re.findall(r'data-tab="([a-z]+)"', html)
        self.assertEqual(len(tabs), 13)
        for t in tabs:
            self.assertIn('id="tab-%s"' % t, html)


if __name__ == "__main__":
    unittest.main()
