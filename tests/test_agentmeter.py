"""Testes com transcripts sintéticos no formato do Claude Code.

Rode com: python3 -m unittest discover -s tests
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")


def assistant(rid, model, inp, cw, cr, out, ts, agent_id=None, sidechain=False, repeat=1):
    line = {
        "type": "assistant", "requestId": rid, "timestamp": ts, "sessionId": "S1",
        "cwd": "/work/demo", "isSidechain": sidechain,
        "message": {"id": "msg_" + rid, "model": model, "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": cw, "ephemeral_5m_input_tokens": 0}}},
    }
    if agent_id:
        line["agentId"] = agent_id
    # o Claude Code grava uma linha por bloco de conteúdo, repetindo o mesmo usage
    return "\n".join(json.dumps(line) for _ in range(repeat)) + "\n"


class AgentmeterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "cfg")
        os.environ.pop("AGENTMETER_HOME", None)
        sys.path.insert(0, SCRIPTS)
        import importlib
        import agentmeter
        self.am = importlib.reload(agentmeter)
        self.proj = os.path.join(self.am.PROJECTS_DIR, self.am.encode_project("/work/demo"))
        os.makedirs(os.path.join(self.proj, "S1", "subagents"))
        self.main = os.path.join(self.proj, "S1.jsonl")
        with open(self.main, "w") as f:
            f.write(json.dumps({"type": "user", "sessionId": "S1", "cwd": "/work/demo", "timestamp": "2026-10-16T09:00:00Z",
                                "message": {"role": "user", "content": "refatora o sync"}}) + "\n")
            f.write(assistant("r1", "claude-opus-5-5", 10, 1000, 0, 100, "2026-10-16T09:00:01Z", repeat=3))
            f.write(assistant("r2", "claude-opus-5-5", 5, 0, 1000, 50, "2026-10-16T09:00:05Z"))
        sub = os.path.join(self.proj, "S1", "subagents", "agent-a1")
        with open(sub + ".jsonl", "w") as f:
            f.write(assistant("r3", "claude-haiku-5-5", 2, 2000, 0, 20, "2026-10-16T09:00:02Z", agent_id="a1", sidechain=True))
        with open(sub + ".meta.json", "w") as f:
            json.dump({"agentType": "Explore", "description": "rotas"}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        sys.path.remove(SCRIPTS)

    def test_ingest_dedupes_and_splits_agents(self):
        con = self.am.db_connect()
        self.am.ingest_session(con, self.main, "S1")
        con.commit()
        rows = dict(con.execute("SELECT agent_type, SUM(input+cw5+cw1+cr+output) FROM usage GROUP BY agent_type"))
        self.assertEqual(rows["principal"], 1110 + 1055)
        self.assertEqual(rows["Explore"], 2022)
        # rodar de novo não duplica nada
        self.am.ingest_session(con, self.main, "S1")
        con.commit()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 3)
        title = con.execute("SELECT title FROM sessions").fetchone()[0]
        self.assertEqual(title, "refatora o sync")

    def test_incremental_offsets(self):
        con = self.am.db_connect()
        self.am.ingest_session(con, self.main, "S1")
        with open(self.main, "a") as f:
            f.write(assistant("r4", "claude-opus-5-5", 1, 0, 0, 1, "2026-10-16T09:01:00Z"))
            f.write('{"type": "assistant", "partial')  # linha ainda sendo escrita
        self.assertEqual(self.am.ingest_file(con, self.main, session_id="S1"), 1)

    def test_cost(self):
        # Opus 5.5: 1M de output = US$ 20
        self.assertAlmostEqual(self.am.cost_of("claude-opus-5-5", 0, 0, 0, 0, 1_000_000), 20.0)
        # Haiku 5.5 muda de faixa acima de 100k de prompt
        self.assertAlmostEqual(self.am.cost_of("claude-haiku-5-5", 200_000, 0, 0, 0, 0), 0.10)

    def test_statusline_and_panel(self):
        now = int(time.time() * 1000)
        panel = {"session_id": "S1", "columns": 80, "tasks": [
            {"id": "a1", "agentType": "Explore", "status": "running", "startTime": now - 5000,
             "contextWindowSize": 200000, "tokenCount": 150000, "description": "rotas"},
            {"id": "a2", "agentType": "Explore", "status": "running", "startTime": now - 3000,
             "contextWindowSize": 200000, "tokenCount": 10000, "description": "testes"},
            {"id": "a3", "agentType": "Plan", "status": "completed", "startTime": now - 9000, "tokenCount": 1},
        ]}
        lines = [json.loads(l) for l in self.am.cmd_subagent_line(panel).splitlines()]
        self.assertEqual(lines[2], {"id": "a3", "content": ""})
        self.assertIn("Explore#1", lines[0]["content"])
        self.assertIn("75%", lines[0]["content"])
        out = self.am.cmd_statusline({"session_id": "S1", "transcript_path": self.main,
                                      "model": {"display_name": "Opus 5.5"},
                                      "context_window": {"used_percentage": 41, "context_window_size": 1000000}})
        self.assertIn("ativos 2", out)
        self.assertIn("Explore#2", out)
        self.assertIn("projeto", out)
        # quando o SubagentStop chega, o subagent some da barra na hora
        self.am.cmd_hook({"hook_event_name": "SubagentStop", "session_id": "S1", "agent_id": "a1",
                          "transcript_path": self.main})
        out = self.am.cmd_statusline({"session_id": "S1", "transcript_path": self.main,
                                      "context_window": {"used_percentage": 41}})
        self.assertIn("ativos 1", out)

    def test_report(self):
        class A:
            cwd = "/work/demo"; todos = False; projeto = None; sessao = None
            desde = None; csv = False; top = 3
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.am.cmd_report(A())
        text = buf.getvalue()
        self.assertIn("principal", text)
        self.assertIn("Explore", text)
        self.assertIn("1 sessão", text)


if __name__ == "__main__":
    unittest.main()
