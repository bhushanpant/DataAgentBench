"""Shared helpers of the untuned core harness (harness/core-untuned): configuration, state, dbt project discovery,
profiles, read-only warehouse access, file snapshots.

Engineering only: nothing here decides or suggests anything about a task. The warehouse access (profile env_var
rendering, Snowflake key-pair forms, DuckDB read-only open with lock retries) follows the outcome-blind engineering
of the earlier harness core (docs/harness-core-untuned/PROVENANCE.md, rows P-ENG*). Python >= 3.11, stdlib + PyYAML,
plus duckdb or snowflake-connector when the backend needs it.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import time

STATE = os.environ.get("SCRYBE_STATE_DIR", "/tmp/scrybe")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.environ.get("CC_HARNESS") or os.environ.get("SCRYBE_HARNESS") or os.path.dirname(SCRIPTS)
SKIP_DIRS = {"dbt_packages", "target", "logs", ".venv", "venv", "node_modules", ".git", "site-packages", "__pycache__"}
SEARCH_ROOTS = ("/app", "/workspace")


def state_path(*parts):
    p = os.path.join(STATE, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# ------------------------------------------------------------------ configuration

def _merge(a, b):
    """Deep merge of two parsed TOML documents: tables merge key by key, everything else (incl. arrays) is replaced."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def config(harness=None):
    """harness.toml, overlaid by bench.toml when the harness directory has one (per-benchmark budgets)."""
    import tomllib
    h = harness or HARNESS
    cfg = {}
    for name in ("harness.toml", "bench.toml"):
        p = os.path.join(h, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                cfg = _merge(cfg, tomllib.load(fh))
    return cfg


def section(name, harness=None):
    return config(harness).get(name) or {}


def task_text():
    p = os.environ.get("SCRYBE_TASK_FILE", os.path.join(STATE, "task.md"))
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def deadline_left():
    d = os.environ.get("SCRYBE_DEADLINE")
    try:
        return int(d) - time.time() if d else None
    except ValueError:
        return None


def save_json(name, obj):
    p = state_path(name)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, default=str, sort_keys=False)
    os.replace(tmp, p)


def load_json(name, default=None):
    try:
        with open(os.path.join(STATE, name)) as fh:
            return json.load(fh)
    except Exception:
        return default


def sh(cmd, cwd=None, timeout=120, env=None):
    """-> (rc, combined output). rc 124 on timeout; never raises for the command's own failure."""
    e = dict(os.environ)
    e.update(env or {})
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=max(1, int(timeout)), env=e)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout or b""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return 124, out + f"\n[timed out after {int(timeout)}s]"


# ------------------------------------------------------------------ multi-database environments

# database config of a multi-database environment: {"databases": {<logical name>: {"type": ..., connection}}}
DB_CONFIG_NAME = "db_config.json"
DB_TYPE_ALIASES = {"postgresql": "postgres", "pg": "postgres", "mongo": "mongodb", "sqlite3": "sqlite"}


def workdir():
    """The task's working directory ($SCRYBE_WORKDIR, set by the adapter; else the current directory)."""
    return os.environ.get("SCRYBE_WORKDIR") or os.getcwd()


def db_config_path():
    """The database config of this environment: $SCRYBE_DB_CONFIG when it names a file, else db_config.json in the
    working directory; None when there is none."""
    p = os.environ.get("SCRYBE_DB_CONFIG")
    if p and os.path.isfile(p):
        return p
    p = os.path.join(workdir(), DB_CONFIG_NAME)
    return p if os.path.isfile(p) else None


def db_inventory(path=None):
    """[(logical name, platform type)] of the databases in the config (connection settings are never returned);
    [] when there is no readable config."""
    p = path or db_config_path()
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            dbs = (json.load(fh) or {}).get("databases") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for name, c in sorted(dbs.items()) if isinstance(dbs, dict) else []:
        t = str((c or {}).get("type") or "").lower() if isinstance(c, dict) else ""
        out.append((str(name), DB_TYPE_ALIASES.get(t, t)))
    return out


