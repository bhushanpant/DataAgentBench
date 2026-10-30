"""Completeness audit: one tool-less Opus call that checks a candidate answer against the question's wording.

It sees the question, the declared answer format, the candidate answer and the worker's evidence. It never sees a
score, a reference answer or a validator. It reports components the question asks for that the answer does not
supply, so the flow can run another attempt with that note. A failed audit call changes nothing.
"""
from sq import frontdoor, overlay

BUDGET_USD = 0.15
EVIDENCE_CHARS = 5000
SCHEMA = {
    "type": "object",
    "required": ["complete", "missing", "revised_format"],
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "revised_format": {"type": "string"},
    },
}
SYSTEM = """You audit one candidate answer to a data question for completeness and shape. You do not judge whether \
the numbers are right and you cannot see the data.

Read the question and list every item it asks to be identified, returned or reported (each noun or value it asks \
for: an entity, a file, a count, a date, a ranked list with its length, a unit). Then compare with the declared answer \
format and the candidate answer.

- complete is false when the question's wording names two or more things to identify or return (for example "the X \
and the Y", "X together with Y") and the declared format or the candidate answer supplies fewer of them. Listing the \
things the question joins with "and" counts: each is an item to deliver. complete is true when every named item is \
supplied, including when the format is merely terse. A condition, filter or qualifier that only defines which entity \
to find is not itself an item to return.
- missing: one short phrase per item the answer lacks (empty when complete).
- revised_format: when incomplete, the exact answer shape that includes every item the question asks for, written as \
the format description a worker would follow (one shape, no options). Empty when complete.
Never invent requirements the question does not state."""


def user_prompt(question: str, fmt: str, answer: str, evidence: str) -> str:
    return (f"## Question\n\n{question.strip()}\n\n## Declared answer format\n\n{fmt}\n\n## Candidate answer\n\n"
            f"{answer}\n\n## Worker evidence (excerpt)\n\n{(evidence or 'none').strip()[:EVIDENCE_CHARS]}\n")


def completeness(question: str, fmt: str, answer: str, evidence: str, *, llm=frontdoor.call_opus) -> tuple:
    """(result | None, cost_usd). result = {complete, missing, revised_format}; None when the call failed."""
    try:
        out, cost = llm(SYSTEM + overlay.text("audit"), user_prompt(question, fmt, answer, evidence), SCHEMA,
                        budget_usd=BUDGET_USD)
    except Exception as e:  # noqa: BLE001 - an unavailable audit leaves the flow unchanged
        return None, float(getattr(e, "cost_usd", 0.0) or 0.0)
    if not isinstance(out.get("complete"), bool):
        return None, cost
    missing = [str(m) for m in (out.get("missing") or [])][:6]
    return {"complete": out["complete"] or not missing, "missing": missing,
            "revised_format": str(out.get("revised_format") or "").strip()}, cost
