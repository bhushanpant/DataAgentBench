"""Accept, repeat and agree: support check, red flags, repeat decision, answer agreement and Opus selection."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sq import overlay  # noqa: E402
from sq.frontdoor import ModelOutputError, call_opus  # noqa: E402
from sq.jev import JevUnavailable, choice, jev_ask, noul  # noqa: E402

SAME_P = 0.7
SUPPORT_Q = noul("Is this answer supported by the evidence and responsive to the question?",
                 "Yes, the evidence supports the answer and it responds to the question.",
                 "No, the evidence does not support it or it does not answer the question.")
SAME_Q = noul("Do these two answers state the same result?",
              "Yes, same result (formatting differences only).",
              "No, they state different results.")
_BAD = (JevUnavailable, KeyError, TypeError, ValueError, AttributeError)


def _noul_p(resp: dict, qid: str) -> float:
    """Probability from a noul answer; the key is omitted when 0.0. Raises on missing or malformed entries."""
    a = resp["answers"][qid]
    if not isinstance(a, dict):
        raise TypeError("answer is not a dict")
    return float(a.get("noul", 0.0))


def _cost(resp) -> float:
    try:
        return float(resp.get("cost_usd") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def support_prob(question: str, answer: str, evidence: str, *, jev=jev_ask) -> tuple:
    state = {"question": question, "answer": answer, "evidence": evidence}
    try:
        r = jev(state, {"supported": SUPPORT_Q})
    except JevUnavailable:
        return None, 0.0
    try:
        return _noul_p(r, "supported"), _cost(r)
    except _BAD:
        return None, _cost(r)


RED_FLAG = "RED FLAG:"  # the worker's marker for readings or ties the question does not resolve (see objective())
RED_FLAG_CHARS = 600


def red_flags(evidence: str) -> list:
    """The worker's red-flag notes in its evidence: from each "RED FLAG:" marker to the end of its line."""
    out = []
    for line in (evidence or "").splitlines():
        i = line.find(RED_FLAG)
        if i < 0:
            continue
        # "No RED FLAG: ..." or "RED FLAG: none" reports that there is no red flag
        if _NEG_BEFORE.search(line[:i]) or _NEG_AFTER.match(line[i + len(RED_FLAG):].strip()):
            continue
        out.append(line[i:].strip()[:RED_FLAG_CHARS])
    return out


_NEG_BEFORE = re.compile(r"\b(no|not|without|zero)\s+$", re.I)
_NEG_AFTER = re.compile(r"$|(none|n/?a|nothing|not applicable|no (red flags?|ambiguit\w*|ties?|alternative\w*))\b",
                        re.I)


def should_repeat(gate_pass: bool, p_support, *, threshold: float = 0.7) -> bool:
    return (not gate_pass) or (p_support is not None and p_support < threshold)


