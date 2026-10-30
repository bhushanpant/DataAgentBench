"""Front door: Opus reads the task and the Sage overview, declares the deliverable, acceptance checks and a short plan.

The plan uses the Quorum kernel's BountyInput field names; exactly one bounty carries `deliverable: true`.
Checks are shell commands run inside the Quorum sandbox with the candidate answer in $QUORUM_ANSWER; they may read
only that string (POSIX text tools). Checks that reach for a database, kit tool, python or the network are dropped.
"""
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sq import overlay, provider
from sq import submission_evidence

MODEL = "claude-opus-5-5"
KIT_HOME = "/home/ide/dab"

_STR = {"type": "string"}
KINDS = ("build", "research", "decompose")
CLAIM_TYPES = ("code_change", "fact", "analysis", "proof", "decision", "synthesis", "protocol")
SCHEMA = {
    "type": "object",
    "required": ["deliverable", "checks", "example_answer", "plan", "signals"],
    "properties": {
        "deliverable": {"type": "object", "required": ["kind", "ac", "format"],
                        "properties": {"kind": _STR, "ac": _STR, "format": _STR,
                                       "columns": {"type": "array", "items": {
                                           "type": "object", "required": ["name", "type"],
                                           "properties": {"name": _STR,
                                                          "type": {"type": "string", "enum": ["string", "integer", "number", "boolean"]},
                                                          "unit": _STR}}}}},
        "checks": {"type": "array", "items": {
            "type": "object", "required": ["id", "text", "cmd"],
            "properties": {"id": _STR, "text": _STR, "cmd": _STR}}},
        "example_answer": _STR,
        "plan": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["ref", "kind", "title", "instructions", "claim_type", "ac_refs", "depends_on", "files",
                         "value", "risk", "est_sec", "deliverable"],
            "properties": {
                "ref": _STR, "kind": {"type": "string", "enum": list(KINDS)}, "title": _STR, "instructions": _STR,
                "claim_type": {"type": "string", "enum": list(CLAIM_TYPES)},
                "ac_refs": {"type": "array", "items": _STR}, "depends_on": {"type": "array", "items": _STR},
                "files": {"type": "array", "items": _STR},
                "value": {"type": "number"}, "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "est_sec": {"type": "integer"},
                "deliverable": {"type": "boolean"}}}},
        "signals": {"type": "object", "required": ["sources", "per_row_semantic", "est_rows"],
                    "properties": {"sources": {"type": "integer"},
                                   "per_row_semantic": {"type": ["boolean", "null"]},
                                   "est_rows": {"type": "integer"}}},
    },
}