# ------------------------------------------------------------------ dbt projects

def find_projects(roots=None):
    """Directories holding a dbt_project.yml under /app, /workspace and the cwd (depth <= 3), outside installed
    packages and build output; nested projects inside another found project are kept apart only if not packages."""
    roots = [r for r in (roots or (*SEARCH_ROOTS, os.getcwd())) if os.path.isdir(r)]
    found = set()
    for root in roots:
        base = root.rstrip("/").count("/")
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            if d.count("/") - base >= 3:
                dirs[:] = []
            if "dbt_project.yml" in files:
                found.add(os.path.realpath(d))
    return sorted(found)


def read_yaml(path):
    import yaml
    with open(path, encoding="utf-8", errors="replace") as fh:
        return yaml.safe_load(fh)


def project_meta(proj):
    """name / profile / paths from dbt_project.yml (dict; 'error' set when it cannot be read)."""
    out = {"root": proj}
    try:
        d = read_yaml(os.path.join(proj, "dbt_project.yml")) or {}
    except Exception as e:  # noqa: BLE001
        out["error"] = f"dbt_project.yml unreadable: {str(e).splitlines()[0][:200]}"
        d = {}
    out["name"] = d.get("name")
    out["profile"] = d.get("profile")
    for key, default in (("model-paths", ["models"]), ("seed-paths", ["seeds"]), ("snapshot-paths", ["snapshots"]),
                         ("macro-paths", ["macros"]), ("test-paths", ["tests"]), ("analysis-paths", ["analyses"])):
        v = d.get(key, d.get(key.replace("-", "_"), default))
        out[key] = [str(x) for x in (v if isinstance(v, list) else [v])] if v else default
    # where the project's own dbt commands write manifest / run_results ($DBT_TARGET_PATH wins, as in dbt)
    tp = os.environ.get("DBT_TARGET_PATH") or d.get("target-path", d.get("target_path")) or "target"
    out["target-path"] = str(tp) if os.path.isabs(str(tp)) else os.path.join(proj, str(tp))
    return out


_ENV_RE = re.compile(r"\{\{\s*env_var\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*(?:['\"]([^'\"]*)['\"]|([^)\s]+)))?\s*\)\s*(?:\|[^}]*)?\}\}")


def _render_env(text):
    def rep(m):
        default = m.group(2) if m.group(2) is not None else m.group(3)
        return os.environ.get(m.group(1), default if default is not None else "")
    return _ENV_RE.sub(rep, text)


