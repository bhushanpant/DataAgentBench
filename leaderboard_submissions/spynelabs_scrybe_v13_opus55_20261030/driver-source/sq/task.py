"""One task through the front door: kit, intake, size, attempt 1, repeat and agree, score. Plus the large-task lock.

Everything the flow touches outside this process (kit staging, Opus, Jev, Quorum, the scorer, the clock) comes in
through `Deps`, so the flow is testable with fakes. Scoring runs host-side after the answer is fixed; nothing from
the scorer reaches intake, Jev or Quorum.
"""
import concurrent.futures as cf
import fcntl
import hashlib
import json
import re
import math
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dab_common as dc  # noqa: E402
from sq import agree as ag, audit, frontdoor, quorum, route, specialists  # noqa: E402

CAPS = {"solo": 5.00, "team": 3.00, "swarm": 8.00}  # per task: front door + Quorum + Jev; repeats draw from it
WALL_S = 1800
EVIDENCE_CHARS = 20_000
LARGE_BYTES = 2_000_000_000  # staged data above this runs one task at a time across both arms
LOCK_PATH = quorum.REPO / "runs" / "sq-large.lock"
GATES_PATH = quorum.REPO / "docs" / "quorum" / "jev-gates.json"
NO_DELIVERABLE = "no deliverable"
INFRA_LOST = "sandbox or session lost after staging; no attempt ended normally"
GATE_DEFAULTS = {"q_label": True, "repeat_trigger": "jev", "threshold": 0.7, "single_attempt": False, "tools": False,
                 "contract": True, "stage_gate": True}  # contract / stage_gate only act when tools is on
TRIGGERS = ("jev", "gate_only", "red_flag")
RED_FLAG_REPEAT_CHARS = 1500
MIN_REPEAT_USD = 0.10  # a bounded retry may be shorter than the first attempt
# appended to a repeat's objective when attempt 1 reported a red flag (the worker's own note, never task data)
RED_FLAG_REPEAT = ("\n\nRED FLAG FROM AN EARLIER ATTEMPT: {flags}\n"
                   "Examine the alternative readings or tied candidates this red flag names and argue, from the "
                   "question's wording and the data, which one the question supports; answer under that reading "
                   "(report a RED FLAG again only if the question truly does not decide it).")


# appended to a repeat's objective when the completeness audit found items the question asks for that the answer lacks
AUDIT_REPEAT = ("\n\nCOMPLETENESS NOTE FROM A REVIEW OF AN EARLIER ATTEMPT: the question asks for items the earlier "
                "answer did not supply: {missing}. Re-read the question's wording, find those items in the data, and "
                "answer with every item the question asks for, in the answer format given below.")


def load_gates(path: Path = None) -> dict:
    """Jev gates (q_label on/off, repeat trigger jev|gate_only|red_flag, support threshold).
    No path: the default file, or the defaults when it is absent. An explicit path must exist and parse
    (FileNotFoundError / ValueError otherwise). Of the file's keys only q_label, repeat_trigger and threshold
    are read (the validation files also carry measurement fields)."""
    explicit = path is not None
    path = Path(path if explicit else GATES_PATH)
    gates = dict(GATE_DEFAULTS)
    if explicit or path.is_file():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"gates file {path} must hold a JSON object")
        gates.update({k: data[k] for k in GATE_DEFAULTS if k in data})
    if gates["repeat_trigger"] not in TRIGGERS:
        raise ValueError(f"repeat_trigger must be one of {', '.join(TRIGGERS)}, got {gates['repeat_trigger']!r}")
    return gates


class LargeTaskLock:
    """Exclusive file lock (fcntl.flock) shared by every runner process and thread on this host."""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = Path(path)
        self._f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        self._f = None


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def data_bytes(kit: Path) -> int:
    return sum(p.stat().st_size for p in (kit / "data").rglob("*") if p.is_file())


@dataclass
class Deps:
    build_kit: Callable = quorum.build_kit  # (ds, q, out) -> (kit, server)
    sage: Callable = frontdoor.sage_overview  # (kit) -> str
    intake: Callable = frontdoor.intake  # (task_md, sage, *, q_label) -> Intake
    choose_size: Callable = route.choose_size  # (intake) -> (size, reason, jev_cost)
    run_attempt: Callable = quorum.run_attempt  # (name, tid, attempt, body, kit, server, deadline_s) -> dict
    support_prob: Callable = ag.support_prob  # (question, answer, evidence) -> (p | None, jev_cost)
    agree: Callable = ag.agree  # (question, candidates) -> (answer, how, jev_cost)
    adjudicate: Callable = ag.adjudicate  # (question, candidates) -> (index, reason, cost); red_flag mode only
    score: Callable = dc.score  # (ds, q, answer) -> {reward, reason}
    clock: Callable = time.time
    lock_path: Path = field(default=LOCK_PATH)


