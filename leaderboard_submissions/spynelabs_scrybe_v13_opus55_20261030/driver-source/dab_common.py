#!/usr/bin/env python3
"""Shared helpers for running scrybe on DataAgentBench (DAB). Runs on the HOST only.

Subcommands:
  workspace <dataset> <queryN> <ws_dir>   write TASK.md + db_config.json into ws_dir and print the
                                          docker `-v` mount specs (one per line) for the data files.
  layer-workspace <dataset> <ws_dir>       DESCRIPTION.md + db_config.json for the offline semantic-layer build.
  score <dataset> <queryN> <answer_file> <verdict_json>
                                          run that query's validate.py on the answer, write the verdict.
  list <scored|dev|all>                   print `<dataset>/<queryN>` task ids.

The DAB checkout is $DAB_ROOT (default: <repo>/vendor/DataAgentBench). Nothing here ever copies a
query folder (query.json aside, which is read on the host), ground_truth.csv, validate.py or
manual_querycode/ into the workspace.
"""
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DAB_ROOT = Path(os.environ.get("DAB_ROOT", REPO / "vendor" / "DataAgentBench")).resolve()

# Leaderboard (scored) datasets (all 12; PATENTS loaded 2026-10-21).
SCORED = ["agnews", "bookreview", "crmarenapro", "DEPS_DEV_V1", "GITHUB_REPOS", "googlelocal",
          "music_brainz_20k", "PANCANCER_ATLAS", "PATENTS", "stockindex", "stockmarket", "yelp"]
DEV = ["civic_unstructured", "cve", "krama", "usaspending"]
DEFERRED = {"imdb"}

PG_HOST, PG_PORT = "dab-pg", 5432
MONGO_URI = "mongodb://agent:<REDACTED>@dab-mongo:27017/?authSource=admin"


def dataset_dir(ds: str) -> Path:
    d = DAB_ROOT / f"query_{ds}"
    if not d.is_dir():
        sys.exit(f"unknown dataset {ds}: {d} not found")
    return d


def parse_db_config(ds: str) -> dict:
    """Minimal parser for DAB's db_config.yaml (db_clients: <name>: <key>: <value>)."""
    clients, cur = {}, None
    for raw in (dataset_dir(ds) / "db_config.yaml").read_text().splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        val = val.strip().strip("'\"")
        if indent == 0:
            continue  # db_clients:
        if not val:
            cur = clients.setdefault(key, {})
        elif cur is not None:
            cur[key] = val
    return clients


def queries(ds: str) -> list:
    qs = [p.name for p in dataset_dir(ds).iterdir() if p.is_dir() and re.fullmatch(r"query\d+", p.name)]
    return sorted(qs, key=lambda q: int(q[5:]))


def task_ids(which: str) -> list:
    sets = {"scored": SCORED, "dev": DEV, "all": SCORED + DEV}[which]
    return [f"{ds}/{q}" for ds in sets if ds not in DEFERRED for q in queries(ds)]


def build_layer_workspace(ds: str, ws: Path) -> list:
    """Workspace for the OFFLINE semantic-layer build: description + hints and db access only (no query)."""
    d = dataset_dir(ds)
    desc = (d / "db_description.txt").read_text().strip() + "\n\n" + (d / "db_description_withhint.txt").read_text().strip()
    (ws / "DESCRIPTION.md").write_text(desc + "\n")
    return _db_config(ds, ws)


def build_workspace(ds: str, q: str, ws: Path) -> list:
    d = dataset_dir(ds)
    if not re.fullmatch(r"query\d+", q) or not (d / q / "query.json").is_file():
        sys.exit(f"unknown query {ds}/{q}")
    question = json.loads((d / q / "query.json").read_text())
    if not isinstance(question, str):
        question = json.dumps(question)
    # Mirrors run_agent.py --use_hints + prompt_builder.init_messages.
    desc = (d / "db_description.txt").read_text().strip()
    hints = (d / "db_description_withhint.txt").read_text()
    desc += "\n\n" + hints.strip()
    (ws / "TASK.md").write_text(f"QUERY:\n{question.strip()}\n\nDATABASE DESCRIPTION:\n{desc.strip()}\n")
    # Precomputed schema snapshot (metadata of the databases the agent may query; no answers).
    snap = REPO / "runs" / "dab-schemas" / f"{ds}.md"
    if snap.is_file():
        (ws / "SCHEMA.md").write_text(snap.read_text())
    # Semantic layer (built once per dataset, offline; or synced by the user). Absent -> agent works as is.
    sl = Path(os.environ.get("SCRYBE_SEMANTIC_DIR", REPO / "runs" / "dab-semantic")) / ds / "semantic_layer.json"
    if sl.is_file() and os.environ.get("SCRYBE_SEMANTIC", "1") != "0":
        (ws / "semantic").mkdir(exist_ok=True)
        (ws / "semantic" / "semantic_layer.json").write_text(sl.read_text())
    return _db_config(ds, ws)