SYSTEM = f"""You are the intake planner for a data-analysis team. You read a data task and an overview of the available \
data, then declare what must be delivered, how to sanity-check a candidate answer, and the shortest plan that reaches it.

Output (structured):
- deliverable: kind (e.g. "answer"), ac "AC-1", and format: the exact shape the final answer must take. The format \
restates only what the question asks for: never add optional fields, qualifiers, parentheticals, approximations or \
extra columns, and never offer alternatives ("optionally", "or equivalently"). Describe exactly one shape.
- deliverable.columns: one entry per value the question asks to be returned, in the order asked, each with a short \
name, a type (string, integer, number or boolean) and a unit when the question gives one. Identifiers and codes are \
strings; counts are integers; measured or computed quantities are numbers. When the question asks for several values \
(for example an entity and an attribute of it, or a value and its count), list a column for each: an answer that omits \
a requested value is incomplete. The format text describes the same shape in words.
- checks: shell commands run inside the sandbox after a candidate answer is produced; the candidate is in the env var \
QUORUM_ANSWER. A check exits 0 when the answer passes. Checks operate ONLY on the answer string in $QUORUM_ANSWER, \
using POSIX shell text tools (test, grep, wc, awk, sed, printf): no database, no kit tools ({KIT_HOME}/bin/*), \
no python, no network, no files. Checks verify FORMAT and SANITY only (non-empty, type, shape, item count, plausible \
range, parses). A check must never contain or compute the expected value. Never write a check that counts \
separator-delimited fields or requires an exact number of comma-separated items: a returned value (a name, a title) \
may itself contain the separator, so only check an item count when the items cannot contain it. Example: \
printf '%s' "$QUORUM_ANSWER" | grep -Eq '^-?[0-9]+(\\.[0-9]+)?$'
- example_answer: one answer written in exactly the declared format with obvious placeholder values (for example \
"Name A, Name B" or "123"). It illustrates the format and is not a guess at the real answer. Every check must accept \
it; a check that rejects it is dropped.
- plan: the shortest list of bounties that reaches the deliverable. A task a single analyst can do end to end is ONE \
bounty. Split only when parts are genuinely independent or need different sources. Each bounty has: ref (unique), kind (build, research or decompose: the work mode, never \
"analysis"), title, instructions, claim_type (what the result asserts: "analysis" or "synthesis"), ac_refs (list of acceptance ids it serves), depends_on \
(refs), files, value (a number from 0 to 1), risk (exactly one of "low", "medium", "high"), est_sec, deliverable. Exactly one bounty has deliverable true: the one \
whose result is the final answer; it depends on any bounty it needs. The plan must not ask for anything beyond the \
question: no extra fields, qualifiers or columns in the answer.
- plan wording: keep plans short. Be concrete about routing (sources, tables, join keys), but do not settle ambiguous \
wording by fiat: when the question admits several readings (a term, a time window, a population, a category \
boundary), list them and tell the worker to determine which one the data or schema supports, looking first for \
definitions in the data and in Sage ({KIT_HOME}/bin/sl) before generic conventions; never recommend one reading as \
the natural or standard default. The plan must never prescribe an arbitrary tie-break; a tie is for the worker to \
investigate.
- signals: sources (number of distinct data sources/databases involved), per_row_semantic (true if answering needs \
per-row language understanding such as classifying free text, false if not, null if unsure), est_rows (rough number of \
rows the work touches).
"""


@dataclass
class Intake:
    deliverable: dict
    checks: list
    plan: list
    signals: dict
    cost_usd: float
    dropped_checks: list = field(default_factory=list)  # removed checks, each with its drop_reason
    example_answer: str = ""  # a format illustration with placeholder values (never sent to the worker)
    self_test: str = ""  # how the check self-test went: ran in <image>, skipped (no docker/image), or no example


DEFAULT_CHECK_CMD = 'test -n "$(printf \'%s\' "$QUORUM_ANSWER" | tr -d \'[:space:]\')"'
DEFAULT_CHECK = {"id": "C-nonempty", "text": "the answer is not blank", "cmd": DEFAULT_CHECK_CMD}
# a check may read only $QUORUM_ANSWER: anything naming the kit, a query tool, python, a database client, the
# network, or SQL is querying the data (fragile, and can reject a plausible answer), so it is dropped
_NOT_ANSWER_ONLY = re.compile(
    r"/home/ide|dbq|dbschema|dbsnap|dpython|python|sqlite|psql|mongo|duckdb|curl|wget|\b(?:select|from|where)\b",
    re.IGNORECASE)


def filter_checks(checks: list) -> tuple[list, list]:
    """Split checks into (kept, dropped): kept checks operate only on the answer string."""
    kept, dropped = [], []
    for c in checks:
        (dropped if _NOT_ANSWER_ONLY.search(c.get("cmd") or "") else kept).append(c)
    return kept, dropped


KIT_TOOLS = (
    ("dbq", 'runs one SQL or Mongo query against a database: dbq <db> "<query>"'),
    ("dbschema", "lists tables and columns"),
    ("dbsnap", "prints a compact snapshot of every database"),
    ("sl", "the Sage profile: row, null and distinct counts, ranges, top values, keys, join match rates"),
    ("dpython", "runs a Python script with pandas, duckdb, pymongo and psycopg2"),
)
Q_LABEL_TOOL = ("q_label", "labels many text rows with one shared model call per batch")


SPECIALIST_TOOLS = (
    ("jlabel", "labels the rows of a saved result with a fast calibrated classifier (categories or yes/no)"),
    ("sage", "question-aware locator over the Sage profile: relevant tables, columns, joins, recorded definitions, ambiguous wording"),
    ("joincheck", "measured match rate of two identifier columns, as stored and after simple clean-ups"),
    ("verify", "independent review of a candidate answer: missing values, declared shape, method consistency"),
)


