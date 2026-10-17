#!/usr/bin/env python3
"""agentmeter: tokens por sessão, projeto e subagent no Claude Code.

Subcomandos:
  statusline      barra de status principal (lê JSON do stdin)
  subagent-line   linhas do painel de subagents (lê JSON do stdin)
  hook            entrada dos hooks do plug-in (lê JSON do stdin)
  report          relatório do projeto (/agentmeter:tokens)
  setup           instala a status line no ~/.claude/settings.json
  ingest          importa todos os transcripts do projeto para o ledger

Só usa a biblioteca padrão do Python 3.8+.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import time

VERSION = "0.1.2"

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
APP_DIR = os.environ.get("AGENTMETER_HOME") or os.path.join(CLAUDE_DIR, "agentmeter")
DB_PATH = os.path.join(APP_DIR, "ledger.db")
STATE_DIR = os.path.join(APP_DIR, "state")
INSTALLED_SCRIPT = os.path.join(APP_DIR, "agentmeter.py")
PRICING_OVERRIDE = os.path.join(APP_DIR, "pricing.json")

MAIN = "principal"

# ---------------------------------------------------------------- preços
# USD por milhão de tokens: input, cache write 5m, cache write 1h, cache read, output.
# Fonte: platform.claude.com/docs/en/about-claude/pricing (out/2026).
# Ordem importa: o primeiro padrão que casar com o id do modelo vence.
PRICES = [
    ("fable-5-1", (10, 12.5, 20, 0.25, 50)),
    ("mythos-5-1", (10, 12.5, 20, 0.25, 50)),
    ("fable-5", (10, 12.5, 20, 1, 50)),
    ("mythos-5", (10, 12.5, 20, 1, 50)),
    ("opus-5-5", (4, 5, 8, 0.20, 20)),
    ("opus-5", (5, 6.25, 10, 0.50, 25)),
    ("opus-4-8", (5, 6.25, 10, 0.50, 25)),
    ("opus-4-7", (5, 6.25, 10, 0.50, 25)),
    ("opus-4-6", (5, 6.25, 10, 0.50, 25)),
    ("opus-4-5", (5, 6.25, 10, 0.50, 25)),
    ("opus-4-1", (15, 18.75, 30, 1.50, 75)),
    ("opus-4", (15, 18.75, 30, 1.50, 75)),
    ("sonnet-5-5", (2, 2.5, 4, 0.10, 10)),
    ("sonnet-5", (2, 2.5, 4, 0.20, 10)),
    ("sonnet-4", (3, 3.75, 6, 0.30, 15)),
    ("haiku-5-5", (0.10, 0.125, 0.20, 0.01, 0.50)),  # prompt <= 100k
    ("haiku-4-5", (1, 1.25, 2, 0.10, 5)),
    ("haiku-3-5", (0.80, 1, 1.60, 0.08, 4)),
    # fallbacks por família
    ("fable", (10, 12.5, 20, 0.25, 50)),
    ("mythos", (10, 12.5, 20, 0.25, 50)),
    ("opus", (4, 5, 8, 0.20, 20)),
    ("sonnet", (2, 2.5, 4, 0.10, 10)),
    ("haiku", (0.10, 0.125, 0.20, 0.01, 0.50)),
]
HAIKU_55_LONG = (0.50, 0.625, 1, 0.05, 2.50)  # prompt > 100k


def _load_prices():
    prices = list(PRICES)
    try:
        with open(PRICING_OVERRIDE, encoding="utf-8") as f:
            extra = json.load(f)
        prices = [(k, tuple(v)) for k, v in extra.items()] + prices
    except Exception:
        pass
    return prices


_PRICES_CACHE = None


def price_for(model, prompt_tokens):
    global _PRICES_CACHE
    if _PRICES_CACHE is None:
        _PRICES_CACHE = _load_prices()
    m = (model or "").lower()
    if "haiku-5-5" in m and prompt_tokens > 100_000:
        return HAIKU_55_LONG
    for key, p in _PRICES_CACHE:
        if key in m:
            return p
    return None


def cost_of(model, inp, cw5, cw1, cr, out):
    p = price_for(model, inp + cw5 + cw1 + cr)
    if not p:
        return 0.0
    return (inp * p[0] + cw5 * p[1] + cw1 * p[2] + cr * p[3] + out * p[4]) / 1e6


# ---------------------------------------------------------------- ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
  request_id TEXT PRIMARY KEY,
  project TEXT, session_id TEXT, agent_id TEXT, agent_type TEXT, model TEXT, ts TEXT,
  input INTEGER, cw5 INTEGER, cw1 INTEGER, cr INTEGER, output INTEGER, cost REAL
);
CREATE INDEX IF NOT EXISTS usage_project ON usage(project, ts);
CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT, project TEXT, cwd TEXT, title TEXT, first_ts TEXT, last_ts TEXT,
  PRIMARY KEY (project, session_id)
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY, session_id TEXT, project TEXT, agent_type TEXT, description TEXT
);
"""