def _render_tree(o):
    if isinstance(o, dict):
        return {k: _render_tree(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_render_tree(v) for v in o]
    return _render_env(o) if isinstance(o, str) else o


def profiles_dir(proj):
    if os.environ.get("DBT_PROFILES_DIR"):
        return os.environ["DBT_PROFILES_DIR"]
    if os.path.isfile(os.path.join(proj, "profiles.yml")):
        return proj
    return os.path.expanduser("~/.dbt")


def profile_target(value):
    """Resolve small env_var/conditional target expressions without executing macros.

    Regex env substitution cannot handle a conditional that selects among two
    backend targets. Keep unsupported expressions unresolved rather than guess
    the first output, which could silently select an inactive warehouse.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value
    if len(value) > 4096 or "{%" in value or "{#" in value:
        return value
    try:
        from jinja2 import StrictUndefined, nodes
        from jinja2.sandbox import SandboxedEnvironment
        env = SandboxedEnvironment(undefined=StrictUndefined)
        env.globals.clear()
        env.filters.clear()
        env.tests.clear()
        tree = env.parse(value)
        allowed = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Const,
                   nodes.CondExpr, nodes.Compare, nodes.Operand, nodes.Name,
                   nodes.Call, nodes.And, nodes.Or, nodes.Not)
        for node in [tree, *tree.find_all(nodes.Node)]:
            if not isinstance(node, allowed):
                return value
            if isinstance(node, nodes.Name) and node.name != "env_var":
                return value
            if isinstance(node, nodes.Call) and (
                    not isinstance(node.node, nodes.Name) or node.node.name != "env_var"
                    or not 1 <= len(node.args) <= 2 or node.kwargs
                    or node.dyn_args is not None or node.dyn_kwargs is not None
                    or not all(isinstance(a, nodes.Const) for a in node.args)):
                return value
        missing = object()
        def env_var(name, default=missing):
            if not isinstance(name, str):
                raise ValueError("environment name must be a string")
            if name in os.environ:
                return os.environ[name]
            if default is missing:
                raise ValueError("required target environment variable is unset")
            return default
        target = env.from_string(value).render(env_var=env_var).strip()
        return target if len(target) <= 1024 else value
    except Exception:  # noqa: BLE001  unsupported target stays explicitly unknown
        return value


def profile_of(proj):
    """-> (profile name, target name, rendered output dict) of the project's active target; ({} when unknown)."""
    import yaml
    name = project_meta(proj).get("profile")
    if not name:
        return None, None, {}
    for d in [x for x in (os.environ.get("DBT_PROFILES_DIR"), proj, os.path.expanduser("~/.dbt")) if x]:
        f = os.path.join(d, "profiles.yml")
        if not os.path.isfile(f):
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        try:
            prof = _render_tree((yaml.safe_load(text) or {}).get(name))
        except Exception:  # noqa: BLE001  unquoted {{ }}: render first, then parse
            try:
                prof = (yaml.safe_load(_render_env(text)) or {}).get(name)
            except Exception:  # noqa: BLE001
                prof = None
        if isinstance(prof, dict):
            tgt = os.environ.get("DBT_TARGET") or profile_target(prof.get("target", "dev"))
            out = (prof.get("outputs") or {}).get(tgt) or {}
            return name, tgt, out if isinstance(out, dict) else {}
    return name, None, {}


def project_backend(proj):
    return str(profile_of(proj)[2].get("type") or "").lower()


def active_backend(projects=None):
    """$DB_TYPE when set, else the profile type of the first project that has one."""
    b = (os.environ.get("DB_TYPE") or "").lower()
    if b:
        return b
    for p in projects if projects is not None else find_projects():
        t = project_backend(p)
        if t:
            return t
    return ""


def conn_profile(prefer=None):
    """Connection settings for the active backend: the profile of `prefer` (a project dir) or of the first project
    whose profile type matches the backend; else SNOWFLAKE_* environment variables; else {}."""
    projects = find_projects()
    want = active_backend(projects)
    order = ([prefer] if prefer else []) + [p for p in projects if p != prefer]
    for p in order:
        if not p or not os.path.isfile(os.path.join(p, "dbt_project.yml")):
            continue
        o = profile_of(p)[2]
        if o and (not want or str(o.get("type") or "").lower() == want):
            return dict(o, _project=p)
    if want == "snowflake":
        e = os.environ
        o = {"type": "snowflake", "account": e.get("SNOWFLAKE_ACCOUNT"), "user": e.get("SNOWFLAKE_USER"),
             "password": e.get("SNOWFLAKE_PASSWORD"), "role": e.get("SNOWFLAKE_ROLE"),
             "warehouse": e.get("SNOWFLAKE_WAREHOUSE"), "database": e.get("SNOWFLAKE_DATABASE"),
             "schema": e.get("SNOWFLAKE_SCHEMA")}
        if e.get("SNOWFLAKE_PRIVATE_KEY") and not e.get("SNOWFLAKE_PASSWORD"):
            o["private_key"] = e["SNOWFLAKE_PRIVATE_KEY"]
            o["private_key_passphrase"] = e.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        return {k: v for k, v in o.items() if v}
    if want in ("duckdb", "") and os.environ.get("DUCKDB_PATH"):
        return {"type": "duckdb", "path": os.environ["DUCKDB_PATH"]}
    return {}


def cwd_project():
    d = os.path.realpath(os.getcwd())
    while d and d != "/":
        if os.path.isfile(os.path.join(d, "dbt_project.yml")):
            return d
        d = os.path.dirname(d)
    return None


# ------------------------------------------------------------------ warehouse (read-only)

class LockError(Exception):
    pass


class ReadOnlyError(ValueError):
    """A statement the read-only helper does not run (it could write, or it is more than one statement)."""


# first keyword of a statement the helper runs; anything else (DML, DDL, USE, SET, CALL, BEGIN, PUT ...) is refused
READ_FIRST = {"select", "with", "show", "describe", "desc", "explain", "values", "table", "from", "pragma", "summarize",
              "list", "ls"}
# write forms anywhere in the statement body (WITH ... INSERT / MERGE, EXPLAIN of a write, procedure calls); narrow
# phrases, so that a column named like a keyword (commit, drop ...) still reads
WRITE_BODY = re.compile(r"\b(?:insert\s+(?:overwrite\s+)?into|update\s+[\w.\"$]+\s+(?:as\s+\w+\s+)?set\s|delete\s+from|"
                        r"merge\s+into|copy\s+into|call\s+[\w.\"$]+\s*\(|execute\s+(?:immediate|task)\b)", re.I)


def _strip_sql(sql):
    """The statement with comments removed and string literals / quoted identifiers blanked (their content cannot
    hide a keyword or a statement separator). Handles '...', "...", $$...$$, --, // and /* */."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c, two = sql[i], sql[i:i + 2]
        if two in ("--", "//"):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif two == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif two == "$$":
            j = sql.find("$$", i + 2)
            i = n if j < 0 else j + 2
            out.append("''")
        elif c in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c == "'":
                    j += 2
                    continue
                if sql[j] == c:
                    if sql[j + 1:j + 2] == c:  # doubled quote inside the literal
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            out.append("''" if c == "'" else '"x"')
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_only_violation(sql):
    """None when `sql` is one statement of a reading kind; else the reason it is refused. Comments, string literals
    and quoted identifiers cannot hide a second statement or a write keyword."""
    s = _strip_sql(sql or "").strip()
    parts = [p.strip() for p in s.split(";")]
    if len([p for p in parts if p]) > 1:
        return "more than one statement"
    s = next((p for p in parts if p), "")
    if not s:
        return "empty statement"
    first = re.match(r"[(\s]*([A-Za-z_]+)", s)
    if not first or first.group(1).lower() not in READ_FIRST:
        return f"statement type {(first.group(1).upper() if first else '?')} is not a read"
    m = WRITE_BODY.search(s)
    if m:
        return f"the statement contains a write ({' '.join(m.group(0).split()).upper()})"
    return None


def query_timeout():
    """Seconds a single warehouse query may run: the time left before the harness deadline (minus a margin),
    capped by $SCRYBE_QUERY_TIMEOUT_S; None when neither is set. Raises TimeoutError when the deadline has passed."""
    left = deadline_left()
    cap = os.environ.get("SCRYBE_QUERY_TIMEOUT_S")
    try:
        cap = float(cap) if cap else None
    except ValueError:
        cap = None
    if left is not None:
        if left <= 5:
            raise TimeoutError("the harness deadline has been reached; no further warehouse queries")
        cap = min(cap, left - 5) if cap else left - 5
    return None if cap is None else max(1, int(cap))


def duckdb_path(prof, *, strict_profile=False):
    p = (os.environ.get("DUCKDB_PATH")
         if not strict_profile and (os.environ.get("DB_TYPE") or "duckdb").lower() == "duckdb" else None)
    if not p:
        p = str(prof.get("path") or "")
        if p and not os.path.isabs(p) and prof.get("_project"):
            p = os.path.normpath(os.path.join(prof["_project"], p))
    if not strict_profile and (not p or not os.path.isfile(p)):
        hits = sorted(glob.glob("/app/*.duckdb") + glob.glob("/app/*/*.duckdb"))
        p = hits[0] if hits else p
    return p


def _sf_private_key(out):
    from cryptography.hazmat.primitives import serialization
    path = out.get("private_key_path") or out.get("private_key_file")
    data = str(out.get("private_key") or "").strip()
    pw = str(out.get("private_key_passphrase") or "") or None
    if path:
        raw, der = open(os.path.expanduser(str(path)), "rb").read(), False
    else:
        if "\\n" in data and "\n" not in data:
            data = data.replace("\\n", "\n")
        der = not data.startswith("-")
        if der:
            import base64
            raw = base64.b64decode(data)
        else:
            raw = data.encode()
    load = serialization.load_der_private_key if der else serialization.load_pem_private_key
    try:
        key = load(raw, password=pw.encode() if pw else None)
    except TypeError:
        key = load(raw, password=None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class Warehouse:
    """One read-only session. DuckDB: opened read_only (retries while dbt holds the write lock) and closed by
    close(). Snowflake has no read-only session mode, so the session is made as close to one as the connector allows:
    the role of $SCRYBE_SNOWFLAKE_READ_ROLE when set (a role without write grants is the real boundary), autocommit
    off with a rollback on close (DML cannot persist), single-statement execution, a query tag, and a statement
    check that refuses anything but one reading statement. Every query is bounded by the harness deadline
    (query_timeout): a Snowflake statement timeout, a DuckDB interrupt. strict_project requires the selected
    project's profile and its DuckDB path, with no global database or discovery fallback."""

    def __init__(self, prefer=None, *, strict_project=False):
        self.prof = conn_profile(prefer)
        if strict_project and (not prefer or not self.prof.get("_project") or
                               os.path.realpath(self.prof["_project"]) != os.path.realpath(prefer)):
            raise ValueError("cannot resolve the selected project's active warehouse profile; no fallback permitted")
        self.engine = str(self.prof.get("type") or "").lower()
        self.con = None
        self.location = ""
        if self.engine == "snowflake":
            import snowflake.connector
            kw = {k: self.prof.get(k) for k in ("account", "user", "password", "role", "warehouse", "database", "schema")
                  if self.prof.get(k)}
            if any(self.prof.get(k) for k in ("private_key_path", "private_key_file", "private_key")):
                kw["private_key"] = _sf_private_key(self.prof)
                kw.pop("password", None)
            auth = str(self.prof.get("authenticator") or "")
            if auth and auth.lower() != "externalbrowser":
                kw["authenticator"] = auth
            if os.environ.get("SCRYBE_SNOWFLAKE_READ_ROLE"):
                kw["role"] = os.environ["SCRYBE_SNOWFLAKE_READ_ROLE"]
            params = {"QUERY_TAG": "scrybe-read-only", "MULTI_STATEMENT_COUNT": 1}
            t = query_timeout()
            if t:
                params["STATEMENT_TIMEOUT_IN_SECONDS"] = t
            self.con = snowflake.connector.connect(login_timeout=30, network_timeout=120, autocommit=False,
                                                   session_parameters=params, **kw)
            self.location = f"snowflake database {self.prof.get('database') or '?'}"
        elif self.engine in ("duckdb", ""):
            import duckdb
            path = duckdb_path(self.prof, strict_profile=strict_project)
            if not path or not os.path.isfile(path):
                raise FileNotFoundError("no DuckDB database file found (profile path / DUCKDB_PATH)")
            last = None
            for i in range(5):
                try:
                    self.con = duckdb.connect(path, read_only=True)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    if "lock" not in str(e).lower():
                        raise
                    time.sleep(2)
            else:
                raise LockError(f"cannot open {path} read-only (another process holds the write lock): {last}")
            self.engine = "duckdb"
            self.location = f"duckdb file {path}"
        else:
            raise RuntimeError(f"unsupported warehouse type {self.engine!r}")

    def query(self, sql, max_rows=200):
        """-> (columns, rows, more). Raises ReadOnlyError (Snowflake) for a statement that is not one read, and
        TimeoutError once the harness deadline has passed."""
        t = query_timeout()
        if self.engine == "snowflake":
            why = read_only_violation(sql)
            if why:
                raise ReadOnlyError(f"read-only session: {why}; data changes go through dbt")
            cur = self.con.cursor()
            try:
                cur.execute(sql, timeout=t) if t else cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1) if cols else []
            finally:
                cur.close()
        else:
            timer = None
            if t and hasattr(self.con, "interrupt"):
                import threading
                timer = threading.Timer(t, self.con.interrupt)
                timer.daemon = True
                timer.start()
            try:
                cur = self.con.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1) if cols else []
            finally:
                if timer:
                    timer.cancel()
        return cols, [tuple(r) for r in rows[:max_rows]], len(rows) > max_rows

    def relations(self):
        """[(schema, name, kind)] of every table/view outside the system schemas, sorted."""
        sql = ("select table_schema, table_name, table_type from information_schema.tables "
               "where lower(table_schema) not in ('information_schema', 'pg_catalog') order by 1, 2")
        _, rows, _ = self.query(sql, 200000)
        return [(str(s), str(n), "view" if "VIEW" in str(t).upper() else "table") for s, n, t in rows]

    def columns(self):
        """{(schema_lower, name_lower): [(column, type)]} in ordinal order."""
        sql = ("select table_schema, table_name, column_name, data_type from information_schema.columns "
               "where lower(table_schema) not in ('information_schema', 'pg_catalog') "
               "order by table_schema, table_name, ordinal_position")
        _, rows, _ = self.query(sql, 2000000)
        out = {}
        for s, t, c, ty in rows:
            out.setdefault((str(s).lower(), str(t).lower()), []).append((str(c), str(ty)))
        return out

    def close(self):
        try:
            if self.con is not None and self.engine == "snowflake":
                self.con.rollback()  # autocommit is off: nothing a statement did persists
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.con is not None:
                self.con.close()
        except Exception:  # noqa: BLE001
            pass
        self.con = None


