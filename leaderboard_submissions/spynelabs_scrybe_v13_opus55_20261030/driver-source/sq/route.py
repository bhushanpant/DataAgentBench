"""Size routing from the intake's plan and signals plus Jev answers; never from dataset, task or benchmark names."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sq.jev import JevUnavailable, jev_ask, noul  # noqa: E402

SWARM_P = 0.7
PER_ROW_Q = noul("Does answering this require a semantic judgment on each of many rows "
                 "(labeling, classifying, extracting from text)?",
                 "Yes, each of many rows needs its own semantic judgment.",
                 "No, the answer comes from computation, joins or aggregation over structured fields.")


def _root_bounties(plan: list) -> int:
    """Count bounties with no in-plan dependency; roots cannot depend on each other."""
    refs = {b.get("ref") for b in plan}
    return sum(1 for b in plan if not [d for d in b.get("depends_on") or [] if d in refs])


def choose_size(intake, *, jev=jev_ask, swarm_rows: int = 2000) -> tuple:
    sig = intake.signals
    cost = 0.0
    p = None
    flag = sig.get("per_row_semantic")
    if flag is None:
        try:
            r = jev({"plan": intake.plan, "deliverable": intake.deliverable, "signals": sig}, {"per_row_semantic": PER_ROW_Q})
        except JevUnavailable:
            return "solo", "jev unavailable", 0.0
        try:
            cost = float(r.get("cost_usd") or 0.0)
            p = float(r["answers"]["per_row_semantic"].get("noul", 0.0))  # noul is omitted when 0.0
        except (KeyError, TypeError, ValueError, AttributeError):
            return "solo", "jev unavailable", cost
        flag = p >= SWARM_P
    rows = sig.get("est_rows", 0)
    if flag and rows >= swarm_rows:
        why = f"per-row semantic (p={p:.2f})" if p is not None else "per-row semantic (intake)"
        return "swarm", f"{why}, ~{rows} rows", cost
    if _root_bounties(intake.plan) >= 2 and sig.get("sources", 0) >= 2:
        return "team", "independent bounties over multiple sources", cost
    return "solo", "default", cost