def db_connect():
    os.makedirs(APP_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=1.5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def encode_project(cwd):
    """Mesma codificação que o Claude Code usa em ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def project_of_transcript(path):
    """Diretório de projeto (nome codificado) de um transcript principal ou de subagent."""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.basename(d) == "subagents":
        d = os.path.dirname(os.path.dirname(d))
    return os.path.basename(d)


def _user_prompt_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if not parts or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        text = " ".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None
    return " ".join(text.split())[:60]


def ingest_file(con, path, session_id=None, agent_id=None, agent_type=None, project=None):
    """Lê só o trecho novo do arquivo (offset salvo) e grava o uso por requisição."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    row = con.execute("SELECT offset FROM files WHERE path=?", (path,)).fetchone()
    offset = row[0] if row else 0
    if size < offset:  # arquivo reescrito: relê tudo (dedupe por request_id)
        offset = 0
    if size == offset:
        return 0
    project = project or project_of_transcript(path)
    n = 0
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return 0
    chunk = chunk[: end + 1]
    new_offset = offset + len(chunk)
    sess_rows = {}
    for raw in chunk.splitlines():
        try:
            d = json.loads(raw)
        except Exception:
            continue
        sid = d.get("sessionId") or session_id
        ts = d.get("timestamp")
        if sid and ts and agent_id is None:
            s = sess_rows.setdefault(sid, {"cwd": d.get("cwd"), "first": ts, "last": ts, "title": None})
            s["last"] = ts
            if d.get("type") == "user" and not s["title"] and not d.get("isMeta") and not d.get("isSidechain"):
                s["title"] = _user_prompt_text(d.get("message") or {})
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        if not u:
            continue
        rid = d.get("requestId") or msg.get("id") or d.get("uuid")
        if not rid:
            continue
        a_id, a_type = agent_id, agent_type
        if a_id is None and d.get("isSidechain"):
            a_id = d.get("agentId") or "sidechain"
            a_type = a_type or "subagent"
        inp = int(u.get("input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        cc = u.get("cache_creation") or {}
        cw1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        cw5 = max(cw - cw1, 0)
        out = int(u.get("output_tokens") or 0)
        model = msg.get("model") or ""
        if model == "<synthetic>":
            continue
        con.execute(
            "INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, project, sid, a_id or "", a_type or MAIN, model, ts,
             inp, cw5, cw1, cr, out, cost_of(model, inp, cw5, cw1, cr, out)),
        )
        n += 1
    for sid, s in sess_rows.items():
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?) ON CONFLICT(project, session_id) DO UPDATE SET "
            "last_ts=MAX(last_ts, excluded.last_ts), cwd=COALESCE(sessions.cwd, excluded.cwd), "
            "title=COALESCE(sessions.title, excluded.title), first_ts=MIN(first_ts, excluded.first_ts)",
            (sid, project, s["cwd"], s["title"], s["first"], s["last"]),
        )
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, new_offset, size))
    return n


