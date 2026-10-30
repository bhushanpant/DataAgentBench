#!/usr/bin/env python3
"""Host-side label service: the sandbox's `jlabel` tool -> this process -> Jev (POST /api/ship/jev).

The server JWT stays on the host. A sandbox only holds a per-trial token that can do one thing: ask Jev to label
rows it sends (choice among categories it defines, or a yes/no probability), up to an item budget, for a few hours.
The service knows nothing about datasets, queries or answers; every request is logged for the trial's traces.

Run: JLABEL_PORT (default 18765), JLABEL_TOKENS (token dir), MULTICA_API, MULTICA_WS, QUORUM_JWT_FILE.
Token file <sha256(token)>.json is created by `register()` (called while a trial's kit is built).
"""
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from sq import specialists  # noqa: E402
RUNTIME = HERE.parents[1]
PORT = int(os.environ.get("JLABEL_PORT", "18765"))
TOKENS = Path(os.environ.get("JLABEL_TOKENS", RUNTIME / "runs" / "jlabel"))
API = os.environ.get("MULTICA_API", "http://localhost:8081")
WS = os.environ.get("MULTICA_WS", "5ef53d9c-52d8-421b-bed0-b56c385a7218")
JWT = Path(os.environ.get("QUORUM_JWT_FILE", RUNTIME.parents[2] / "runs" / "quorum-dab" / ".jwt"))
BACKEND = os.environ.get("JLABEL_BACKEND", "server")  # "server": multica /api/ship/jev; "openrouter": OpenRouter decisions API
OR_URL = "https://openrouter.ai/api/alpha/decisions"
OR_MODEL = os.environ.get("JLABEL_MODEL", "typesafe/jev-1.14-20261014")  # pinned Jev version, not the moving alias
MAX_BATCH = 500           # items per request
MAX_ITEMS = 60000         # items per trial token
MAX_ITEM_CHARS = 20000    # serialized size of one row
TTL_S = 3 * 3600
WORKERS = 64
CONFIDENT = 0.9           # a first pass this sure is accepted as is
_lock = threading.Lock()
_used: dict = {}
_cache: dict = {}
_asks: dict = {}
ASK_BUDGET_USD = 2.0        # specialist calls per task token
ASK_SLOTS = threading.BoundedSemaphore(int(os.environ.get("ASK_CONCURRENCY", "6")))
_billing_down = 0.0          # when Jev last answered 'no credits'
BILLING_RETRY_S = 30        # while down, refuse label requests for this long instead of hammering Jev
GLOBAL_SLOTS = threading.BoundedSemaphore(int(os.environ.get('JLABEL_CONCURRENCY', '48')))  # Jev calls in flight, all trials
REQUEST_DEADLINE_S = 90     # a label request must answer well inside the sandbox's tool-call limit
PASSES = int(os.environ.get("JLABEL_PASSES", "3"))  # Jev is not deterministic: average this many passes on unsure rows


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(max_items: int = MAX_ITEMS, tokens_dir: Path = TOKENS) -> str:
    """Create a trial token (returned) and its record on disk; called on the host."""
    token = secrets.token_urlsafe(24)
    tokens_dir.mkdir(parents=True, exist_ok=True)
    (tokens_dir / f"{_digest(token)}.json").write_text(json.dumps({"created": time.time(), "max_items": max_items}))
    return token


def finish(token_hash: str, tokens_dir: Path = TOKENS) -> dict:
    """Revoke a token and return its usage summary {calls, items, cost_usd} (its log stays for the traces)."""
    rec = tokens_dir / f"{token_hash}.json"
    rec.unlink(missing_ok=True)
    log, calls, items, cost, billing = tokens_dir / f"{token_hash}.jsonl", 0, 0, 0.0, 0
    if log.exists():
        for line in log.read_text().splitlines():
            r = json.loads(line)
            calls += 1
            items += r.get("n", 0)
            cost += float(r.get("cost_usd") or 0)
            billing += bool(r.get("billing_error"))
    return {"calls": calls, "items": items, "cost_usd": cost, "billing_errors": billing}


class Billing(Exception):
    pass


def _openrouter_key() -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for env in (RUNTIME.parents[2] / ".env",):
        for line in env.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("OPENROUTER_API_KEY not found")


def _request(state: dict, question: dict) -> urllib.request.Request:
    if BACKEND == "openrouter":
        body = json.dumps({"model": OR_MODEL, "state": state, "questions": {"q": question}}).encode()
        req = urllib.request.Request(OR_URL, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + _openrouter_key())
    else:
        body = json.dumps({"state": state, "questions": {"q": question}}).encode()
        req = urllib.request.Request(API + "/api/ship/jev", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + JWT.read_text().strip())
        req.add_header("X-Workspace-ID", WS)
    req.add_header("Content-Type", "application/json")
    return req