def qident(x):
    return '"' + str(x).replace('"', '""') + '"'


def qrel(schema, name, database=None):
    return ".".join(qident(p) for p in (database, schema, name) if p)


def fmt_rows(cols, rows, more=False, width=60):
    def f(v):
        s = "NULL" if v is None else str(v)
        return s if len(s) <= width else s[: width - 3] + "..."
    lines = [" | ".join(str(c) for c in cols)] + [" | ".join(f(v) for v in r) for r in rows]
    lines.append(f"[TRUNCATED: first {len(rows)} rows shown, more rows exist; rerun with a larger max_rows or a narrower "
                 "query before concluding that anything is absent]" if more else f"[{len(rows)} rows]")
    return "\n".join(lines)


# ------------------------------------------------------------------ project files

def project_files(proj):
    """Relative paths of the project's own files (everything except build output, logs, installed packages)."""
    out = []
    for d, dirs, files in os.walk(proj):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for f in files:
            if f in (".user.yml",) or f.endswith((".pyc", ".duckdb", ".duckdb.wal", ".msgpack")):
                continue
            out.append(os.path.relpath(os.path.join(d, f), proj))
    return sorted(out)


def file_hashes(proj):
    out = {}
    for rel in project_files(proj):
        try:
            with open(os.path.join(proj, rel), "rb") as fh:
                out[rel] = hashlib.sha1(fh.read()).hexdigest()
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ file paths named in the task (finish check iv)