def tools_section(q_label: bool, tools: bool = False) -> str:
    """The worker's kit tools; plans may name only these (a plan once prescribed a classifier that did not exist)."""
    tools = KIT_TOOLS + ((Q_LABEL_TOOL,) if q_label else ()) + (SPECIALIST_TOOLS if tools else ())
    lines = "\n".join(f"  - {name}: {desc}" for name, desc in tools)
    return (f"- tools: the worker has only these tools in {KIT_HOME}/bin (plus POSIX shell). Plans may reference only \
these tools; no other tool or service exists (no model or classifier unless listed):\n{lines}\n")


SELF_TEST_TIMEOUT_S = 10
CHECK_IMAGE = os.environ.get("SQ_CHECK_IMAGE", "alpha/ide-sandbox-qs:latest")  # the workers' sandbox image
CHECK_IMAGE_FALLBACK = "alpine:latest"
SELF_TEST_SKIPPED = "selftest: skipped"
# a throwaway container: no network, read-only root, no capabilities, no privilege gain, few processes, little memory
SANDBOX_FLAGS = ("--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--pids-limit", "64", "--memory", "128m")


def check_image(runner=subprocess.run):
    """The image the self-test runs checks in: CHECK_IMAGE (shell, grep, awk as in the gate), else alpine when that
    image is missing; None when docker is unavailable or neither image is present (nothing is pulled)."""
    for image in (CHECK_IMAGE, CHECK_IMAGE_FALLBACK):
        try:
            r = runner(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None  # no docker client, or it hung
        if r.returncode == 0:
            return image
    return None


def _run_in_container(cmd: str, example: str, image: str, timeout_s: int, runner) -> bool:
    name = f"sq-check-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", "--rm", "--name", name, *SANDBOX_FLAGS, "-e", f"QUORUM_ANSWER={example}",
            "-e", "LC_ALL=C", "--entrypoint", "sh", image, "-c", cmd]
    try:
        return runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s).returncode == 0
    except subprocess.TimeoutExpired:
        runner(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # a hung check: remove its container
        return False


def self_test_checks(checks: list, example: str, *, timeout_s: int = SELF_TEST_TIMEOUT_S,
                     runner=subprocess.run) -> tuple[list, list, str]:
    """Split checks into (kept, failed, status) by running each with QUORUM_ANSWER=example in a throwaway, isolated
    container (never on this host: the checks are model-written shell). A check that rejects a well-formed example (or
    hangs) is buggy and would fail every correct answer, so it is not used. Without docker or an image the self-test
    is skipped and every check is kept (status SELF_TEST_SKIPPED)."""
    image = check_image(runner)
    if image is None:
        return list(checks), [], SELF_TEST_SKIPPED
    kept, failed = [], []
    for c in checks:
        (kept if _run_in_container(c["cmd"], example, image, timeout_s, runner) else failed).append(c)
    return kept, failed, f"selftest: ran in {image}"


def build_prompt(task_md: str, sage: str, q_label: bool = False, tools: bool = False) -> tuple[str, str]:
    user = f"## Task\n\n{task_md}\n\n## Data overview\n\n{sage}\n"
    return SYSTEM + tools_section(q_label, tools) + overlay.text("intake"), user


def sage_overview(kit: Path) -> str:
    """Output of `sl overview` for a staged kit, run host-side against the kit's lib (as the kit wrappers do)."""
    env = dict(os.environ, PYTHONPATH=f"{kit}/site:{kit}/lib", SCRYBE_DB_CONFIG=str(kit / "db_config.json"),
               SCRYBE_SAGE_FILE=str(kit / "sage" / "layer.json"), SCRYBE_WORKDIR=str(kit.parent / "work"))
    Path(env["SCRYBE_WORKDIR"]).mkdir(exist_ok=True)
    r = subprocess.run(["python3", str(kit / "lib" / "sage_dbview.py"), "overview"], env=env, capture_output=True,
                       text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"sl overview failed: {r.stderr.strip()[-300:]}")
    return r.stdout


class ModelOutputError(ValueError):
    """An unusable model response still has a cost, when the provider reports one."""

    def __init__(self, message, *, cost_usd=None):
        super().__init__(message)
        self.cost_usd = cost_usd


def call_opus(system: str, user: str, schema: dict, timeout_s: int = 240, budget_usd: float = 0.50) -> tuple[dict, float]:
    """One isolated, tool-less, budget-capped Opus call: fresh empty cwd (no project CLAUDE.md or memory), no tools,
    no slash commands/skills, no user/project/local settings, no MCP servers, no persisted session."""
    for _ in range(2):  # a billing error switches every later call to OpenRouter (sq/provider.py) and retries once
        cmd = ["claude", "-p", "--model", provider.model("opus"), "--output-format", "json",
               "--json-schema", json.dumps(schema),
               "--append-system-prompt", system, "--max-budget-usd", f"{budget_usd:g}",
               "--tools", "", "--disable-slash-commands", "--setting-sources", "", "--strict-mcp-config",
               "--no-session-persistence"]
        submission_evidence.opus_request(system, user, schema, budget_usd)
        with tempfile.TemporaryDirectory(prefix="sq-intake-") as cwd:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
                               env=provider.claude_env())
        submission_evidence.opus_response(r)
        if r.returncode and provider.current() == "anthropic" and provider.is_credit_error(r.stderr + r.stdout):
            provider.switch(f"claude -p: {(r.stderr + r.stdout).strip()[-300:]}")
            continue
        break
    try:
        res = json.loads(r.stdout)
        if not isinstance(res, dict):
            raise ValueError("response is not an object")
    except (ValueError, TypeError) as e:
        raise ModelOutputError("claude returned no parseable response; cost unknown") from e
    cost = res.get("total_cost_usd", res.get("cost_usd"))
    cost = float(cost) if cost is not None else None
    if r.returncode:
        raise ModelOutputError(f"claude failed ({r.returncode})", cost_usd=cost)
    obj = res.get("structured_output")
    if not isinstance(obj, dict):
        try:
            obj = json.loads(res.get("result") or "")
        except (ValueError, TypeError) as e:
            raise ModelOutputError("opus returned no parseable structured output", cost_usd=cost) from e
    if not isinstance(obj, dict):
        raise ModelOutputError("opus structured output is not an object", cost_usd=cost)
    return obj, cost or 0.0