def _jev(state: dict, question: dict) -> dict:
    global _billing_down
    for attempt in range(3):
        req = _request(state, question)
        try:
            with GLOBAL_SLOTS:
                with urllib.request.urlopen(req, timeout=20) as r:
                    resp = json.loads(r.read())
            cost = float(resp.get("cost_usd") or (resp.get("usage") or {}).get("cost") or 0)
            return {"answer": resp["answers"]["q"], "cost_usd": cost, "model": resp.get("model")}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:600]
            if e.code == 402 or "billing_error" in text or "no available TypeSafe API credits" in text or "HTTP 402" in text:
                _billing_down = time.time()
                raise Billing(text)
            err = f"HTTP {e.code}: {text[:160]}"
            if e.code == 429:
                time.sleep(2.0 * (attempt + 1))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            err = f"{type(e).__name__}: {e}"[:200]
        if attempt == 2:
            return {"error": err, "cost_usd": 0.0}
        time.sleep(1.0 * (attempt + 1))


def _question(req: dict) -> dict:
    instructions = str(req["instructions"])
    if req["mode"] == "choice":
        cats = req["categories"]
        return {"type": "choice", "instructions": instructions,
                "criteria": {str(k): (v if v else None) for k, v in cats.items()}}
    return {"type": "noul", "instructions": instructions}


def _combine(mode: str, answers: list) -> dict:
    """Average the passes: mean probability per option (choice) or mean yes-probability (noul)."""
    if mode == "noul":
        vals = [a.get("noul") for a in answers if a.get("noul") is not None]
        return {"probability": sum(vals) / len(vals), "passes": len(vals)}
    keys = {k for a in answers for k in (a.get("probabilities") or {})}
    mean = {k: sum((a.get("probabilities") or {}).get(k, 0.0) for a in answers) / len(answers) for k in keys}
    pick = max(mean, key=mean.get)
    return {"label": pick, "confidence": round(mean[pick], 4), "probabilities": mean,
            "votes": sum(1 for a in answers if a.get("choice") == pick), "passes": len(answers)}


def label(req: dict, th: str = "") -> dict:
    question = _question(req)
    # Labels are shared within a trial by row + category names (choice) or the exact condition (yes/no), whatever the
    # wording of the instruction: parallel worker sessions labelling the same rows reuse each other's work.
    qkey = json.dumps(sorted(question["criteria"]) if question["type"] == "choice" else question["instructions"])
    cache = _cache.setdefault(th, {})
    deadline = time.time() + REQUEST_DEADLINE_S
    billing = []

    def one(item):
        ikey = qkey + json.dumps(item, sort_keys=True, default=str)
        if ikey in cache:  # same row and question within one trial: same label
            return cache[ikey], 0.0
        if billing:
            return {"error": "out of credits"}, 0.0
        if time.time() > deadline:
            return {"error": "deadline: request too large for one call, retry"}, 0.0
        state = item if isinstance(item, dict) else {"text": str(item)}
        try:
            passes = [_jev(state, question)]
            first = passes[0].get("answer") or {}
            sure = (first.get("confidence") or 0) >= CONFIDENT if req["mode"] == "choice" \
                else not 0.15 < (first.get("noul") if first.get("noul") is not None else 0.5) < 0.85
            if not sure:  # Jev is not deterministic and flips on borderline rows: average more passes only there
                passes += [_jev(state, question) for _ in range(PASSES - 1)]
        except Billing as e:
            billing.append(str(e))
            return {"error": "out of credits"}, 0.0
        good = [p["answer"] for p in passes if "answer" in p]
        cost = sum(p["cost_usd"] for p in passes)
        if not good:
            return {"error": passes[0].get("error", "no answer")}, cost
        cache[ikey] = _combine(req["mode"], good)
        return cache[ikey], cost

    with ThreadPoolExecutor(WORKERS) as pool:
        out = list(pool.map(one, items_of(req)))
    return {"results": [o[0] for o in out], "cost_usd": sum(o[1] for o in out), "billing_error": bool(billing)}


