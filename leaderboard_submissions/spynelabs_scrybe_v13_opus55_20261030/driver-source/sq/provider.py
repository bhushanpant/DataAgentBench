"""Model provider for every front-door call: Anthropic direct, or OpenRouter (same models) once Anthropic credits run out.

The switch is one-way and shared by every process through runs/sq-provider.json. It flips when any call reports an
Anthropic billing error (credit balance too low / insufficient credits / HTTP 402), or when SQ_PROVIDER=openrouter.
- Quorum workers: the charter model becomes openrouter/anthropic/claude-sonnet-5.5; the server's LLM gateway serves
  it with MULTICA_OPENROUTER_API_KEY (the sandbox already lists the openrouter provider).
- Opus calls (`claude -p`): ANTHROPIC_BASE_URL=https://openrouter.ai/api with the OpenRouter key from scrybe .env,
  model anthropic/claude-opus-5.5.
"""
import json
import os
import re
import time
from pathlib import Path

STATE = Path(__file__).resolve().parents[2] / "runs" / "sq-provider.json"
ENV_FILES = (Path(__file__).resolve().parents[2] / ".env",
             Path.home() / "Desktop/Code/spynelabs/scrybe/.env")  # worktree, then the main checkout
MODELS = {"anthropic": {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"},
          "openrouter": {"opus": "anthropic/claude-opus-5.5", "sonnet": "openrouter/anthropic/claude-sonnet-5.5"}}
CREDIT = re.compile(r"credit balance is too low|insufficient[_ ]credits?|payment required|purchase (more )?credits"
                    r"|\b402\b.{0,60}(payment|credit)", re.I)  # applied to error text only, never to task data


def current() -> str:
    if os.environ.get("SQ_PROVIDER") == "openrouter":
        return "openrouter"
    try:
        return json.loads(STATE.read_text()).get("provider", "anthropic")
    except (OSError, ValueError):
        return "anthropic"


def switch(reason: str) -> None:
    if current() == "openrouter":
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"provider": "openrouter", "since": time.strftime("%F %T"), "reason": reason[:500]}))
    os.replace(tmp, STATE)


def is_credit_error(text: str) -> bool:
    return bool(CREDIT.search(text or ""))


def view_credit_error(view: dict) -> str:
    """A billing error in a Quorum run's own error fields (assignment reasons, run/report errors), or ""."""
    texts = [str(a.get("reason") or "") for a in view.get("assignments") or []]
    for k in ("run", "report"):
        v = view.get(k) or {}
        texts += [str(v.get(f) or "") for f in ("error", "reason", "stop_reason", "message")]
    return next((t[:300] for t in texts if is_credit_error(t)), "")


def model(role: str) -> str:
    return MODELS[current()][role]


def _openrouter_key() -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for f in ENV_FILES:
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            k, _, v = line.strip().removeprefix("export ").partition("=")
            if k == "OPENROUTER_API_KEY" and v:
                return v.strip().strip("'\"")
    raise RuntimeError("OPENROUTER_API_KEY not found for the OpenRouter fallback")


def claude_env() -> dict:
    """Environment for `claude -p` under the current provider (a copy of os.environ)."""
    env = dict(os.environ)
    if current() == "openrouter":
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.update(ANTHROPIC_BASE_URL="https://openrouter.ai/api", ANTHROPIC_AUTH_TOKEN=<REDACTED>
                   ANTHROPIC_API_KEY="")
    return env
