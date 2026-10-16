"""Regressões do Windows: BOM no settings.json, fim de linha LF e conexões SQLite fechadas."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "agentmeter.py")


def run(cfg, *args):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    env.pop("AGENTMETER_HOME", None)
    return subprocess.run([sys.executable, "-W", "always::ResourceWarning", SCRIPT, *args],
                          env=env, capture_output=True, check=False)


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg ação ")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_setup_aceita_settings_com_bom_e_grava_lf(self):
        path = os.path.join(self.tmp, "settings.json")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + '{"nota": "ação"}'.encode("utf-8"))
        r = run(self.tmp, "setup")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))["nota"], "ação")

    def test_report_fecha_a_conexao_sqlite(self):
        r = run(self.tmp, "report", "--todos")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b"unclosed database", r.stderr)

    def test_ingest_fecha_a_conexao_sqlite(self):
        r = run(self.tmp, "ingest")
        self.assertNotIn(b"unclosed database", r.stderr)


if __name__ == "__main__":
    unittest.main()