BOUNTY_FIELDS = ("ref", "kind", "title", "instructions", "claim_type", "ac_refs", "depends_on", "files",
                 "value", "risk", "est_sec", "deliverable")
# the kernel's BountyInput fields minus `inputs`: the kernel forbids plans naming claim inputs


def _num(x, default):
    try:
        return float(x) if not isinstance(x, bool) else default
    except (TypeError, ValueError):
        return default


def _str_list(x) -> list:
    if x is None:
        return []
    return [str(i) for i in (x if isinstance(x, (list, tuple)) else [x])]


def normalize_risk(r) -> str:
    if isinstance(r, str) and r.strip().lower() in ("low", "medium", "high"):
        return r.strip().lower()
    n = _num(r, None) if not isinstance(r, str) or r.strip()[:1] in "0123456789.-" else None
    if n is None:
        return "medium"
    return "low" if n < 0.34 else "medium" if n < 0.67 else "high"


def normalize_bounty(b: dict) -> dict:
    """Coerce one bounty to the kernel's BountyInput types; drop keys the kernel does not know."""
    v = _num(b.get("value"), 0.5)
    if v > 1:  # the intake prompt once asked for 1-10
        v /= 10
    kind = str(b.get("kind", "")).strip().lower()
    claim = str(b.get("claim_type", "")).strip().lower()
    if kind in CLAIM_TYPES and claim not in CLAIM_TYPES:  # the model put the claim type in `kind`
        claim = kind
    if kind not in KINDS:
        kind = "build"
    if claim not in CLAIM_TYPES:
        claim = "analysis"
    if b.get("deliverable") is True and claim not in ("analysis", "synthesis"):
        claim = "analysis"  # a data-task deliverable (answer/table/artifact) is an analysis claim
    out = {"ref": str(b.get("ref", "")), "kind": kind, "title": str(b.get("title", "")),
           "instructions": str(b.get("instructions", "")), "claim_type": claim,
           "ac_refs": _str_list(b.get("ac_refs")), "depends_on": _str_list(b.get("depends_on")),
           "files": _str_list(b.get("files")),
           "value": min(1.0, max(0.0, v)), "risk": normalize_risk(b.get("risk")),
           "est_sec": max(0, int(_num(b.get("est_sec"), 0))), "deliverable": b.get("deliverable") is True}
    return out