def _agent_meta(path):
    meta = path[: -len(".jsonl")] + ".meta.json"
    try:
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
        return m.get("agentType") or "subagent", m.get("description") or ""
    except Exception:
        return "subagent", ""


def ingest_session(con, transcript_path, session_id=None):
    """Importa o transcript principal e os transcripts dos subagents dessa sessão."""
    if not transcript_path:
        return
    project = project_of_transcript(transcript_path)
    sid = session_id or os.path.basename(transcript_path)[: -len(".jsonl")]
    ingest_file(con, transcript_path, session_id=sid, project=project)
    sub_dir = os.path.join(os.path.dirname(transcript_path), sid, "subagents")
    if os.path.isdir(sub_dir):
        for name in os.listdir(sub_dir):
            if not (name.startswith("agent-") and name.endswith(".jsonl")):
                continue
            aid = name[len("agent-"): -len(".jsonl")]
            path = os.path.join(sub_dir, name)
            atype, desc = _agent_meta(path)
            con.execute("INSERT OR IGNORE INTO agents VALUES (?,?,?,?,?)", (aid, sid, project, atype, desc))
            ingest_file(con, path, session_id=sid, agent_id=aid, agent_type=atype, project=project)


def ingest_project(con, project):
    pdir = os.path.join(PROJECTS_DIR, project)
    if not os.path.isdir(pdir):
        return
    for name in sorted(os.listdir(pdir)):
        if name.endswith(".jsonl"):
            ingest_session(con, os.path.join(pdir, name), name[: -len(".jsonl")])
    con.commit()


# ---------------------------------------------------------------- estado dos subagents ativos

def _state_path(session_id, kind="hooks"):
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "x")
    return os.path.join(STATE_DIR, "%s.%s.json" % (sid, kind))


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_state(session_id):
    """Estado dos hooks (agents/done) + último snapshot do painel, em arquivos separados."""
    st = _load_json(_state_path(session_id), {"agents": {}, "done": {}})
    st["panel"] = _load_json(_state_path(session_id, "panel"), None)
    return st


def save_state(session_id, state):
    _save_json(_state_path(session_id), {"agents": state.get("agents", {}), "done": state.get("done", {})})


