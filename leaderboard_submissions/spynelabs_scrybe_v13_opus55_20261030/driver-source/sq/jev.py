"""Jev (TypeSafe System One) client. Reached only through the multica server endpoint POST /api/ship/jev."""
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class JevUnavailable(RuntimeError):
    """Jev could not be reached or answered with an error."""


def noul(instructions: str, yes: str, no: str) -> dict:
    q = {"type": "noul", "instructions": instructions}
    if yes or no:
        q["criteria"] = {"true": yes, "false": no}
    return q


def choice(instructions: str, options: list) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": {option: None for option in options}}  # Jev takes {option: null}, not a list


def jev_ask(state: dict, questions: dict, run_id: str = None) -> dict:
    """Ask Jev `questions` ({id: question}) about `state`. Returns {answers, cost_usd}."""
    from sq import quorum
    body = {"state": state, "questions": questions}
    if run_id:
        body["run_id"] = run_id
    try:
        resp = quorum.api("POST", "/api/ship/jev", body, timeout=120)
    except (quorum.APIError, urllib.error.URLError, OSError, ValueError) as e:
        raise JevUnavailable(str(e)) from e
    return {"answers": resp.get("answers", {}), "cost_usd": float(resp.get("cost_usd") or 0.0)}