def plan_repeats(remaining_usd: float, est_attempt_usd: float, max_extra: int = 2) -> int:
    if est_attempt_usd <= 0:
        return max_extra
    return max(0, min(max_extra, int(remaining_usd // est_attempt_usd)))


def normalize(answer: str) -> str:
    """Exact answer text except surrounding whitespace; meaning requires task context.

    Never round counts, rewrite identifiers, fold case, or reorder lists here.
    Semantic agreement can still recognize formatting variants using the question.
    """
    return str(answer).strip()


class Agreement(tuple):
    """The legacy (answer, how, cost) tuple plus the selected candidate's index."""

    def __new__(cls, candidates, index, how, cost):
        obj = super().__new__(cls, (candidates[index]["answer"], how, cost))
        obj.candidate_index = index
        return obj


def identical_agreement(candidates: list):
    """Select without a model only when every answer is byte-for-byte identical.

    The selector can only choose an existing answer, so a model call cannot change
    the result here. Keep the strongest available format verification attached;
    this does not promote consensus to semantic verification.
    """
    if len(candidates) < 2 or len({c["answer"] for c in candidates}) != 1:
        return None
    def rank(i):
        c = candidates[i]
        state = (c.get("verification") or {}).get("state")
        return (state == "passed", c.get("gate_pass") is True and not c.get("fallback"),
                not bool(c.get("red_flags")), -i)
    index = max(range(len(candidates)), key=rank)
    return Agreement(candidates, index, "identical_answers", 0.0)


def _group(n: int, same) -> list:
    """Greedy grouping: an index joins the first group whose first member it matches."""
    groups = []
    for i in range(n):
        for g in groups:
            if same(g[0], i):
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


def _largest(groups: list) -> list:
    return max(groups, key=len)  # earliest group wins ties


def _exact(candidates: list, cost: float = 0.0) -> Agreement:
    norm = [normalize(c["answer"]) for c in candidates]
    big = _largest(_group(len(norm), lambda a, b: norm[a] == norm[b]))
    how = "exact_majority" if len(big) * 2 > len(candidates) else "no_majority_fallback"
    return Agreement(candidates, big[0], how, cost)


def agree(question: str, candidates: list, *, jev=jev_ask) -> tuple:
    if len(candidates) == 1:
        return Agreement(candidates, 0, "single", 0.0)
    identical = identical_agreement(candidates)
    if identical is not None:
        return identical
    cost = 0.0

    def same(a: int, b: int) -> bool:
        nonlocal cost
        r = jev({"question": question, "answer_a": candidates[a]["answer"], "answer_b": candidates[b]["answer"]},
                {"same": SAME_Q})
        cost += _cost(r)
        return _noul_p(r, "same") >= SAME_P

    try:
        groups = _group(len(candidates), same)
    except _BAD:
        return _exact(candidates, cost)
    big = _largest(groups)
    if len(big) * 2 > len(candidates):
        return Agreement(candidates, big[0], "majority", cost)

    labels = [f"candidate {i + 1}" for i in range(len(candidates))]
    state = {"question": question,
             "candidates": {lab: {"answer": c["answer"], "evidence": c["evidence"]} for lab, c in zip(labels, candidates)}}
    q = choice("Which candidate is best supported by its own evidence and most responsive to the question?", labels)
    try:
        r = jev(state, {"best": q})
        cost += _cost(r)
        pick = r["answers"]["best"]["choice"]
        return Agreement(candidates, labels.index(pick), "jev_best", cost)
    except _BAD:
        return _exact(candidates, cost)


def disagree(candidates: list) -> bool:
    """True when exact answer texts differ; semantic equality must be assessed separately."""
    return len({normalize(c["answer"]) for c in candidates}) > 1


_STR_SCHEMA = {"type": "string"}
ADJUDICATE_BUDGET_USD = 0.15
ADJUDICATE_EVIDENCE_CHARS = 6000
ADJUDICATE_SYSTEM = """You adjudicate between candidate answers to one data question. Each candidate comes from an \
independent analyst who queried the data; you see the question, each candidate's answer, its evidence and any RED FLAG \
notes (readings of the question, or tied results, that the analyst could not resolve, with the answer under each). You \
cannot query the data. You never write a new answer: you select exactly one candidate by its number.

Select the candidate whose answer is best supported:
- Reading: which reading of the question its exact wording supports (the terms, scope, population, time window and \
selection it states). "Most natural", "standard" or "conventional" is not an argument.
- Evidence: whether the candidate's evidence actually computes its answer under that reading (queries, row counts, \
final values). A tie or ambiguity settled by a sort key or condition the question does not state counts against it.
- Shape: whether the answer gives exactly what the question asks, with nothing missing and nothing added.
- A candidate that failed its format check is weaker, all else equal.
Give a short reason that names the reading you judged the question to support."""


def _adjudicate_user(question: str, candidates: list) -> str:
    parts = [f"## Question\n\n{question.strip()}\n"]
    for i, c in enumerate(candidates, 1):
        flags = c.get("red_flags") or red_flags(c.get("evidence") or "")
        failed = c.get("fallback") or c.get("gate_pass") is False  # a fallback answer never passed its gate
        verification = c.get("verification") or {}
        if verification:
            gate = "format verification: " + verification.get("state", "missing")
        else:
            gate = ("failed its format check" if failed else "passed its format check"
                    if c.get("gate_pass") is True else "format verification unavailable")
        parts.append(f"## Candidate {i} ({gate})\n\nAnswer:\n{c['answer']}\n\n"
                     f"Red flags:\n{chr(10).join(flags) if flags else 'none reported'}\n\n"
                     f"Evidence:\n{(c.get('evidence') or 'none').strip()[:ADJUDICATE_EVIDENCE_CHARS]}\n")
    return "\n".join(parts)


def adjudicate(question: str, candidates: list, *, llm=call_opus, budget_usd: float = ADJUDICATE_BUDGET_USD) -> tuple:
    """One Opus call that SELECTS a candidate -> (index, reason, cost). The schema has no answer field, so the call
    cannot write a new answer; a selection outside 1..n (or malformed) raises ValueError."""
    n = len(candidates)
    schema = {"type": "object", "required": ["candidate", "reason"],
              "properties": {"candidate": {"type": "integer", "minimum": 1, "maximum": n}, "reason": _STR_SCHEMA}}
    out, cost = llm(ADJUDICATE_SYSTEM + overlay.text("adjudicate"), _adjudicate_user(question, candidates), schema, budget_usd=budget_usd)
    pick = (out or {}).get("candidate") if isinstance(out, dict) else None
    if isinstance(pick, bool) or not isinstance(pick, int) or not 1 <= pick <= n:
        raise ModelOutputError(f"adjudication selected no valid candidate (1..{n})", cost_usd=cost)
    return pick - 1, str(out.get("reason") or ""), float(cost or 0.0)