def acceptance(intake) -> list:
    """The deliverable AC carries every intake check: the kernel gate runs only the deliverable AC's check."""
    d = intake.deliverable
    cmds = [c["cmd"].strip() for c in intake.checks if c.get("cmd", "").strip()] or [frontdoor.DEFAULT_CHECK_CMD]
    text = f"The statement is the final answer only, in this format: {d.get('format', '')}."
    texts = [c["text"] for c in intake.checks if c.get("text")]
    if texts:
        text += " Checks: " + "; ".join(texts) + "."
    return [{"id": d.get("ac") or "AC-1", "text": text, "check": " && ".join(f"( {c} )" for c in cmds)}]


def _cents(usd: float) -> float:
    return math.floor(usd * 100 + 1e-9) / 100  # round down: a sub-cap never pushes the task over its cap


def _write(path: Path, obj: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _usable(rec: dict) -> bool:
    """An attempt that yields a candidate: a report statement, or a fallback (a gate-failed answer claim)."""
    return rec["ok"] or rec["fallback"]


def _answer(rec: dict) -> str:
    return (rec["statement"] if rec["ok"] else rec.get("fallback_statement") or "").strip()


def solo_plan(plan: list[dict]) -> list[dict]:
    """Collapse a plan to ONE deliverable bounty: solo size admits only the deliverable step, so a multi-step plan
    would otherwise end with the deliverable as a gap. Instructions are the steps in dependency order."""
    if len(plan) == 1:  # already one bounty (possibly collapsed before): do not re-prefix its instructions
        return [dict(frontdoor.normalize_bounty(plan[0]), deliverable=True, depends_on=[], ac_refs=[])]
    by_ref = {b["ref"]: b for b in plan}
    deliv = next((b for b in plan if b.get("deliverable")), plan[-1])
    order, seen = [], set()

    def visit(b):
        if b["ref"] in seen:
            return
        seen.add(b["ref"])
        for d in b.get("depends_on") or []:
            if d in by_ref:
                visit(by_ref[d])
        order.append(b)

    for b in plan:
        if b is not deliv:
            visit(b)
    visit(deliv)  # the deliverable step comes last
    steps = [b for b in order if b is not deliv] + [deliv]
    one = {"ref": deliv["ref"], "kind": "build", "title": deliv.get("title", ""), "claim_type": "analysis",
           "deliverable": True, "depends_on": [], "ac_refs": [],
           "instructions": "\n\n".join(f"Step {k} ({b.get('title', '')}): {b.get('instructions', '')}"
                                        for k, b in enumerate(steps, 1))}
    nd = frontdoor.normalize_bounty(deliv)
    one.update({k: nd[k] for k in ("files", "value", "risk")})
    one["est_sec"] = sum(frontdoor.normalize_bounty(b)["est_sec"] for b in plan)
    return [frontdoor.normalize_bounty(one)]


def run_task(arm: str, tid: str, out: Path, *, deps: Deps = Deps(), wall_s: int = WALL_S,
             gates: dict = None) -> dict:
    """Run one task for `arm` (solo|tiered); write and return out/<ds>__<q>/result.json. `out` is runs/<name>."""
    if arm not in ("solo", "tiered"):
        raise ValueError(f"arm must be solo or tiered, got {arm!r}")
    gates = load_gates() if gates is None else {**GATE_DEFAULTS, **gates}
    ds, q = tid.split("/")
    tdir = Path(out) / f"{ds}__{q}"
    tdir.mkdir(parents=True, exist_ok=True)
    t0 = deps.clock()
    kit, server = deps.build_kit(ds, q, tdir / "stage")
    try:
        task_md = (kit / "TASK.md").read_text()
        sage = deps.sage(kit)
        if "ground_truth" in task_md + sage or "validate(" in task_md + sage:
            raise RuntimeError(f"leak check: intake input for {tid}")
        intake = deps.intake(task_md, sage, q_label=bool(gates["q_label"]), tools=bool(gates.get("tools")))
        front_door = float(intake.cost_usd)
        contract_txt, contract_obj = "", None
        if gates.get("tools") and gates["contract"]:  # interpretation brief: values to return, stated filters and reading choices, from the wording only
            try:
                contract_txt, c_cost, contract_obj = specialists.make_brief(
                    task_md, intake.deliverable.get("format", ""), frontdoor.call_opus)
                front_door += float(c_cost)
            except Exception as e:  # noqa: BLE001 - the worker carries on without a contract
                contract_txt = ""
                print(f"contract unavailable for {tid}: {type(e).__name__}", flush=True)
        if arm == "solo":
            size, size_reason, jev = "solo", "solo arm", 0.0
        else:
            size, size_reason, jev = deps.choose_size(intake)
        cap = CAPS[size]
        base_prompt = quorum.objective(kit, q_label=bool(gates["q_label"]), tools=bool(gates.get("tools")),
                                         contract=bool(gates["contract"]), gate=bool(gates["stage_gate"]))
        prompt = base_prompt
        if contract_txt:
            prompt += ("\n\nINTERPRETATION BRIEF (written from the question's wording alone, before any data; follow it unless "
                       f"the data shows it impossible):\n{contract_txt}")
        if intake.deliverable.get("format"):
            prompt += f"\n\nANSWER FORMAT: {intake.deliverable['format']}"
        acs = acceptance(intake)
        if gates.get("tools"):  # the shape the verify tool checks the worker's candidate against
            (kit / "DELIVERABLE.json").write_text(json.dumps({"format": intake.deliverable.get("format", "")}))
            (kit / "CONTRACT.txt").write_text(contract_txt)
            if gates["stage_gate"]:
                (kit / "STAGE_GATE").write_text("query tools stay locked until sage has run\n")
        ctx = {"deliverable": intake.deliverable, "acs": acs}  # a completeness audit may widen both for repeats
        plan = solo_plan(intake.plan) if size == "solo" and intake.plan else intake.plan
        attempts, mu, numbers = [], threading.Lock(), iter(range(1, 100))

        def spent():
            with mu:
                return front_door + jev + audit_cost + sum(a["cost_usd"] for a in attempts)

        def call(share: float, objective: str) -> dict:
            with mu:
                n = next(numbers)
            body = quorum.objective_body(f"SQ {arm} {tid} a{n} ({Path(out).name})", objective, size=size,
                                         deliverable=ctx["deliverable"], acs=ctx["acs"], plan=plan,
                                         cap_usd=_cents(share), wall_s=wall_s)
            try:
                r = deps.run_attempt(Path(out).name, tid, n, body, kit, server, wall_s + 600)
                rec = {k: v for k, v in r.items() if k not in ("rendering", "fallback_rendering", "evidence")}
                rec["cost_usd"] = float(r.get("cost_usd") or 0.0)
                rec["staged"] = bool(r.get("staged"))
                rec["infra_loss"] = str(r.get("infra_loss") or "")  # staged, but the sandbox/session died
                rec["ok"] = rec["staged"] and bool((r.get("statement") or "").strip())
                # no report statement, but the answer claim exists (retracted when its gate failed): a candidate
                # that never passed the gate
                rec["fallback"] = (rec["staged"] and not rec["ok"]
                                   and bool((r.get("fallback_statement") or "").strip()))
                if rec["fallback"]:
                    rec["gate_pass"] = False
                rendering = (r.get("fallback_rendering") if rec["fallback"] else r.get("rendering")) or ""
                # the worker's own evidence behind the answer; the report rendering is only the kernel's stub
                rendering = (r.get("evidence") or "").strip() or rendering
            except Exception as e:  # noqa: BLE001 - infrastructure failure: no result, retried once
                # AttemptFailed carries what the run spent; it counts against the cap like any other spend
                rec = {"run_id": getattr(e, "run_id", None), "staged": False, "infra_loss": "", "statement": "",
                       "gate_pass": False, "cost_usd": float(getattr(e, "cost_usd", 0.0) or 0.0), "ok": False,
                       "fallback": False, "error": f"{type(e).__name__}: {e}"}
                rendering = ""
            rec["attempt"] = n
            rec["red_flag"] = bool(ag.red_flags(rendering))
            with mu:
                attempts.append(rec)
            return {"rec": rec, "evidence": rendering[:EVIDENCE_CHARS]}

        def slot(share: float, objective: str = prompt):
            """One attempt -> {rec, evidence} of the answering try (statement or fallback), or None. Only an
            infrastructure failure (never staged, or staged but its sandbox/session died before any claim) is
            retried, once; a staged attempt that ended normally without a statement or a fallback is a real failure,
            not retried."""
            if _cents(share) <= 0:  # nothing left of the cap to launch a run with
                return None
            first = call(share, objective)
            if _usable(first["rec"]):
                return first
            if first["rec"]["staged"] and not first["rec"]["infra_loss"]:
                return None  # ran to a normal end without a deliverable: a scored failure, not infra
            rest = share - first["rec"]["cost_usd"]
            if _cents(rest) <= 0:
                return None
            again = call(rest, objective)
            return again if _usable(again["rec"]) else None

        question = task_md.strip()
        candidates, repeats, p_support, trigger, skipped = [], 0, None, None, None
        audit_res, audit_cost = None, 0.0
        mode = gates["repeat_trigger"]
        large = data_bytes(kit) > LARGE_BYTES

        def candidate(got: dict) -> dict:
            rec = got["rec"]
            answer = _answer(rec)
            return {"candidate_id": f"attempt-{rec['attempt']}", "attempt": rec["attempt"],
                    "run_id": rec.get("run_id"), "view_path": rec.get("view_path"),
                    "answer": answer, "evidence": got["evidence"], "fallback": rec["fallback"],
                    "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "evidence_sha256": hashlib.sha256(got["evidence"].encode()).hexdigest(),
                    "verification": rec.get("verification") or {"state": "missing", "scope": "format"},
                    "gate_pass": bool(rec.get("gate_pass")), "red_flags": ag.red_flags(got["evidence"])}

        with LargeTaskLock(deps.lock_path) if large else _NoLock():
            one = slot(cap - spent())
            if one:
                rec = one["rec"]
                candidates.append(candidate(one))
                flags = candidates[0]["red_flags"]
                repeat_prompt = prompt
                if gates.get("single_attempt"):  # generic mode: the one attempt is the answer; no repeats or selection
                    again = False
                elif mode == "jev":
                    p_support, c = deps.support_prob(question, candidates[0]["answer"], one["evidence"])
                    jev += c
                    again = ag.should_repeat(bool(rec.get("gate_pass")), p_support, threshold=gates["threshold"])
                elif mode == "red_flag":  # the worker's own RED FLAG note, or a failed gate; Jev is not asked
                    again = ag.should_repeat(bool(rec.get("gate_pass")), None) or bool(flags)
                    if flags:
                        note = " | ".join(flags)[:RED_FLAG_REPEAT_CHARS]
                        repeat_prompt = prompt + RED_FLAG_REPEAT.format(flags=note)
                else:  # gate_only: Jev is not asked
                    again = ag.should_repeat(bool(rec.get("gate_pass")), None)
                if gates.get("audit", True):  # label-free: question wording vs declared format vs answer
                    audit_res, audit_cost = audit.completeness(
                        question, intake.deliverable.get("format", ""), candidates[0]["answer"], one["evidence"])
                    if audit_res and not audit_res["complete"]:
                        repeat_prompt += AUDIT_REPEAT.format(missing="; ".join(audit_res["missing"]))
                        fmt = audit_res["revised_format"]
                        if fmt:
                            ctx["deliverable"] = dict(intake.deliverable, format=fmt)
                            ctx["acs"] = [{"id": intake.deliverable.get("ac") or "AC-1",
                                           "text": f"The statement is the final answer only, in this format: {fmt}.",
                                           "check": frontdoor.DEFAULT_CHECK_CMD}]
                            repeat_prompt = repeat_prompt.replace(
                                f"ANSWER FORMAT: {intake.deliverable.get('format', '')}", f"ANSWER FORMAT: {fmt}")
                        again = True
                if again:
                    trigger = ("gate failed" if not rec.get("gate_pass") else
                               "audit incomplete" if audit_res and not audit_res["complete"] else
                               "red flag" if mode == "red_flag" else f"support p={p_support:.2f}")
                    # red_flag mode keeps the selection call's budget out of what the repeats share
                    remaining = cap - spent() - (ag.ADJUDICATE_BUDGET_USD if mode == "red_flag" else 0.0)
                    repeats = ag.plan_repeats(remaining, rec["cost_usd"])
                    if mode == "red_flag":
                        # Estimated first-attempt cost is not a minimum useful retry cost.
                        # Keep the selector reserve and fund one shorter retry when necessary.
                        repeats = min(repeats, max(0, int((remaining + 1e-9) / MIN_REPEAT_USD)))
                        if repeats == 0 and _cents(remaining) >= MIN_REPEAT_USD:
                            repeats = 1
                    if repeats:
                        # large data: one staged sandbox at a time, so repeats run one after another
                        with cf.ThreadPoolExecutor(1 if large else repeats) as ex:
                            for got in ex.map(slot, [remaining / repeats] * repeats, [repeat_prompt] * repeats):
                                if got:
                                    candidates.append(candidate(got))
                    else:
                        skipped = f"cap: ${remaining:.2f} left, attempt 1 cost ${rec['cost_usd']:.2f}"
        adjudication, adj_cost, how, selected_idx = None, 0.0, None, None
        if candidates:  # normal and fallback candidates alike; fallback-only answers are scored too
            # red_flag mode: disagreement or any red flag goes to one Opus call that selects a candidate (never writes
            # an answer); a single candidate has nothing to select from
            why = [w for w, hit in (("disagree", ag.disagree(candidates)),
                                    ("red flag", any(x["red_flags"] for x in candidates))) if hit]
            identical = ag.identical_agreement(candidates)
            if mode == "red_flag" and identical is not None and why:
                answer, how, _ = identical
                selected_idx = identical.candidate_index
                adjudication = {"why": why, "index": selected_idx, "cost_usd": 0.0, "error": None,
                                "skipped": "Every candidate has the identical answer; selection cannot change it."}
            elif mode == "red_flag" and len(candidates) > 1 and why:
                adjudication = {"why": why, "index": None, "reason": None, "cost_usd": 0.0, "error": None}
                try:
                    idx, reason, adj_cost = deps.adjudicate(question, candidates)
                    if not (isinstance(idx, int) and 0 <= idx < len(candidates)):
                        raise ValueError(f"selected index {idx!r} is not a candidate")
                    adjudication.update(index=idx, reason=reason, cost_usd=float(adj_cost))
                    answer, how = candidates[idx]["answer"], "opus_select"
                    selected_idx = idx
                except Exception as e:  # noqa: BLE001 - selection unavailable: normal agreement decides
                    known_cost = getattr(e, "cost_usd", adj_cost or None)
                    adj_cost = float(known_cost or 0.0)
                    adjudication.update(cost_usd=known_cost, cost_known=known_cost is not None)
                    adjudication["error"] = f"{type(e).__name__}: {e}"
            if how is None:
                selection = deps.agree(question, candidates)
                answer, how, c = selection
                selected_idx = getattr(selection, "candidate_index", None)
                if selected_idx is None:  # legacy injected selectors have no index; never invent an ambiguous one
                    matches = [i for i, x in enumerate(candidates) if x["answer"] == answer]
                    selected_idx = matches[0] if len(matches) == 1 else None
                jev += c
            verdict = deps.score(ds, q, answer)
            reward, reason = verdict["reward"], verdict.get("reason")
        elif any(a["staged"] and not a.get("infra_loss") for a in attempts):
            # staged and ended normally without a deliverable: a real failure, scores 0
            answer, reward, reason = "", 0, NO_DELIVERABLE
        elif any(a.get("infra_loss") for a in attempts):  # every staged attempt lost its sandbox/session: unscored
            answer, reward, reason = "", None, INFRA_LOST
        else:  # never staged (infrastructure failure): not scored as a wrong answer
            answer, reward, reason = "", None, "no staged attempt with an answer"
        quorum_cost = sum(a["cost_usd"] for a in attempts)
        sig = signals(kit, answer, candidates[0]["evidence"] if candidates else "", contract_obj,
                      intake.deliverable.get("format", ""), bool(gates.get("tools")))
        res = {"arm": arm, "task": tid, "size": size, "size_reason": size_reason, "cap_usd": cap,
               "attempts": sorted(attempts, key=lambda a: a["attempt"]), "repeats": repeats,
               "dropped_checks": list(getattr(intake, "dropped_checks", None) or []),
               "example_answer": getattr(intake, "example_answer", ""),
               "check_self_test": getattr(intake, "self_test", ""),
               "gates": gates, "repeat_trigger": trigger, "repeat_skipped": skipped, "p_support": p_support,
               "candidates": [dict(x, red_flag=bool(x["red_flags"])) for x in candidates],
               "selection": ({k: candidates[selected_idx][k] for k in
                              ("candidate_id", "attempt", "run_id", "answer_sha256", "evidence_sha256", "verification")}
                             if selected_idx is not None else None),
               "adjudication": adjudication, "agree_how": how, "answer": answer,
               "reward": reward, "score_reason": reason,
               "audit": audit_res, "signals": sig, "contract": contract_txt,
               "cost": {"front_door": front_door, "quorum": quorum_cost, "jev": jev, "adjudicate": adj_cost,
                        "audit": audit_cost,
                        "complete": not (adjudication and adjudication.get("cost_known") is False),
                        "total": front_door + quorum_cost + jev + adj_cost + audit_cost},
               "seconds": round(deps.clock() - t0)}
        _write(tdir / "result.json", res)
        return res
    finally:
        shutil.rmtree(tdir / "stage", ignore_errors=True)


def signals(kit: Path, answer: str, evidence: str, contract: dict, fmt: str, tools: bool) -> dict:
    """Label-free per-trial signals: which tools ran, what verify said, lint of the answer, contract coverage. Reads the
    trial's service log and the answer only; no gold, no score. For reporting and offline analysis, never selection."""
    from sq import jlabel_proxy, shapelint
    out = {"tools": tools}
    log = []
    try:
        h = (kit / ".jlabel_hash").read_text().strip()
        log = [json.loads(x) for x in (jlabel_proxy.TOKENS / f"{h}.jsonl").read_text().splitlines()]
    except (OSError, ValueError):
        pass
    req = lambda r: (r.get("request") or {}).get("tool")  # noqa: E731
    out["locate_calls"] = sum(1 for r in log if req(r) == "locate")
    verifies = [r for r in log if req(r) == "verify"]
    out["verify_calls"] = len(verifies)
    notes = [str((r.get("request") or {}).get("how", "")) for r in log if req(r) == "note"]
    out["joincheck_calls"] = sum(1 for n in notes if n.startswith("joincheck"))
    out["bounds_calls"] = sum(1 for n in notes if n.startswith("bounds"))
    out["judge_calls"] = sum(1 for r in log if req(r) == "judge")
    out["label_calls"] = sum(1 for r in log if req(r) is None)
    out["tool_errors"] = sum(1 for r in log if isinstance((r.get("response") or {}).get("error"), str))
    raw = [(r.get("response") or {}).get("raw") or {} for r in verifies]
    out["verify_verdicts"] = [x.get("verdict") for x in raw]
    out["verify_last"] = out["verify_verdicts"][-1] if raw else None
    out["verify_flags"] = sum(len(x.get("missing_values") or []) + len(x.get("shape_problems") or []) for x in raw)
    asks = (contract or {}).get("asks") or []
    out["contract_asks"] = len(asks)
    out["contract_ids_in_evidence"] = sum(1 for a in asks if re.search(rf"\b{re.escape(a['id'])}\b", evidence or ""))
    out["lint"] = shapelint.lint(answer, fmt, len(asks))
    out["red_flag_note"] = bool(ag.red_flags(evidence or ""))
    out["answer_empty"] = not (answer or "").strip()
    return out


def arm_results(arm_dir: Path) -> list:
    return [json.loads(p.read_text()) for p in sorted(Path(arm_dir).glob("*/result.json"))]


def scored(rows: list) -> list:
    """Results with a reward (guard.check needs a number; infrastructure failures have None)."""
    return [r for r in rows if r.get("reward") is not None]


def _strat(per_task: dict) -> float:
    by_ds = {}
    for t, v in per_task.items():
        by_ds.setdefault(t.split("/", 1)[0], []).append(v)
    return 100.0 * sum(sum(v) / len(v) for v in by_ds.values()) / len(by_ds)


def summary(rows: list, base: dict, total: int) -> dict:
    """Progress numbers for one arm: counts, Pass@1 raw and stratified, Scrybe alone on the same tasks, cost, mix."""
    ok = scored(rows)
    mine = {r["task"]: float(r["reward"]) for r in ok}
    matched = {t: v for t, v in mine.items() if t in base}
    sizes = {}
    for r in rows:
        sizes[r["size"]] = sizes.get(r["size"], 0) + 1
    cost = sum(r["cost"]["total"] for r in rows)
    s = {"done": len(rows), "total": total, "scored": len(ok), "passed": sum(mine.values()),
         "raw": 100.0 * sum(mine.values()) / len(ok) if ok else None,
         "strat": _strat(mine) if mine else None,
         "strat_matched": _strat(matched) if matched else None,
         "base_matched": _strat({t: base[t] for t in matched}) if matched else None,
         "matched": len(matched), "cost": cost, "mean_cost": cost / len(rows) if rows else None,
         "sizes": sizes, "repeated": sum(1 for r in rows if r.get("repeats"))}
    s["gap"] = s["strat_matched"] - s["base_matched"] if matched else None
    return s
