#!/usr/bin/env python3
"""sage / verify: specialist helpers that answer in a separate context from yours.

  sage "<what you need to find out>"       which tables, columns, joins and recorded definitions the question needs, and
                                           which wording is ambiguous (reads the offline Sage profile, never rows)
  verify --answer-file <file> --how-file <file>   (or --answer "<candidate>" --how "<account>"; text in double quotes is
                                           altered by the shell: $20 becomes empty, backticks run; write the files with a
                                           quoted heredoc: cat > f <<'EOF' ... EOF)
                                           an independent review: every requested value present, the declared shape,
                                           and whether the method supports the answer

Each can be called a few times per task; use what it returns, then check it against the data yourself.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, f"{os.environ.get('KIT_HOME', '/home/ide/dab')}/lib")
import shapelint  # noqa: E402

KIT = os.environ.get("KIT_HOME", "/home/ide/dab")
URL = os.environ.get("JLABEL_URL", "http://host.docker.internal:18765")
TOKEN = os.environ.get("JLABEL_TOKEN", "")
PROFILE_CHARS = 60000
STAGE_DIR = "/home/ide/dab-work/.stage"


def _mark(name: str):
    """Stage marker: the query tools stay locked until sage has run (or was unavailable)."""
    try:
        os.makedirs(STAGE_DIR, exist_ok=True)
        open(f"{STAGE_DIR}/{name}", "w").write("1")
    except OSError:
        pass


def _post(body: dict) -> dict:
    req = urllib.request.Request(URL + "/ask", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:300]
        _mark("locate")
        sys.exit(f"{body['tool']}: {e.code} {msg}")
    except (urllib.error.URLError, OSError) as e:
        _mark("locate")
        sys.exit(f"{body['tool']}: service unreachable ({e}); continue without it")


def _sl(*args) -> str:
    r = subprocess.run([f"{KIT}/bin/sl", *args], capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


def _read(name: str) -> str:
    try:
        return open(f"{KIT}/{name}").read()
    except OSError:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tool = os.environ.get("TOOL_NAME", "sage")
    if tool == "verify":
        ap.add_argument("--answer", help="the candidate answer (prefer --answer-file: a shell expands $5 or backticks inside double quotes)")
        ap.add_argument("--answer-file")
        ap.add_argument("--how", help="how it was computed (prefer --how-file for the same reason)")
        ap.add_argument("--how-file")
    else:
        ap.add_argument("focus", nargs="?", default="")
    a = ap.parse_args(argv)
    if tool == "verify":
        for name in ("answer", "how"):
            path = getattr(a, name + "_file")
            if path:
                try:
                    setattr(a, name, open(path).read().rstrip("\n"))
                except OSError as e:
                    sys.exit(f"verify: cannot read {path}: {e}")
            if not getattr(a, name):
                sys.exit(f"verify: give --{name} or --{name}-file")
    if not TOKEN:
        sys.exit(f"{tool}: not available in this sandbox; continue without it")
    question = _read("TASK.md")
    if tool == "verify":
        shape = ""
        try:
            shape = json.loads(_read("DELIVERABLE.json") or "{}").get("format", "")
        except ValueError:
            pass
        contract = _read("CONTRACT.txt")
        asks = len(re.findall(r"^- A\d+:", contract, re.M))
        problems = shapelint.lint(a.answer, shape, asks, a.how)
        if problems:
            print("LINT (code-checked, fix before answering):\n" + "\n".join(f"- {p}" for p in problems) + "\n")
        body = {"tool": "verify", "question": question, "shape": shape, "answer": a.answer, "how": a.how,
                "contract": contract}
    else:
        profile = "\n\n".join(p for p in (_sl("overview"), "JOINS\n" + _sl("joins")) if p)
        body = {"tool": "locate", "question": question, "focus": a.focus, "profile": profile[:PROFILE_CHARS]}
    try:
        print(_post(body)["text"])
    finally:
        if tool != "verify":
            _mark("locate")


if __name__ == "__main__":
    main()