def items_of(req: dict) -> list:
    return req["items"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "jev": time.time() - _billing_down > BILLING_RETRY_S})
        if self.path == "/health?probe=1":  # a real, cheap Jev call: is the label model usable right now?
            try:
                r = _jev({"text": "probe"}, {"type": "noul", "instructions": "Is this text empty?"})
                ok = "answer" in r
            except Billing:
                ok = False
            return self._send(200, {"status": "ok", "jev": ok})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/label", "/ask"):
            return self._send(404, {"error": "not found"})
        if self.path == "/ask":
            return self._ask()
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        th = _digest(token) if token else ""
        rec = TOKENS / f"{th}.json"
        try:
            meta = json.loads(rec.read_text())
        except (OSError, ValueError):
            return self._send(401, {"error": "invalid or revoked token"})
        if time.time() - meta["created"] > TTL_S:
            return self._send(401, {"error": "token expired"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            items, mode = req["items"], req["mode"]
            assert mode in ("choice", "noul") and isinstance(items, list) and req.get("instructions")
            if mode == "choice":
                assert isinstance(req["categories"], dict) and 2 <= len(req["categories"]) <= 50
            assert all(len(json.dumps(i)) <= MAX_ITEM_CHARS for i in items)
        except (ValueError, KeyError, AssertionError, TypeError):
            return self._send(400, {"error": "bad request: need mode (choice|noul), instructions, items[, categories]"})
        if time.time() - _billing_down < BILLING_RETRY_S:
            self._log(th, req, {"billing_error": True, "results": []}, 0.0, 0.0)
            return self._send(503, {"error": "the label service is out of credits"})
        if len(items) > MAX_BATCH:
            return self._send(400, {"error": f"at most {MAX_BATCH} items per request"})
        with _lock:
            if _used.get(th, 0) + len(items) > meta["max_items"]:
                return self._send(429, {"error": f"item budget of {meta['max_items']} for this trial exhausted"})
            _used[th] = _used.get(th, 0) + len(items)
        t0 = time.time()
        res = label(req, th)
        self._log(th, req, res, time.time() - t0, res["cost_usd"])
        if res.get("billing_error"):
            return self._send(503, {"error": "the label service is out of credits"})
        self._send(200, res)

    def _ask(self):
        """Specialist tools (sq/specialists.py): one tool-less Opus call per request, capped per trial."""
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        th = _digest(token) if token else ""
        try:
            meta = json.loads((TOKENS / f"{th}.json").read_text())
        except (OSError, ValueError):
            return self._send(401, {"error": "invalid or revoked token"})
        if time.time() - meta["created"] > TTL_S:
            return self._send(401, {"error": "token expired"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            tool = req["tool"]
            assert (tool == "note" or tool in specialists.SCHEMAS) and len(json.dumps(req)) <= 200000
            assert all(isinstance(req.get(k, ""), str) for k in ("question", "focus", "profile", "answer", "how", "shape", "contract"))
        except (ValueError, KeyError, AssertionError, TypeError):
            return self._send(400, {"error": "bad request: need tool (locate|verify) and text fields"})
        if tool == "note":  # a sandbox tool reports its own use (joincheck); logged, no model call
            self._log(th, {"tool": "note", "how": req.get("how", "")[:500], "items": []}, {"text": ""}, 0.0, 0.0)
            return self._send(200, {"text": ""})
        with _lock:
            used = _asks.setdefault(th, {"cost": 0.0})
            if used.get(tool, 0) >= specialists.limits(tool):
                return self._send(429, {"error": f"{tool}: at most {specialists.limits(tool)} calls per task; use what it returned"})
            if used["cost"] > ASK_BUDGET_USD:
                return self._send(429, {"error": "specialist budget for this task is used up"})
            used[tool] = used.get(tool, 0) + 1
        t0 = time.time()
        try:
            from sq import frontdoor, submission_evidence
            submission_evidence.opus_request = submission_evidence.opus_response = lambda *a, **k: None  # logged below
            with ASK_SLOTS:
                text, cost, obj = specialists.ask(tool, req, frontdoor.call_opus)
        except Exception as e:  # noqa: BLE001 - the worker carries on without the specialist
            self._log(th, {"tool": tool, "items": []}, {"error": str(e)[:300]}, time.time() - t0, 0.0)
            return self._send(503, {"error": f"{tool} is unavailable right now ({type(e).__name__}); continue without it"})
        with _lock:
            _asks[th]["cost"] += cost
        self._log(th, {"tool": tool, "request": req, "items": []}, {"text": text, "raw": obj}, time.time() - t0, cost)
        self._send(200, {"text": text, "cost_usd": cost})

    def _log(self, th, req, res, seconds, cost):
        with (TOKENS / f"{th}.jsonl").open("a") as f:
            f.write(json.dumps({"time": time.time(), "seconds": round(seconds, 2), "n": len(req.get("items", [])),
                                "request": req, "response": res, "cost_usd": cost,
                                "billing_error": bool(res.get("billing_error"))}, ensure_ascii=False) + "\n")


def main():
    TOKENS.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"jlabel proxy on 127.0.0.1:{PORT}, backend {BACKEND}" + (f" ({OR_MODEL})" if BACKEND == "openrouter" else "") + f", tokens {TOKENS}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