PATH_RE = re.compile(r"(?<![\w/.:-])((?:/[\w.-]+)+/[\w.-]+\.(?:sql|ya?ml|csv|md|py|json|txt)|"
                     r"(?:[\w.-]+/)+[\w.-]+\.(?:sql|ya?ml|csv|md|py|json|txt))(?![\w/-])")


def _path_matches(text):
    """(path, start, end) of every file path written out in the text; placeholders, globs, URLs and build/package
    output paths are not paths of a deliverable."""
    for m in PATH_RE.finditer(text or ""):
        p = m.group(1).rstrip(".")
        start = m.start(1)
        if text[max(0, start - 3):start].endswith("://") or any(ch in p for ch in "*<>{}$"):
            continue
        if re.search(r"(^|/)(target|dbt_packages|logs|\.venv|node_modules)/", p):
            continue
        yield p, start, start + len(p)


def task_paths(text):
    """File paths written out in the task text (with a directory part and a file extension), in order of first
    appearance."""
    out = []
    for p, _, _ in _path_matches(text):
        if p not in out:
            out.append(p)
    return out


# A mention is not an output obligation when its clause prohibits, removes or replaces the path: a cue before it
# ("do not create", "remove", "rename ... from", "instead of", "without" ...) that no later conjunction or
# "to / into / as / with" target releases, or a removal statement right after it ("... should be deleted").
NOT_OBLIGED_BEFORE = re.compile(
    r"\b(?:do\s*n[o'’]?t|don['’]t|doesn['’]t|never|avoid\w*|without|must\s*not|mustn['’]t|should\s*not|"
    r"shouldn['’]t|cannot|can['’]t|no\s+longer|not|instead\s+of|rather\s+than|except|exclud\w*|remov\w*|delet\w*|"
    r"drop(?:s|ped|ping)?|renam\w*|deprecat\w*|retir\w*|get\s+rid\s+of|eliminat\w*|discard\w*|"
    r"mov(?:e|es|ed|ing)|replac(?:e|es|ed|ing))\b", re.I)