def _last_context(path):
    """Tokens no contexto segundo a última resposta registrada no transcript (lê só o final)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().splitlines()
    except OSError:
        return None, None
    for raw in reversed(tail):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        if u:
            ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            return ctx, msg.get("model")
    return None, None


def window_for(model, fallback=200_000):
    m = (model or "").lower()
    if "[1m]" in m:
        return 1_000_000
    return fallback


def active_subagents(session_id, transcript_path, main_window):
    """Lista de subagents rodando: prefere o snapshot do painel (mais preciso), senão os hooks."""
    st = load_state(session_id)
    now = time.time()
    panel = st.get("panel")
    rows = []
    done = st.get("done", {})
    if panel and now - panel.get("at", 0) < 5:
        for t in panel.get("tasks", []):
            if t.get("status") != "running" or t.get("id") in done:
                continue
            win = t.get("contextWindowSize") or main_window
            rows.append({
                "id": t.get("id"), "type": t.get("agentType") or "subagent",
                "tokens": t.get("tokenCount") or 0, "window": win,
                "start": (t.get("startTime") or now * 1000) / 1000.0,
            })
        return rows
    base = os.path.join(os.path.dirname(transcript_path or ""), session_id or "", "subagents")
    for aid, a in st.get("agents", {}).items():
        if now - a.get("start", now) > 6 * 3600:
            continue
        ctx, model = _last_context(os.path.join(base, "agent-%s.jsonl" % aid))
        rows.append({
            "id": aid, "type": a.get("type") or "subagent", "tokens": ctx or 0,
            "window": window_for(model, main_window), "start": a.get("start", now),
        })
    return rows


def number_instances(rows):
    rows.sort(key=lambda r: r["start"])
    counts = {}
    for r in rows:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    seen = {}
    for r in rows:
        if counts[r["type"]] > 1:
            seen[r["type"]] = seen.get(r["type"], 0) + 1
            r["label"] = "%s#%d" % (r["type"], seen[r["type"]])
        else:
            r["label"] = r["type"]
    return rows


# ---------------------------------------------------------------- formatação

def human(n):
    n = float(n or 0)
    if n >= 1e9:
        return ("%.2fB" % (n / 1e9)).replace(".00B", "B")
    if n >= 1e6:
        return ("%.2fM" % (n / 1e6)).replace(".00M", "M")
    if n >= 1e3:
        return "%.1fk" % (n / 1e3) if n < 1e4 else "%dk" % round(n / 1e3)
    return "%d" % n


def brl_usd(v):
    s = "%.2f" % (v or 0)
    inteiro, dec = s.split(".")
    inteiro = "{:,}".format(int(inteiro)).replace(",", ".")
    return "US$ %s,%s" % (inteiro, dec)


def mmss(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
PALETTE = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m"}
AGENT_COLORS = ["\033[36m", "\033[35m", "\033[34m", "\033[95m", "\033[96m", "\033[94m"]
MAIN_COLOR = "\033[38;5;173m"


def level_color(pct):
    if pct >= 85:
        return PALETTE["red"]
    if pct >= 60:
        return PALETTE["yellow"]
    return PALETTE["green"]


def agent_color(agent_type):
    if agent_type == MAIN:
        return MAIN_COLOR
    return AGENT_COLORS[sum(map(ord, agent_type)) % len(AGENT_COLORS)]


def bar(pct, width=10, color=True):
    pct = max(0.0, min(100.0, float(pct or 0)))
    full = int(round(pct / 100 * width))
    s = "█" * full + "░" * (width - full)
    if not color:
        return s
    return level_color(pct) + s[:full] + DIM + s[full:] + RESET


def visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def fit(s, cols):
    """Corta a linha para caber em cols colunas sem quebrar códigos ANSI."""
    if cols <= 0 or visible_len(s) <= cols:
        return s
    out, n, i = [], 0, 0
    while i < len(s) and n < cols - 1:
        m = re.match(r"\033\[[0-9;]*m", s[i:])
        if m:
            out.append(m.group(0))
            i += len(m.group(0))
            continue
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out) + "…" + RESET


# ---------------------------------------------------------------- status line principal

def totals(con, where, args):
    r = con.execute(
        "SELECT COALESCE(SUM(input+cw5+cw1+cr+output),0), COALESCE(SUM(cost),0), "
        "COALESCE(SUM(CASE WHEN agent_id!='' THEN input+cw5+cw1+cr+output ELSE 0 END),0) "
        "FROM usage WHERE " + where, args).fetchone()
    return r[0], r[1], r[2]


def cmd_statusline(data):
    session_id = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    cols = int(os.environ.get("COLUMNS") or 0) or 120
    project = project_of_transcript(transcript) if transcript else encode_project(data.get("cwd") or os.getcwd())

    sess_tot = proj_tot = proj_cost = proj_sub = 0
    try:
        con = db_connect()
        try:
            ingest_session(con, transcript, session_id)
            con.commit()
        except sqlite3.OperationalError:
            pass  # ledger ocupado por um hook: mostra o último valor gravado
        sess_tot, _, _ = totals(con, "project=? AND session_id=?", (project, session_id))
        proj_tot, proj_cost, proj_sub = totals(con, "project=?", (project,))
        con.close()
    except Exception:
        pass

    cw = data.get("context_window") or {}
    window = cw.get("context_window_size") or 200_000
    pct = cw.get("used_percentage")
    if pct is None:
        cu = cw.get("current_usage") or {}
        used = sum(int(cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        pct = 100.0 * used / window if window else 0
    model = (data.get("model") or {}).get("display_name") or ""

    lines = []
    head = "%s▸ %s%s  %s %s%3d%%%s" % (MAIN_COLOR, MAIN, RESET, bar(pct), level_color(pct), round(pct), RESET)
    if model:
        head += "  %s%s%s" % (DIM, model, RESET)

    subs = []
    if os.environ.get("AGENTMETER_INLINE", "1") != "0":
        try:
            subs = number_instances(active_subagents(session_id, transcript, window))
        except Exception:
            subs = []
    lines.append(head)
    if subs:
        label_w = max(len(r["label"]) for r in subs)
        label_w = max(label_w, len(MAIN))
        lines[0] = "%s▸ %-*s%s  %s %s%3d%%%s" % (MAIN_COLOR, label_w, MAIN, RESET, bar(pct), level_color(pct), round(pct), RESET)
        if model:
            lines[0] += "  %s%s%s" % (DIM, model, RESET)
        lines.append("%s─ ativos %d %s%s" % (DIM, len(subs), "─" * max(0, min(cols, 40) - 11), RESET))
        now = time.time()
        for r in subs:
            p = 100.0 * r["tokens"] / r["window"] if r["window"] else 0
            c = agent_color(r["type"])
            lines.append("%s▸ %-*s%s  %s %s%3d%%%s  %6s  %s%s%s" % (
                c, label_w, r["label"], RESET, bar(p), level_color(p), round(p), RESET,
                human(r["tokens"]), DIM, mmss(now - r["start"]), RESET))

    foot = "sessão %s%s%s │ projeto %s%s%s" % (BOLD, human(sess_tot), RESET, BOLD, human(proj_tot), RESET)
    if proj_tot:
        foot += " │ %ssub %d%%%s" % (DIM, round(100.0 * proj_sub / proj_tot), RESET)
    foot += " │ %s%s%s" % (DIM, brl_usd(proj_cost), RESET)
    lines.append(foot)
    return "\n".join(fit(l, cols) for l in lines)


# ---------------------------------------------------------------- painel de subagents

def cmd_subagent_line(data):
    session_id = data.get("session_id") or ""
    cols = int(data.get("columns") or 80)
    tasks = data.get("tasks") or []
    try:
        _save_json(_state_path(session_id, "panel"), {"at": time.time(), "tasks": tasks})
    except Exception:
        pass
    running = [t for t in tasks if t.get("status") == "running"]
    rows = number_instances([{
        "id": t.get("id"), "type": t.get("agentType") or "subagent", "start": (t.get("startTime") or 0) / 1000.0,
    } for t in running])
    labels = {r["id"]: r["label"] for r in rows}
    width = max([len(l) for l in labels.values()] + [6])
    now = time.time()
    out = []
    for t in tasks:
        tid = t.get("id")
        if t.get("status") != "running":
            out.append({"id": tid, "content": ""})  # só mostra os ativos
            continue
        tokens = t.get("tokenCount") or 0
        win = t.get("contextWindowSize") or 200_000
        p = 100.0 * tokens / win if win else 0
        label = labels.get(tid, t.get("agentType") or "subagent")
        desc = t.get("label") or t.get("description") or ""
        line = "%s%-*s%s %s %s%3d%%%s %6s %s%s%s  %s%s%s" % (
            agent_color(t.get("agentType") or "subagent"), width, label, RESET,
            bar(p), level_color(p), round(p), RESET, human(tokens),
            DIM, mmss(now - (t.get("startTime") or now * 1000) / 1000.0), RESET,
            DIM, desc, RESET)
        out.append({"id": tid, "content": fit(line, cols)})
    return "\n".join(json.dumps(o, ensure_ascii=False) for o in out)


# ---------------------------------------------------------------- hooks

def install_copy():
    """Copia este script e o lançador para ~/.claude/agentmeter/, um caminho que não muda entre versões do plug-in."""
    src = os.path.abspath(__file__)
    if os.path.abspath(INSTALLED_SCRIPT) == src:
        return
    os.makedirs(APP_DIR, exist_ok=True)
    here = os.path.dirname(src)
    for name in ("agentmeter.py", "run.sh"):
        origem = os.path.join(here, name)
        destino = os.path.join(APP_DIR, name)
        try:
            with open(origem, "rb") as a:
                new = a.read()
            try:
                with open(destino, "rb") as b:
                    if b.read() == new:
                        continue
            except OSError:
                pass
            tmp = destino + ".tmp"
            with open(tmp, "wb") as f:
                f.write(new)
            os.replace(tmp, destino)
        except OSError:
            pass


def cmd_hook(data):
    event = data.get("hook_event_name")
    sid = data.get("session_id") or ""
    if event == "SessionStart":
        install_copy()
    if event == "SubagentStart":
        st = load_state(sid)
        st.setdefault("agents", {})[data.get("agent_id") or "?"] = {
            "type": data.get("agent_type") or "subagent", "start": time.time()}
        save_state(sid, st)
        return
    if event == "SubagentStop":
        st = load_state(sid)
        aid = data.get("agent_id") or "?"
        st.setdefault("agents", {}).pop(aid, None)
        now = time.time()
        done = {k: v for k, v in st.get("done", {}).items() if now - v < 3600}
        done[aid] = now
        st["done"] = done
        save_state(sid, st)
    if event == "SessionEnd":
        for kind in ("hooks", "panel"):
            try:
                os.remove(_state_path(sid, kind))
            except OSError:
                pass
    if event in ("SessionStart", "SubagentStop", "Stop", "SessionEnd", "PreCompact"):
        con = db_connect()
        ingest_session(con, data.get("transcript_path"), sid)
        con.commit()
        con.close()


# ---------------------------------------------------------------- relatório

def parse_since(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([dhw])", s.strip().lower())
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"d": dt.timedelta(days=n), "h": dt.timedelta(hours=n), "w": dt.timedelta(weeks=n)}[u]
        return (dt.datetime.now(dt.timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S")
    return s  # data ISO, ex.: 2026-10-01


def find_project(cwd):
    p = encode_project(cwd)
    if os.path.isdir(os.path.join(PROJECTS_DIR, p)):
        return p
    # sobe a árvore: o projeto pode ter sido aberto num diretório pai
    d = cwd
    while d and d != os.path.dirname(d):
        d = os.path.dirname(d)
        p2 = encode_project(d)
        if os.path.isdir(os.path.join(PROJECTS_DIR, p2)):
            return p2
    return p


def latest_session(project):
    pdir = os.path.join(PROJECTS_DIR, project)
    best = None
    try:
        for n in os.listdir(pdir):
            if n.endswith(".jsonl"):
                t = os.path.getmtime(os.path.join(pdir, n))
                if best is None or t > best[0]:
                    best = (t, n[: -len(".jsonl")])
    except OSError:
        pass
    return best[1] if best else None


def fmt_date(ts, with_time=False):
    if not ts:
        return "?"
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return d.strftime("%d/%m %H:%M" if with_time else "%d/%m")
    except Exception:
        return ts[:10]


def cmd_report(args):
    con = db_connect()
    try:
        _report(args, con)
    finally:
        con.close()


def _report(args, con):
    cwd = args.cwd or os.getcwd()
    if args.todos:
        if os.path.isdir(PROJECTS_DIR):
            for p in sorted(os.listdir(PROJECTS_DIR)):
                ingest_project(con, p)
        where, params, scope = "1=1", [], "todos os projetos"
        project = None
    else:
        project = args.projeto and encode_project(os.path.abspath(args.projeto)) or find_project(cwd)
        ingest_project(con, project)
        where, params = "project=?", [project]
        row = con.execute("SELECT cwd FROM sessions WHERE project=? AND cwd IS NOT NULL ORDER BY last_ts DESC LIMIT 1", (project,)).fetchone()
        scope = os.path.basename(row[0]) if row and row[0] else os.path.basename(cwd)
    current = latest_session(project) if project else None
    if args.sessao:
        sid = args.sessao if args.sessao != "atual" else current
        where += " AND session_id=?"
        params.append(sid)
        scope += " · sessão " + (sid or "?")[:8]
    since = parse_since(args.desde)
    if since:
        where += " AND ts>=?"
        params.append(since)
        scope += " · desde " + args.desde

    if args.csv:
        cur = con.execute(
            "SELECT ts, session_id, agent_id, agent_type, model, input, cw5, cw1, cr, output, ROUND(cost,6) "
            "FROM usage WHERE " + where + " ORDER BY ts", params)
        print("ts,session_id,agent_id,agent_type,model,input,cache_write_5m,cache_write_1h,cache_read,output,cost_usd")
        for r in cur:
            print(",".join("" if v is None else str(v) for v in r))
        return

    tot = con.execute(
        "SELECT COALESCE(SUM(input+cw5+cw1+cr+output),0), COALESCE(SUM(cost),0), COUNT(DISTINCT session_id), "
        "MIN(ts), MAX(ts), COALESCE(SUM(input),0), COALESCE(SUM(cw5+cw1),0), COALESCE(SUM(cr),0), COALESCE(SUM(output),0) "
        "FROM usage WHERE " + where, params).fetchone()
    total, cost, nsess, first, last, s_in, s_cw, s_cr, s_out = tot
    W = 46
    line = "─" * W
    out = []
    out.append("agentmeter · %s" % scope)
    if not total:
        out.append("Ainda não há uso registrado aqui.")
        print("\n".join(out))
        return
    out.append("%d %s · %s → %s" % (nsess, "sessão" if nsess == 1 else "sessões", fmt_date(first), fmt_date(last)))
    out.append("")
    out.append("total %-12s custo est. %s" % (human(total), brl_usd(cost)))
    out.append("")
    out.append("─ por agente " + line[13:])
    rows = con.execute(
        "SELECT agent_type, SUM(input+cw5+cw1+cr+output) t, SUM(cost), COUNT(DISTINCT NULLIF(agent_id,'')) "
        "FROM usage WHERE " + where + " GROUP BY agent_type ORDER BY (agent_type!=?), t DESC",
        params + [MAIN]).fetchall()
    name_w = max([len(r[0]) for r in rows] + [7])
    for a_type, t, c, runs in rows:
        p = 100.0 * t / total
        out.append("%-*s %s %3d%% %7s %4s" % (
            name_w, a_type, bar(p, 10, color=False), round(p), human(t), "—" if a_type == MAIN else str(runs)))
    sub = sum(r[1] for r in rows if r[0] != MAIN)
    runs = sum(r[3] for r in rows if r[0] != MAIN)
    if sub:
        out.append("subagents: %d%% do total · %d runs · média %s/run" % (
            round(100.0 * sub / total), runs, human(sub / max(runs, 1))))
    out.append("")
    out.append("─ por tipo " + line[11:])
    # custo por tipo de token, recalculado por modelo
    by_model = con.execute(
        "SELECT model, SUM(input), SUM(cw5), SUM(cw1), SUM(cr), SUM(output) FROM usage WHERE " + where + " GROUP BY model",
        params).fetchall()
    ct = [0.0] * 4
    for model, i, c5, c1, r_, o in by_model:
        p = price_for(model, 0) or (0, 0, 0, 0, 0)
        ct[0] += r_ * p[3] / 1e6
        ct[1] += (c5 * p[1] + c1 * p[2]) / 1e6
        ct[2] += i * p[0] / 1e6
        ct[3] += o * p[4] / 1e6
    for label, v, c in (("cache read", s_cr, ct[0]), ("cache write", s_cw, ct[1]), ("input", s_in, ct[2]), ("output", s_out, ct[3])):
        out.append("%-12s %8s %14s" % (label, human(v), brl_usd(c)))
    out.append("")
    out.append("─ sessões mais pesadas " + line[23:])
    top = con.execute(
        "SELECT u.session_id, SUM(u.input+u.cw5+u.cw1+u.cr+u.output) t, MAX(u.ts), s.title "
        "FROM usage u LEFT JOIN sessions s ON s.session_id=u.session_id AND s.project=u.project WHERE " + where.replace("project", "u.project").replace("session_id", "u.session_id").replace(" ts", " u.ts") +
        " GROUP BY u.session_id ORDER BY t DESC LIMIT ?", params + [args.top]).fetchall()
    for sid, t, ts, title in top:
        mark = " ●" if sid == current else ""
        title = (title or sid[:8])
        title = title[:26] + ("…" if len(title) > 26 else "")
        out.append("%s  %-28s %7s" % (fmt_date(ts), title + mark, human(t)))
    out.append(line)
    out.append("--sessao · --desde 7d · --csv · --todos")
    print("\n".join(out))


# ---------------------------------------------------------------- setup

def cmd_setup(args):
    install_copy()
    settings_path = os.path.join(CLAUDE_DIR, "settings.json")
    try:
        # utf-8-sig: editores do Windows (Bloco de Notas, PowerShell 5) gravam o BOM.
        with open(settings_path, encoding="utf-8-sig") as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except Exception as e:
        print("Não consegui ler %s: %s" % (settings_path, e))
        sys.exit(1)
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        command = 'sh "%s" statusline' % os.path.join(os.path.dirname(INSTALLED_SCRIPT), "run.sh").replace(os.sep, "/")
    else:
        command = 'sh "$HOME/.claude/agentmeter/run.sh" statusline'
    current = settings.get("statusLine")
    if args.remover:
        if current and "agentmeter" in json.dumps(current):
            shutil.copy2(settings_path, settings_path + ".agentmeter-backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
            del settings["statusLine"]
            with open(settings_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print("Status line do agentmeter removida de %s" % settings_path)
        else:
            print("A status line atual não é do agentmeter; nada foi alterado.")
        return
    if current and "agentmeter" not in json.dumps(current) and not args.force:
        print("Você já tem uma status line configurada:")
        print(json.dumps(current, indent=2, ensure_ascii=False))
        print("Rode de novo com --force para substituir (um backup do settings.json é salvo).")
        sys.exit(2)
    if os.path.exists(settings_path):
        backup = settings_path + ".agentmeter-backup-%s" % time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, backup)
        print("Backup: %s" % backup)
    settings["statusLine"] = {"type": "command", "command": command, "refreshInterval": args.refresh}
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, settings_path)
    print("Status line do agentmeter ativada em %s" % settings_path)
    print("Comando: %s (atualiza a cada %ss)" % (command, args.refresh))


# ---------------------------------------------------------------- main

def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="agentmeter", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("statusline")
    sp.add_parser("subagent-line")
    sp.add_parser("hook")
    r = sp.add_parser("report")
    r.add_argument("--sessao", nargs="?", const="atual", help="só a sessão atual (ou um id)")
    r.add_argument("--desde", help="período: 7d, 24h, 2w ou data ISO")
    r.add_argument("--csv", action="store_true", help="exporta uma linha por requisição")
    r.add_argument("--todos", action="store_true", help="todos os projetos")
    r.add_argument("--projeto", help="caminho de outro projeto")
    r.add_argument("--cwd", help=argparse.SUPPRESS)
    r.add_argument("--top", type=int, default=3)
    s = sp.add_parser("setup")
    s.add_argument("--force", action="store_true")
    s.add_argument("--refresh", type=int, default=2)
    s.add_argument("--remover", action="store_true", help="tira a status line do agentmeter do settings.json")
    i = sp.add_parser("ingest")
    i.add_argument("--cwd")
    args = ap.parse_args(argv)

    if args.cmd == "statusline":
        try:
            print(cmd_statusline(read_stdin_json()))
        except Exception as e:  # a barra nunca deve quebrar a interface
            print("agentmeter: %s" % e)
    elif args.cmd == "subagent-line":
        try:
            out = cmd_subagent_line(read_stdin_json())
            if out:
                print(out)
        except Exception:
            pass
    elif args.cmd == "hook":
        try:
            cmd_hook(read_stdin_json())
        except Exception:
            pass
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "ingest":
        con = db_connect()
        try:
            p = find_project(args.cwd or os.getcwd())
            ingest_project(con, p)
        finally:
            con.close()
        print("ok: %s" % p)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