def _db_account(ds: str, cfg: dict):
    """(user, password) for the workspace's server databases. Default: the shared read-only `agent` account.
    DAB_DB_ACCOUNTS=per-dataset: the dataset's own account from scripts/dab-db-accounts.py, which can open only
    this dataset's databases; a missing account is an error (never a silent fallback to the shared one)."""
    if os.environ.get("DAB_DB_ACCOUNTS", "shared") != "per-dataset":
        return "agent", "agent"
    if not any(c["db_type"] in ("postgres", "mongo") for c in cfg.values()):
        return "agent", "agent"  # file-only dataset: no server account is used
    f = Path(os.environ.get("DAB_ACCOUNTS_FILE", Path.home() / ".config/scrybe/dab-agent-accounts.json"))
    try:
        a = json.loads(f.read_text())[ds]
        return a["user"], a["password"]
    except Exception:
        sys.exit(f"DAB_DB_ACCOUNTS=per-dataset but no account for {ds} in {f}: run scripts/dab-db-accounts.py ensure {ds}")


def _db_config(ds: str, ws: Path) -> list:
    d = dataset_dir(ds)
    dbs, mounts = {}, []
    cfg = parse_db_config(ds)
    user, pw = _db_account(ds, cfg)
    for name, c in cfg.items():
        t = c["db_type"]
        if t == "postgres":
            dbs[name] = {"type": "postgres", "dsn": f"postgresql://{user}:<REDACTED>@{PG_HOST}:{PG_PORT}/{c['db_name']}"}
        elif t == "mongo":
            uri = MONGO_URI if user == "agent" else f"mongodb://{user}:<REDACTED>@dab-mongo:27017/?authSource=admin"
            dbs[name] = {"type": "mongo", "uri": uri, "db": c["db_name"]}
        elif t in ("sqlite", "duckdb"):
            rel = Path(c["db_path"])
            if rel.parts[0] != "query_dataset" or ".." in rel.parts:
                sys.exit(f"unexpected db_path {rel}")
            inner = Path(*rel.parts[1:])
            src = (d / rel).resolve()
            if not src.is_file() or src.stat().st_size < 1024:
                sys.exit(f"missing data file {src} (run download.sh)")
            dbs[name] = {"type": t, "path": f"/data/{inner}"}
            mounts.append(f"{src}:/data/{inner}:ro")
        else:
            sys.exit(f"unsupported db_type {t} in {ds}")
    (ws / "db_config.json").write_text(json.dumps({"databases": dbs}, indent=2) + "\n")
    return mounts


def score(ds: str, q: str, answer: str) -> dict:
    import importlib.util
    qdir = dataset_dir(ds) / q
    if not answer.strip():
        return {"reward": 0, "reason": "empty answer"}
    sys.path.insert(0, str(DAB_ROOT))  # validators import common_scaffold.validate.levenshtein
    cwd = os.getcwd()
    try:
        os.chdir(qdir)
        spec = importlib.util.spec_from_file_location(f"dab_validate_{ds}_{q}", str(qdir / "validate.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok, reason = mod.validate(answer)
        return {"reward": 1 if ok else 0, "reason": str(reason)}
    except Exception as e:  # noqa: BLE001
        return {"reward": 0, "reason": f"validator error: {type(e).__name__}: {e}"}
    finally:
        os.chdir(cwd)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "workspace" and len(argv) == 5:
        for m in build_workspace(argv[2], argv[3], Path(argv[4])):
            print(m)
    elif cmd == "layer-workspace" and len(argv) == 4:
        for m in build_layer_workspace(argv[2], Path(argv[3])):
            print(m)
    elif cmd == "score" and len(argv) == 6:
        ans = Path(argv[4]).read_text() if Path(argv[4]).is_file() else ""
        v = score(argv[2], argv[3], ans)
        Path(argv[5]).write_text(json.dumps(v) + "\n")
        print(f"{argv[2]}/{argv[3]} reward={v['reward']}")
    elif cmd == "list" and len(argv) == 3:
        print("\n".join(task_ids(argv[2])))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
