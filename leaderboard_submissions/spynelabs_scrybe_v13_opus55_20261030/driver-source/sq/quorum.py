"""Quorum (multica server) client: API access, kit staging into a run's sandbox, objective bodies, one attempt.

Shared by scripts/dab-quorum.py (DAB driver) and scripts/scrybe-quorum.py (front-door arms).
Env: MULTICA_API (http://localhost:8080), MULTICA_WS (workspace id), QUORUM_JWT_FILE (runs/quorum-dab/.jwt)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("DAB_DB_ACCOUNTS", "per-dataset")
os.environ.setdefault("SCRYBE_SEMANTIC", "0")  # no LLM-written semantic notes; the Sage profile is code-measured
import dab_common as dc  # noqa: E402
from sq import overlay, provider  # noqa: E402

API = os.environ.get("MULTICA_API", "http://localhost:8080")
WS = os.environ.get("MULTICA_WS", "5ef53d9c-52d8-421b-bed0-b56c385a7218")
JWT = Path(os.environ.get("QUORUM_JWT_FILE", REPO / "runs" / "quorum-dab" / ".jwt"))
BUNDLE = REPO / "runs" / "bundles" / "7409ad29afc6d9c98ffa" / "install"  # the untuned DAB submission's bundle
SITE = REPO / "runs" / "quorum-dab" / "site"  # linux/aarch64 cp311 pandas, duckdb, pymongo, psycopg2, pyarrow
KIT = "/home/ide/dab"
READY_WAIT_S = 180  # how long a worker polls for KIT/READY before giving up (staging can lag the session start)
TERMINAL = {"solved", "stuck", "aborted", "failed", "stopped", "cancelled"}
PG_NET = "dab-int"
SKIP_SKILLS = {"dbt-projects.md", "dbt-testing-and-docs.md", "incremental-snapshots.md", "inventory-costing.md",
               "snowflake.md"}  # project-workflow skills; the analysis workflow never lists them either


class APIError(RuntimeError):
    """The multica server answered with an HTTP error; the message carries the code and the response body."""
    def __init__(self, code: int, method: str, path: str, body: str):
        super().__init__(f"HTTP {code} {method} {path}: {body}")
        self.code, self.body = code, body


def api(method, path, body=None, timeout=60):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + JWT.read_text().strip())
    req.add_header("X-Workspace-ID", WS)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            text = ""
        raise APIError(e.code, method, path, text) from e
    return json.loads(raw) if raw else {}


def sh(*cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def psql(sql):
    return sh("docker", "exec", "multica-postgres-1", "psql", "-U", "multica", "-d", "multica", "-Atc", sql).stdout.strip()


GATE = """if [ -f {kit}/STAGE_GATE ] && [ ! -f /home/ide/dab-work/.stage/locate ]; then
  echo "LOCKED: stage 1 (LOCATE) first. Run {kit}/bin/sage '<what you need to find out>' and read what it returns; this tool unlocks after it." >&2
  exit 3