def normalize_plan(plan: list) -> list:
    return [normalize_bounty(b) for b in plan]


def _validate(o: dict):
    for k in ("deliverable", "checks", "plan", "signals"):
        if k not in o:
            raise ValueError(f"intake missing {k!r}")
    plan = o["plan"]
    if not plan:
        raise ValueError("intake plan is empty")
    n = sum(1 for b in plan if b.get("deliverable") is True)
    if n != 1:
        raise ValueError(f"plan must have exactly one deliverable bounty, got {n}")
    refs = [b.get("ref") for b in plan]
    if len(set(refs)) != len(refs) or not all(refs):
        raise ValueError("plan refs must be unique and non-empty")
    for b in plan:
        for d in b.get("depends_on") or []:
            if d not in refs:
                raise ValueError(f"bounty {b['ref']} depends on unknown ref {d!r}")
    for c in o["checks"]:
        if not (c.get("id") and c.get("cmd", "").strip()):
            raise ValueError(f"check needs id and cmd: {c}")
    if not o["deliverable"].get("ac"):
        raise ValueError("deliverable needs an ac id")


def intake(task_md: str, sage: str, *, q_label: bool = False, tools: bool = False, llm=call_opus, budget_usd: float = 0.50,
           runner=subprocess.run) -> Intake:
    """q_label: whether the worker's kit has the batch labeler (the plan may name only tools that exist).
    runner: subprocess.run for the check self-test's docker calls (tests pass a fake)."""
    system, user = build_prompt(task_md, sage, q_label=q_label, tools=tools)
    o, cost = llm(system, user, SCHEMA) if llm is not call_opus else llm(system, user, SCHEMA, budget_usd=budget_usd)
    try:
        _validate(o)
    except (ValueError, KeyError, TypeError) as e:
        raise ModelOutputError(f"invalid intake: {e}", cost_usd=cost) from e
    checks, dropped = filter_checks(o["checks"])
    dropped = [dict(c, drop_reason="not answer-only") for c in dropped]
    example, self_test = str(o.get("example_answer") or ""), "selftest: no example_answer"
    if example.strip():
        checks, failed, self_test = self_test_checks(checks, example, runner=runner)
        dropped += [dict(c, drop_reason="rejects example_answer") for c in failed]
    s = o["signals"]
    cols = [c for c in (o["deliverable"].pop("columns", None) or []) if isinstance(c, dict) and c.get("name")]
    if cols:  # the typed shape travels as words in the format; numeric single values must contain a digit
        shape = "; ".join(f"{c['name']} ({c.get('type', 'string')}{', ' + c['unit'] if c.get('unit') else ''})"
                          for c in cols)
        o["deliverable"]["format"] = f"{o['deliverable'].get('format', '').rstrip('. ')}. Values, in order: {shape}."
        if len(cols) == 1 and cols[0].get("type") in ("integer", "number"):
            checks = checks + [{"id": "C-numeric", "text": "the answer contains a number",
                                "cmd": "printf '%s' \"$QUORUM_ANSWER\" | grep -Eq '[0-9]'"}]
    return Intake(deliverable=o["deliverable"], checks=checks or [dict(DEFAULT_CHECK)], plan=normalize_plan(o["plan"]),
                  signals={"sources": int(s["sources"]), "per_row_semantic": s["per_row_semantic"],
                           "est_rows": int(s["est_rows"])}, cost_usd=float(cost), dropped_checks=dropped,
                  example_answer=example, self_test=self_test)