# A comma does not release a cue ("do not create A, B"): an unreleased mention only loses a once-only notice, while a
# wrongly released one costs a rejection
RELEASE = re.compile(r"\bPATH\b[^\w]*(?:\w+\W+){0,3}?(?:and|but|then|instead|so)\b|"
                     r"\b(?:to|into|as|with|by)\s*[`'\"(]*$|\bbut\b|\bthen\b", re.I)
NOT_OBLIGED_AFTER = re.compile(
    r"^[`'\")\s]*(?:(?:\w+\s+){0,3}?(?:should|must|can|needs?\s+to|has\s+to|have\s+to|is\s+to|are\s+to|will|to)\s+"
    r"(?:also\s+|now\s+)?(?:be\s+)?(?:removed|deleted|dropped|renamed|retired|deprecated|replaced)\b|"
    r"(?:is|are|was|were)\s+(?:now\s+)?(?:no\s+longer|not)\s+(?:needed|used|required|necessary|wanted)\b|"
    r"(?:is|are)\s+(?:now\s+)?(?:obsolete|deprecated|unused|redundant)\b|(?:should|must)\s+not\s+(?:exist|be\s+created)\b)",
    re.I)
_CLAUSE_END = re.compile(r"[.!?;]\s|\n")


def task_path_mentions(text):
    """{path: (kind, context)} for the paths of task_paths(text): kind "obligation" when at least one mention reads
    as a path to produce or keep, else "not_obliged" (every mention prohibits, removes or replaces it)."""
    out = {}
    for p, a, b in _path_matches(text):
        before = text[max(0, a - 240):a]
        cuts = list(_CLAUSE_END.finditer(before))
        before = PATH_RE.sub(" PATH ", before[cuts[-1].end():] if cuts else before)
        after = text[b:b + 160]
        m = _CLAUSE_END.search(after)
        after = PATH_RE.sub(" PATH ", after[:m.start()] if m else after)
        cue = list(NOT_OBLIGED_BEFORE.finditer(before))
        neg = bool(cue) and not RELEASE.search(before[cue[-1].end():])
        neg = neg or bool(NOT_OBLIGED_AFTER.search(after))
        kind = "not_obliged" if neg else "obligation"
        ctx = " ".join((before[-80:] + " <PATH> " + after[:60]).split())
        if p not in out or (out[p][0] == "not_obliged" and kind == "obligation"):
            out[p] = (kind, ctx)
    return out


def path_candidates(p, projects):
    """Absolute locations a task-named path can refer to: itself if absolute, else relative to every dbt project,
    /app and the working directory."""
    if os.path.isabs(p):
        return [p]
    bases = list(projects) + [b for b in ("/app", os.getcwd()) if b not in projects]
    return [os.path.normpath(os.path.join(b, p)) for b in bases]


def path_exists(p, projects):
    return any(os.path.isfile(c) for c in path_candidates(p, projects))