fi
"""
WRAP = """#!/bin/sh
export PYTHONPATH={kit}/site:{kit}/lib SCRYBE_DB_CONFIG={kit}/db_config.json SCRYBE_SAGE_FILE={kit}/sage/layer.json
export SCRYBE_WORKDIR=/home/ide/dab-work
mkdir -p /home/ide/dab-work 2>/dev/null
exec python3 {kit}/lib/{tool} "$@"
"""
JLABEL_URL = os.environ.get("JLABEL_URL", "http://host.docker.internal:18765")
JLABEL_WRAP = """#!/bin/sh
export PYTHONPATH={kit}/site:{kit}/lib JLABEL_URL={url} JLABEL_TOKEN={token}
export SCRYBE_WORKDIR=/home/ide/dab-work
mkdir -p /home/ide/dab-work/results 2>/dev/null
exec python3 {kit}/lib/jlabel.py "$@"
"""
SPEC_WRAP = """#!/bin/sh
export PYTHONPATH={kit}/site:{kit}/lib JLABEL_URL={url} JLABEL_TOKEN={token} TOOL_NAME={name} KIT_HOME={kit}
exec python3 {kit}/lib/specialist.py "$@"
"""
JOIN_WRAP = """#!/bin/sh
export PYTHONPATH={kit}/site:{kit}/lib SCRYBE_DB_CONFIG={kit}/db_config.json KIT_HOME={kit} JLABEL_URL={url} JLABEL_TOKEN={token}
exec python3 {kit}/lib/joincheck.py "$@"
"""
BOUNDS_WRAP = """#!/bin/sh
export PYTHONPATH={kit}/site:{kit}/lib JLABEL_URL={url} JLABEL_TOKEN={token}
exec python3 {kit}/lib/bounds.py "$@"
"""
PYWRAP = """#!/bin/sh
{gate}export PYTHONPATH={kit}/site:{kit}/lib SCRYBE_DB_CONFIG={kit}/db_config.json
exec python3 "$@"
"""


def install_tools(kit: Path):
    """Copy the real query-tool libs into kit/lib and write the bin/ wrappers."""
    hs = BUNDLE / "harness" / "scripts"
    for f in ("multidb.py", "sage_dbview.py"):
        shutil.copy2(hs / f, kit / "lib" / f)
    for tool, src in (("dbq", "dbq"), ("dbschema", "dbschema"), ("dbsnap", "dbsnap"), ("sl", "sage_dbview.py")):
        if src != "sage_dbview.py":
            shutil.copy2(hs / src, kit / "lib" / (src + ".py"))
            src = src + ".py"
        p = kit / "bin" / tool
        gate = GATE.format(kit=KIT) if tool in ("dbq", "dbsnap") else ""
        p.write_text(WRAP.replace("export", gate + "export", 1).format(kit=KIT, tool=src))
        p.chmod(0o755)
    p = kit / "bin" / "dpython"
    p.write_text(PYWRAP.format(kit=KIT, gate=GATE.format(kit=KIT)))
    p.chmod(0o755)
    # jlabel: row labelling by Jev through a host-side service; the sandbox only holds a per-trial, label-only token
    from sq import jlabel_proxy
    token = jlabel_proxy.register()
    shutil.copy2(Path(__file__).with_name("jlabel_tool.py"), kit / "lib" / "jlabel.py")
    p = kit / "bin" / "jlabel"
    p.write_text(JLABEL_WRAP.format(kit=KIT, url=JLABEL_URL, token=token))
    p.chmod(0o755)
    # specialist tools: sage (locate), verify (independent review), joincheck (measured join keys)
    shutil.copy2(Path(__file__).with_name("tool_specialist.py"), kit / "lib" / "specialist.py")
    shutil.copy2(Path(__file__).with_name("tool_join.py"), kit / "lib" / "joincheck.py")
    shutil.copy2(Path(__file__).with_name("shapelint.py"), kit / "lib" / "shapelint.py")
    shutil.copy2(Path(__file__).with_name("tool_bounds.py"), kit / "lib" / "bounds.py")
    p = kit / "bin" / "bounds"
    p.write_text(BOUNDS_WRAP.format(kit=KIT, url=JLABEL_URL, token=token))
    p.chmod(0o755)
    for name in ("sage", "verify"):
        p = kit / "bin" / name
        p.write_text(SPEC_WRAP.format(kit=KIT, url=JLABEL_URL, token=token, name=name))
        p.chmod(0o755)
    p = kit / "bin" / "joincheck"
    p.write_text(JOIN_WRAP.format(kit=KIT, url=JLABEL_URL, token=token))
    p.chmod(0o755)
    (kit / ".jlabel_hash").write_text(jlabel_proxy._digest(token))  # host-side bookkeeping, not secret


def build_kit(ds, q, out: Path):
    """Host-side staging dir for one trial -> (kit dir, needs_server_dbs)."""
    if out.exists():
        shutil.rmtree(out)
    kit = out / "kit"
    for d in ("bin", "lib", "data", "sage", "skills"):
        (kit / d).mkdir(parents=True)
    mounts = dc.build_workspace(ds, q, kit)
    cfg = json.loads((kit / "db_config.json").read_text())
    for m in mounts:  # "<host file>:/data/<inner>:ro" -> copy to kit/data/<inner>
        src, dst, _ = m.rsplit(":", 2)
        tgt = kit / "data" / Path(dst).relative_to("/data")
        tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, tgt)
        except OSError:
            shutil.copy2(src, tgt)
    for name, c in cfg["databases"].items():
        if c.get("path", "").startswith("/data/"):
            c["path"] = KIT + "/data/" + c["path"][len("/data/"):]
    (kit / "db_config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    server = any(c["type"] in ("postgres", "mongo") for c in cfg["databases"].values())
    sage = BUNDLE / "sage" / ds / "layer.json"
    if not sage.is_file():
        sys.exit(f"no Sage view for {ds} in {BUNDLE}")
    shutil.copy2(sage, kit / "sage" / "layer.json")
    install_tools(kit)
    for s in sorted((BUNDLE / "harness" / "skills").glob("*.md")):
        if s.name not in SKIP_SKILLS:
            shutil.copy2(s, kit / "skills" / s.name)
    leak_check(kit)
    return kit, server


def leak_check(kit: Path):
    bad = ("ground_truth", "validate.py", "manual_querycode", "queries.json")
    for p in kit.rglob("*"):
        if any(b in p.name for b in bad):
            sys.exit(f"leak check: {p} must not be staged")
    for p in (kit / "TASK.md", kit / "db_config.json"):
        t = p.read_text()
        if "ground_truth" in t or "validate(" in t:
            sys.exit(f"leak check: {p}")


def keep_alive(container: str):
    """Mark the run's sandbox active. Every server on this database reaps sandboxes: one whose image tag differs from
    its own (the live server sees the headless server's sandboxes that way) is put to sleep after 5 idle minutes, and
    docker exec does not count as activity, so a working attempt would lose its container mid-run."""
    try:
        psql(f"update ide_workspace set last_active_at=now() where container_name='{container}' and deleted_at is null")
    except Exception:  # noqa: BLE001 - best effort; the harvest falls back to the volume
        pass


def stage(container, kit: Path, server: bool):
    if server:
        r = sh("docker", "network", "connect", PG_NET, container, check=False)
        if r.returncode and "already exists" not in r.stderr:
            raise RuntimeError(f"network connect: {r.stderr.strip()}")
    sh("docker", "exec", "-u", "0", container, "mkdir", "-p", "/home/ide")
    p = subprocess.Popen(["docker", "exec", "-i", "-u", "0", container, "tar", "-xf", "-", "-C", "/home/ide"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with tarfile.open(fileobj=p.stdin, mode="w|") as t:  # streamed: nothing large is written on the host
        t.add(kit, arcname="dab")
        t.add(SITE, arcname="dab/site")
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"staging tar failed: {p.stderr.read().decode()[-300:]}")
    sh("docker", "exec", "-u", "0", container, "sh", "-c",
       f"chown -R 0:0 {KIT} && chmod -R a+rX,go-w {KIT} && chmod -R a-w {KIT}/data {KIT}/sage {KIT}/db_config.json "
       f"&& touch {KIT}/READY && chmod 444 {KIT}/READY")


def unstage(container, server):
    sh("docker", "exec", "-u", "0", container, "rm", "-rf", KIT, check=False)
    if server:
        sh("docker", "network", "disconnect", PG_NET, container, check=False)


Q_LABEL_BULLET = (
    '- Labeling many rows: q_label labels rows with one shared model call per batch (same prompt and model for every row, consistent and cheap). Input JSON (a file path, or `-` to read stdin): {"labels": [..], "instructions": "..", "rows": [{"id": .., "text": ".."}], "out": "labels.csv"}; it writes id,label,confidence to out. Use it instead of labeling rows yourself; spot-check a few low-confidence rows.'
    "\n"
)


SPECIALIST_ENV = """- Specialist tools, each answers in a separate context from yours (use them at the stage named in WORK IN STAGES):
  {kit}/bin/sage "<what you need to find out>": which tables, columns, joins and recorded definitions the question needs, and which wording is ambiguous (reads the Sage profile; at most 4 calls).
  {kit}/bin/joincheck <db>.<table>.<col> <db>.<table>.<col>: measured match of two identifier columns as stored and after each simple clean-up (trim, case, digits only, zeros, prefix/suffix): share of keys and of ROWS that join on each side, fan-out (rows multiplied by the join), keys a clean-up would merge, and the unmatched keys with their closest partner.
  {kit}/bin/bounds --in <file> --agg count|sum|mean [--value col] (--status col | --p col): the answer with the uncertain rows (unmatched join rows, low-confidence labels) all out, all in and probability-weighted, so you can see whether noise changes what you report.
  {kit}/bin/verify --answer-file <file> --how-file <file>: an adversarial review of your candidate against the interpretation brief: the strongest alternative reading, filters nobody asked for, values altered from their stored form, missing values, shape (at most 3 calls). Its fix verdict is binding.
"""
STAGES = """WORK IN STAGES (each stage is one job; finish it before the next):
0. BRIEF: the INTERPRETATION BRIEF below the question was written from the wording alone, before any data was seen. It lists every value to return (A1, A2, ...), the only filters the question states, and the reading chosen for each ambiguous term by fixed rules (a definition the task gives outranks column names and conventions; the whole named set is not narrowed; format examples never decide a reading; values are returned as stored). Follow it. Where it says "decide from the data", the data decides. Deviate only when the data shows its reading impossible, and then put a line starting "DEVIATION:" in the claim's evidence with the query that shows it; a deviation never relaxes returning every value in the brief or returning values exactly as stored, and a task-given definition is never a ground for deviating.
1. LOCATE: run `sage` with the question. Read which tables, joins, recorded definitions and ambiguous wordings it reports; confirm anything you rely on with `sl` or one query.
2. DEFINE: for each ambiguous wording, look in the data for what decides it (a column, a lookup, a description, how values are stored) and fix one reading (the brief already fixed those it could). Write down, in one line each: the grain of a row, the filters, the aggregation level, and every value in the brief (A1, A2, ...).
3. JOIN: when two sources are joined on identifiers, run `joincheck` on the key columns and use the clean-up it finds before joining. Read the row shares, not only the key shares. A fan-out above 1 means the join multiplies rows: aggregate to one row per key first. If rows stay unmatched, look at the unmatched keys it prints; if you cannot explain them, mark the unmatched rows `unknown` in your result and run `bounds` to see whether the answer depends on them.
4. COMPUTE: query. After every join or filter note the rows before and after and the share of the declared population that survives; a surviving share under about 20%, or a filter the question states that removes nothing, is a finding to explain with a query (does the entity appear in another table, does another column carry the term?) before you go on. Label free text with `jlabel` instead of reading rows (it sends the rows it is unsure about to an independent judge and gives the corrected counts in label_final / yes_final, prints a RANGE per label and, from 60 rows, the judge's agreement on a sample: below 90% sharpen --describe and rerun; below 70% do not rely on the labels). When the answer depends on counts of labelled or joined rows, run `bounds` on the result file (--p p_<label> or --p p_yes) and report the reading the evidence supports, naming in a RED FLAG line any answer that moves across the range. Write files only under /home/ide/dab-work.
5. VERIFY: write the candidate answer and your account to files with a QUOTED heredoc (cat > /home/ide/dab-work/how.txt <<'EOF' ... EOF; text inside double quotes on the command line is altered by the shell, $20 would vanish) and run `verify --answer-file ... --how-file ...`. In the account give: each filter you applied and how many rows it removed, the share of the declared population that survives every join and filter, one line `READING-CHECK: <term> -> <option> -> <result>` for EVERY reading the brief lists (compute each; a reading with no result is a gap), and the returned values exactly as stored. Its verdict is binding. When it says fix: on altered values, unrequested filters, missing values or uncomputed readings, apply it (a DEVIATION line cannot override these); on anything else either apply it or answer the item with a query result in the claim's evidence ("VERIFY-REPLY: item -> result"). An alternative reading it names that you have not ruled out with data is not ruled out. Never add a filter or population restriction because a reviewer suggests it; only remove restrictions the question does not state. Then recompute the final number or list once by an independent query.
6. ANSWER: return every value in the brief (A1, A2, ...), in the declared shape, each value exactly as stored (never shortened or reformatted to fit a separator). In the claim's evidence list each id with the value you found for it.

"""


def stages_text(contract: bool = True, gate: bool = True) -> str:
    """The stage protocol; without the interpretation brief the text drops what refers to it."""
    t = STAGES
    if not contract:
        t = "\n".join(l for l in t.split("\n") if not l.startswith("0. BRIEF"))
        t = t.replace("and every value in the brief (A1, A2, ...).", "and every value the question asks to be returned.")
        t = t.replace("return every value in the brief (A1, A2, ...), in the declared shape, each value exactly as stored",
                      "return every value asked, in the declared shape, each value exactly as stored")
        t = t.replace(" In the claim's evidence list each id with the value you found for it.", "")
    if gate:
        t = t.replace("1. LOCATE:", "1. LOCATE (the query tools stay locked until this has run):")
    return t


def objective(kit: Path, q_label: bool = True, tools: bool = False, contract: bool = True, gate: bool = True) -> str:
    """Task prompt for a Quorum run: the kit's TASK.md plus the sandbox environment (databases, tools, Sage, skills)."""
    task = (kit / "TASK.md").read_text().strip()
    skills = ", ".join(sorted(p.stem for p in (kit / "skills").glob("*.md")))
    dbs = json.loads((kit / "db_config.json").read_text())["databases"]
    dbl = ", ".join(f"{n} ({c['type']})" for n, c in dbs.items())
    ql = Q_LABEL_BULLET if q_label else ""
    return f"""Answer a data question from the databases provided in this sandbox.

{task}

ENVIRONMENT (in this sandbox at {KIT}, outside the repository):
- Readiness: the kit is ready only once {KIT}/READY exists. If it is missing, poll for it (every 10 s) for up to
  {READY_WAIT_S} s before giving up; until then a missing or unreadable {KIT} means setup is still in progress, not
  that the task is blocked.
- Databases (read-only), configured in {KIT}/db_config.json: {dbl}.
- Query tools: {KIT}/bin/dbq <db> "<SQL, or Mongo JSON {{collection, filter|pipeline}}>" prints the result and saves the
  full result as parquet under /home/ide/dab-work/results; {KIT}/bin/dbschema [<db>] lists tables and columns; {KIT}/bin/dbsnap gives a
  compact snapshot of every database. Python with pandas, duckdb, pymongo and psycopg2: {KIT}/bin/dpython script.py,
  where `from multidb import q; df = q("<db>", "<query>")` returns a DataFrame.
- Sage: {KIT}/bin/sl overview | tables | table <t> | column <t>.<c> | joins [<t>] | search <term> is an offline,
  question-blind profile of these databases (row counts, null and distinct counts, ranges, top values, candidate keys
  and measured join match rates). {KIT}/SCHEMA.md is a schema snapshot.
- Labelling: {KIT}/bin/jlabel labels the rows of a saved result (parquet/csv/json) with a fast calibrated classifier (Jev). \
You define the categories (or a yes/no condition) and the question. Prefer it to judging many rows by reading, and check the \
rows it reports as least certain. Label only the rows the question needs (try a small sample first) and reuse a saved result instead of labelling the same rows again. Large inputs take several calls: when it prints PARTIAL, run the same command again. `{KIT}/bin/jlabel --help` shows usage.
{SPECIALIST_ENV.format(kit=KIT) if tools else ""}- Reference skills (read what the work needs): {KIT}/skills/*.md: {skills}.
{ql}- Use only these databases and files. Do not use the network (no web fetches, no package installs).

{stages_text(contract, gate) if tools else ""}WORKING METHOD (before you answer):
- A tie in a ranking or selection, a ranking value that is the same for every candidate, or a join that matches far fewer rows than expected is a red flag: re-check your interpretation of the question and the join.
- List all candidates that satisfy the question before choosing one. Never break a tie or settle an ambiguity by a sort key or a reading the question does not state.
- If, after checking the data, the question still admits several readings that give different answers, or the result has a tie the question does not resolve, report it: the claim's evidence must include a line starting "RED FLAG:" that names the readings or tied candidates and the answer under each. The claim statement is the answer under the reading best supported by the question's wording and the data.
- Recompute the final number or list once by an independent query.
- Put the key evidence in the claim's evidence, as text (not a file path): the query, row counts, and the final values.

DELIVERABLE: an analysis claim whose statement is the final answer only. Return the final answer only. Do not
include extra text, explanation, or formatting.""" + overlay.text("worker")


SLOTS = {"solo": 1, "team": 3, "swarm": 12}


def objective_body(title: str, prompt: str, *, size: str, deliverable: dict, acs: list, plan: list,
                   cap_usd: float, wall_s: int, model: str = None) -> dict:
    """Request body for POST /api/ship/objectives: one Quorum run of the given size (solo|team|swarm)."""
    if size not in SLOTS:
        raise ValueError(f"size must be one of {sorted(SLOTS)}, got {size!r}")
    model = model or provider.model("opus")  # OpenRouter's Sonnet 5.5 once Anthropic credits ran out
    charter = (f"size: {size}\nlineages:\n  - family: anthropic\n    model: {model}\n    harness: opencode\n"
               f"    slots: {SLOTS[size]}\n")
    return {"title": title, "prompt": prompt,
            "config": {"engine": "quorum", "merge_solution": False,
                       "quorum": {"acceptance_criteria": acs, "deliverable": deliverable, "plan": plan,
                                  "charter_yaml": charter},
                       "budget": {"spend_cap_usd": cap_usd, "wall_clock_sec": wall_s,
                                  "concurrency": SLOTS[size]}}}


def _answer_claim(view: dict):
    """Resolve one claim for both evidence and verification of the returned answer."""
    stmt = ((view.get("report") or {}).get("statement") or "").strip()
    if not stmt:
        body = _fallback_body(view)
        if body is None:
            return None
        return next((c for c in view.get("claims") or [] if c.get("body") is body), None)
    bounties = view.get("bounties") or []
    deliverable = {b.get("bounty_id") for b in bounties if b.get("deliverable")}
    if not deliverable and len(bounties) == 1:
        deliverable = {bounties[0].get("bounty_id")}
    pick = [c for c in view.get("claims") or []
            if (c.get("body") or {}).get("claim_type") in ("analysis", "synthesis")
            and (c["body"].get("statement") or "").strip() == stmt
            and (not deliverable or c["body"].get("bounty_id") in deliverable)]
    # The kernel's later synthesis repeats the answer without the worker's evidence.
    pick = [c for c in pick if _summaries(c["body"])] or pick
    return max(pick, key=lambda c: c.get("created_at") or "") if pick else None


def answer_verification(view: dict) -> dict:
    """Preserve the gate and its claim identity. A format pass is not semantic proof."""
    claim = _answer_claim(view)
    if not claim:
        return {"state": "missing", "scope": "format", "claim_id": None, "gate": None}
    gate = claim.get("gate") or {}
    refuted = claim.get("status") in ("refuted", "rejected", "retracted") or any(
        v.get("cid") == claim.get("cid") and v.get("outcome") == "fail" and v.get("verifier_kind") != "gate"
        for v in view.get("verdicts") or [] if claim.get("cid"))
    state = ("refuted" if refuted else "missing" if not gate else "failed" if gate.get("pass") is False
             else "passed" if gate.get("pass") is True and gate.get("strong") is True else "weak")
    return {"state": state, "scope": "format", "claim_id": claim.get("cid"),
            "claim_status": claim.get("status"), "gate": dict(gate)}


def deliverable_gate_pass(view: dict) -> bool:
    """Only a strong passing gate on the answer's own claim counts as verified."""
    return answer_verification(view)["state"] == "passed"


def fallback_claim(view: dict) -> tuple[str, str]:
    """(statement, rendering) of the deliverable claim when the report has no statement, else ("", "").

    A solo run whose deliverable gate fails retracts the answer claim and ends with an empty report statement; the
    claim's statement is still a candidate answer. Only claims on the deliverable bounty count (with no bounty marked
    deliverable, a lone bounty is it): an intermediate claim is never an answer. Claims refuted by verification
    (status refuted/rejected, or a non-gate fail verdict; such claims are also retracted) are skipped, so what
    remains is a claim retracted by its own failed gate. Picks the latest analysis/synthesis claim with a non-empty statement. The
    rendering is the claim's own, else its statement plus its evidence summaries."""
    if ((view.get("report") or {}).get("statement") or "").strip():
        return "", ""
    body = _fallback_body(view)
    if body is None:
        return "", ""
    stmt = body["statement"].strip()
    rendering = (body.get("rendering") or "").strip()
    if not rendering:
        notes = _summaries(body)
        rendering = stmt + ("\n\nEvidence:\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
    return stmt, rendering


def _summaries(body: dict) -> list:
    return [e["summary"] for e in body.get("evidence") or [] if isinstance(e, dict) and e.get("summary")]


def _fallback_body(view: dict):
    """Body of the claim fallback_claim answers from (report statement ignored), or None."""
    bounties = view.get("bounties") or []
    deliv = {b.get("bounty_id") for b in bounties if b.get("deliverable") is True}
    if not deliv and len(bounties) == 1:
        deliv = {bounties[0].get("bounty_id")}
    # the failed gate is itself a verdict (verifier_kind "gate"); only a verifier's fail verdict refutes the claim
    refuted = {v.get("cid") for v in view.get("verdicts") or []
               if v.get("outcome") == "fail" and v.get("verifier_kind") != "gate"}
    pick = [c for c in view.get("claims") or []
            if (c.get("body") or {}).get("claim_type") in ("analysis", "synthesis")
            and (c["body"].get("statement") or "").strip()
            and c["body"].get("bounty_id") in deliv
            and c.get("status") not in ("refuted", "rejected") and not (c.get("cid") and c["cid"] in refuted)]
    if not pick:
        return None
    return max(pick, key=lambda c: c.get("created_at") or "")["body"]


def answer_evidence(view: dict) -> str:
    """The worker's evidence (its claim's evidence summaries, one "- " line each) behind the run's answer, or "".

    With a report statement: the latest analysis/synthesis claim stating it that carries evidence (the kernel's own
    synthesis claim repeats the statement with no evidence, and the report rendering is the kernel's stub). Without
    one: the claim fallback_claim answers from."""
    claim = _answer_claim(view)
    body = claim["body"] if claim else None
    return "\n".join(f"- {n}" for n in _summaries(body)) if body else ""


INFRA_STATUSES = {"aborted", "failed"}  # the run ended by an error or abort, not by a normal stop
# an assignment that ended because its sandbox or agent session died, not because the worker finished or gave up
_INFRA_REASON = re.compile(r"session error|session failed|no such container|container \S* ?is not running", re.I)


def infra_loss(view: dict, status: str = "", *, stopped_by_driver: bool = False) -> str:
    """Why a staged run produced nothing because its sandbox or session died, or "" when it ended normally.

    Never infrastructure when the driver stopped the run itself (its deadline; the server then reports "aborted"),
    or when the run left a claim or a report statement. Otherwise infrastructure when the run (or its report) ended
    with an error/abort status, or when every one of the run's assignments was abandoned with a session/container
    error. The kernel itself stops such a run normally ("diminishing_returns") after its assignments fail, so the run
    status alone does not show it. Container liveness is deliberately not probed: a transient probe failure would turn
    a real scored failure into an unscored one."""
    if stopped_by_driver or view.get("claims") or ((view.get("report") or {}).get("statement") or "").strip():
        return ""
    for st in (status, (view.get("run") or {}).get("status"), (view.get("report") or {}).get("status")):
        if st in INFRA_STATUSES:
            return f"run ended with status {st}"
    assignments = sorted(view.get("assignments") or [], key=lambda a: a.get("started_at") or "")
    if assignments and all(a.get("status") == "abandoned" and _INFRA_REASON.search(a.get("reason") or "")
                           for a in assignments):
        last = (assignments[-1].get("reason") or "").strip()
        return f"{len(assignments)} of {len(assignments)} assignments lost; last: {last[:300]}"
    return ""


class AttemptFailed(RuntimeError):
    """A Quorum run that produced no result (ended before staging, or staging/polling failed). Carries what the run
    spent so far (from its report when available, else 0.0) so callers can count it against the task cap."""

    def __init__(self, msg: str, *, cost_usd: float = 0.0, run_id: str = None):
        super().__init__(msg)
        self.cost_usd = float(cost_usd or 0.0)
        self.run_id = run_id


def _report_cost(rq: dict) -> float:
    try:
        return float(((rq or {}).get("report") or {}).get("cost_usd") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _server_spend(rq: dict) -> float:
    """Largest spend the server recorded for this run (spend buckets, or the sandbox figure)."""
    run = (rq or {}).get("run") or {}
    buckets = run.get("spend")
    total = sum(float(v or 0) for v in buckets.values()) if isinstance(buckets, dict) else 0.0
    return max(total, float(run.get("sandbox_seen_usd") or 0))


def run_attempt(name: str, tid: str, attempt: int, body: dict, kit: Path, server: bool, deadline_s: int, *,
                stager=None) -> dict:
    """Submit body, stage kit into the run's sandbox, wait for the run, fetch its report, delete the sandbox.
    `stager` (optional) replaces the DAB kit staging: an object with stage(container, kit, server),
    harvest(container, out_dir) (called once the run ended, before unstaging; its return value is stored as
    "harvest") and unstage(container, server).
    With an empty report statement, a deliverable claim's statement comes back as fallback_statement (gate_pass False).
    A staged run whose sandbox or session died before any claim comes back with infra_loss set (see infra_loss).

    Raises AttemptFailed (no result, with the run's spend) when the run ended before the kit was staged or the
    attempt failed after the run was created."""
    out = REPO / "runs" / name / (tid.replace("/", "__") + f"__r{attempt}")
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "driver.log", "a")

    def L(*m):
        print(time.strftime("%H:%M:%S"), *m, file=log, flush=True)

    t0 = time.time()
    run_id = api("POST", "/api/ship/objectives", body)["run_id"]
    L("run", run_id)
    container, staged, info, rq, stage_errors, stopped_by_driver = None, False, {}, {}, 0, False
    harvested = None
    deadline = t0 + deadline_s
    try:
        while True:
            if not staged:
                c = psql(f"select container_name from ide_workspace where ship_run_id='{run_id}' and deleted_at is null "
                         f"and container_name<>'' limit 1")
                if c and sh("docker", "inspect", "-f", "{{.State.Running}}", c, check=False).stdout.strip() == "true":
                    container = c
                    try:  # a just-started sandbox can refuse exec for a few seconds
                        (stager.stage if stager else stage)(container, kit, server)
                        staged = True
                        L("staged into", container, f"{time.time() - t0:.0f}s")
                    except (RuntimeError, subprocess.CalledProcessError) as e:
                        stage_errors += 1
                        L("staging failed", stage_errors, e)
                        if stage_errors >= 6:  # never leave a swarm running without its data
                            raise
            if staged and container:
                keep_alive(container)
            try:
                info = api("GET", f"/api/ship/runs/{run_id}")
            except Exception as e:  # noqa: BLE001
                L("poll error", e)
            st = (info.get("run") or info).get("status", "")
            if st in TERMINAL:
                break
            if time.time() > deadline:
                if stager and staged and container:  # the kernel's own stop tears the sandbox down: harvest first
                    try:
                        harvested = stager.harvest(container, out)
                        L("harvested before stopping")
                    except Exception as e:  # noqa: BLE001
                        L("pre-stop harvest error", e)
                L("driver deadline: stopping the run")
                stopped_by_driver = True  # our own stop: the run then reads "aborted", which is not infrastructure
                try:
                    api("POST", f"/api/ship/runs/{run_id}/stop", {})
                except Exception as e:  # noqa: BLE001
                    L("stop error", e)
                time.sleep(30)
                break
            # Detect a newly allocated sandbox promptly so its worker can read
            # READY and start querying. Back off after 30 seconds; retain the
            # normal polling cadence once staged and all existing deadlines.
            time.sleep(2 if not staged and time.time() - t0 < 30 else 10)
        for _ in range(12):  # the report is written when the kernel stops
            try:
                rq = api("GET", f"/api/ship/runs/{run_id}/quorum", timeout=120)
            except Exception as e:  # noqa: BLE001
                L("quorum view error", e)
            if (rq.get("report") or {}).get("status") or not rq.get("live", True):
                break
            time.sleep(10)
    except Exception as err:
        # failed before a result: stop the run and delete its sandbox, or it keeps spending money
        try:
            api("POST", f"/api/ship/runs/{run_id}/stop", {})
        except Exception as e:  # noqa: BLE001
            L("stop error", e)
        try:
            if not rq.get("ide_id"):
                rq = api("GET", f"/api/ship/runs/{run_id}/quorum", timeout=120)
            if rq.get("ide_id"):
                api("DELETE", f"/api/ide/workspaces/{rq['ide_id']}", timeout=180)
        except Exception as e:  # noqa: BLE001
            L("sandbox delete error", e)
        log.close()
        raise AttemptFailed(f"{tid}: attempt failed: {type(err).__name__}: {err}", cost_usd=_report_cost(rq),
                            run_id=run_id) from err
    finally:
        if container:
            if stager and staged:
                try:  # the final state; a pre-stop harvest stays when the sandbox is already gone
                    harvested = stager.harvest(container, out)
                except Exception as e:  # noqa: BLE001 - a lost sandbox leaves nothing to harvest
                    L("harvest error", e)
            (stager.unstage if stager else unstage)(container, server)
    rep_ = rq.get("report") or {}
    view_path = out / "quorum_view.json"
    view_path.write_text(json.dumps(rq, default=str))
    (out / "report.json").write_text(json.dumps(rep_, indent=1, default=str))
    fb_stmt, fb_rendering = fallback_claim(rq)
    status = (info.get("run") or info).get("status")
    r = {"run_id": run_id, "status": status, "staged": staged,
         "statement": (rep_.get("statement") or "").strip(), "rendering": rep_.get("rendering") or "",
         "fallback_statement": fb_stmt, "fallback_rendering": fb_rendering,  # set only when statement is empty
         "evidence": answer_evidence(rq),  # the worker's evidence behind statement (or fallback_statement)
         "gate_pass": deliverable_gate_pass(rq) and not fb_stmt,
         "verification": answer_verification(rq),
         # the report is empty when a run ends abnormally, so also count what the server saw the run spend
         "cost_usd": max(float(rep_.get("cost_usd") or 0), _server_spend(rq)), "seconds": round(time.time() - t0),
         "server_status": status, "stop_reason": str(rep_.get("reason") or (rq.get("run") or {}).get("stop_reason") or ""),
         "view_path": str(view_path), "container": container,
         "stopped_by_driver": stopped_by_driver, "harvest": harvested,
         "infra_loss": infra_loss(rq, status, stopped_by_driver=stopped_by_driver) if staged else ""}
    if r["infra_loss"]:
        L("infrastructure failure after staging:", r["infra_loss"])
    if rq.get("ide_id"):  # the run's sandbox outlives the run; remove it through the server (container + volume)
        try:
            api("DELETE", f"/api/ide/workspaces/{rq['ide_id']}", timeout=180)
        except Exception as e:  # noqa: BLE001
            L("sandbox delete error", e)
    if not staged:
        L("infrastructure failure: run ended before staging", r["status"])
        log.close()
        raise AttemptFailed(f"{tid}: run ended before the kit was staged ({r['status']})", cost_usd=r["cost_usd"],
                            run_id=run_id)
    log.close()
    return r
