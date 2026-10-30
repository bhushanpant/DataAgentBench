"""Sage layer builder (untuned core). Builds, at task start and without the task text, a description of the
environment's dbt projects and of the warehouse they point to:

  PROJECT  every dbt project found (dbt_project.yml): models, sources, seeds, snapshots with paths, materialization /
           alias / schema config, ref()/source() lineage (package and version kept), YAML descriptions, columns and
           declared tests. Source of the facts, per project, in this order:
             1. target/manifest.json when it matches the project files (same model/seed/snapshot files, newer than
                every project file, same adapter type);
             2. a manifest written by `dbt parse` into the harness state directory ([sage].manifest_parse; the project
                directory is not written);
             3. else a regex scan of the project files: partial discovery (ref()/source() calls, config() literals,
                dbt_project.yml folder configs; Jinja, macros and vars are not evaluated).
  WAREHOUSE  the relations that exist in the warehouse now (information_schema, with catalog), resolved to dbt nodes by
           fully qualified expected identity: the manifest's database/schema/alias, or (regex scan) the declared source
           schema/identifier and dbt's default schema rule for models. A node whose expected relation is not found, or
           is matched by more than one relation, gets no relation: the candidates are recorded as candidates, never as
           the node's data. A profile (row count; per column type, NULL %, distinct count, min/max, most frequent
           values of low-cardinality columns) for up to [sage].profile_max_relations relations in a fixed order;
           measured match, multiplicity and fanout for the joins the project declares with `relationships` tests.
  OFFLINE  an optional offline warehouse profile file ([sage].warehouse_layer), loaded as is, plus a live check at
           start of which of its tables exist here with the same column names, type families and row counts (values
           are not compared). The file may instead be an offline DATABASE PROFILE VIEW (sage_dbview.py, format
           scrybe-sage-view/1: a precomputed profile of the logical databases of a multi-database environment); it is
           loaded as is, rendered by sage_dbview, and, when the environment has no dbt project and no warehouse
           connection settings, the warehouse part is recorded as absent instead of attempted.

Every warehouse query is bounded by [sage].query_timeout_s and by the build deadline (DuckDB: interrupt; Snowflake:
statement timeout); the project scan stops at the deadline too. Each part records its source identity, method,
coverage and time. The result depends only on the project files, the warehouse contents and the configured limits;
profile_named() profiles extra relations chosen by the caller (the start hook passes the task-named ones). Every limit
that leaves something out is recorded in the layer (and stated by the renderer). See sage_render.py for the text.
No secret (password, key, token, account) is written into the layer: errors are redacted.
"""
import json
import os
import re
import tempfile
import threading
import time

import scrybe_core as core

FORMAT = "scrybe-sage-core/2"
# Offline database profile view (sage_dbview.VIEW_FORMAT): a precomputed profile of the logical databases of a
# multi-database environment, accepted as the offline layer input ([sage].warehouse_layer).
DBVIEW_FORMAT = "scrybe-sage-view/1"
LAYER_FILE = "sage/layer.json"
REF_RE = re.compile(r"\bref\(\s*['\"]([\w.-]+)['\"]\s*(?:,\s*['\"]([\w.-]+)['\"]\s*)?"
                    r"(?:,\s*v(?:ersion)?\s*=\s*['\"]?([\w.]+)['\"]?\s*)?\)")
SRC_RE = re.compile(r"\bsource\(\s*['\"]([\w.-]+)['\"]\s*,\s*['\"]([\w.-]+)['\"]\s*\)")
CFG_RE = re.compile(r"\{\{\s*config\s*\((.*?)\)\s*\}\}", re.S)
KV_RE = re.compile(r"\b(materialized|alias|schema|database|unique_key|incremental_strategy|target_schema|target_database)"
                   r"\s*=\s*['\"]([^'\"]*)['\"]")
SCHEMA_MACRO_RE = re.compile(r"\{%-?\s*macro\s+generate_(schema|alias|database)_name\s*\(")
FOLDER_KEYS = ("materialized", "schema", "alias", "database", "unique_key", "target_schema", "target_database", "enabled")
NUMERIC = re.compile(r"int|float|double|real|decimal|numeric|number|hugeint", re.I)
TEMPORAL = re.compile(r"date|time", re.I)
TEXTUAL = re.compile(r"char|text|string|varchar", re.I)
BOOLEAN = re.compile(r"bool", re.I)
COMPLEX = re.compile(r"struct|map|list|array|\[\]|variant|object|json|blob|binary|geometry|union", re.I)
# Locations of benchmark test/solution material; never read as a project, whatever a search root contains.
OFF_LIMITS = re.compile(r"^/(solutions?|tests?|verifier|grader|logs/verifier)(/|$)")
SECRET_KEYS = ("password", "private_key", "private_key_passphrase", "private_key_path", "private_key_file", "token",
               "passphrase", "account", "user", "client_secret", "oauth_client_secret", "refresh_token")
SCAN_METHOD = ("regex scan of the project files (partial discovery: ref()/source() calls, config() literals, YAML, "
               "dbt_project.yml folder configs; Jinja, macros and vars are not evaluated)")


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Redactor:
    """Replaces the connection's secret values (and the account identifier) in any text kept in the layer."""

    def __init__(self, prefer=None):
        try:
            prof = core.conn_profile(prefer)
        except Exception:  # noqa: BLE001
            prof = {}
        vals = [str(prof.get(k)) for k in SECRET_KEYS if prof.get(k)]
        vals += [os.environ[k] for k in os.environ if re.search(r"PASSWORD|PRIVATE_KEY|TOKEN|SECRET|ACCOUNT", k)
                 and len(os.environ[k]) >= 4]
        self.vals = sorted({v for v in vals if len(v) >= 4}, key=len, reverse=True)

    def __call__(self, text):
        s = str(text or "")
        for v in self.vals:
            s = s.replace(v, "***")
        return s


def _err(e, n=200, redact=None):
    s = f"{type(e).__name__}: {str(e).splitlines()[0][:n] if str(e) else ''}"
    return redact(s) if redact else s


# ===================================================================== bounded warehouse queries

def run_query(wh, sql, max_rows, stop_at=None, per_query_s=None):
    """wh.query bounded by min(per_query_s, time to stop_at): DuckDB is interrupted, Snowflake gets a statement
    timeout. Raises TimeoutError when the bound is reached (or already passed)."""
    left = min(x for x in (per_query_s or 1e9, (stop_at - time.time()) if stop_at else 1e9))
    if left <= 0.05:
        raise TimeoutError("not run: the Sage time budget was used up")
    t0 = time.time()
    if wh.engine == "snowflake":
        check = getattr(core, "read_only_violation", None)
        why = check(sql) if check else None
        if why:
            raise ValueError(f"read-only session: {why}")
        cur = wh.con.cursor()
        try:
            try:
                cur.execute(sql, timeout=max(1, int(left)))
            except Exception as e:  # noqa: BLE001
                if time.time() - t0 >= max(1, int(left)) - 0.5:
                    raise TimeoutError(f"query stopped after {int(left)} s (Sage time limit)") from e
                raise
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1) if cols else []
        finally:
            cur.close()
        return cols, [tuple(r) for r in rows[:max_rows]], len(rows) > max_rows
    fired = []

    def stop():
        fired.append(1)
        try:
            wh.con.interrupt()
        except Exception:  # noqa: BLE001
            pass
    t = threading.Timer(left, stop)
    t.daemon = True
    t.start()
    try:
        return wh.query(sql, max_rows)
    except Exception as e:  # noqa: BLE001
        if fired:
            raise TimeoutError(f"query stopped after {left:.0f} s (Sage time limit)") from e
        raise
    finally:
        t.cancel()


def _relations(wh, stop_at, per_q):
    """[(schema, name, kind, catalog)] of every table/view outside the system schemas, sorted."""
    sql = ("select table_catalog, table_schema, table_name, table_type from information_schema.tables "
           "where lower(table_schema) not in ('information_schema', 'pg_catalog')"
           + (" and lower(table_catalog) not in ('system', 'temp')" if wh.engine == "duckdb" else "") + " order by 1, 2, 3")
    _, rows, _ = run_query(wh, sql, 500000, stop_at, per_q)
    return [(str(s), str(n), "view" if "VIEW" in str(t).upper() else "table", str(c)) for c, s, n, t in rows]


def _columns(wh, stop_at, per_q):
    """{(catalog_lower, schema_lower, name_lower): [(column, type)]} in ordinal order."""
    sql = ("select table_catalog, table_schema, table_name, column_name, data_type from information_schema.columns "
           "where lower(table_schema) not in ('information_schema', 'pg_catalog') "
           "order by table_catalog, table_schema, table_name, ordinal_position")
    _, rows, _ = run_query(wh, sql, 5000000, stop_at, per_q)
    out = {}
    for c, s, t, col, ty in rows:
        out.setdefault((str(c).lower(), str(s).lower(), str(t).lower()), []).append((str(col), str(ty)))
    return out


def rel_key(r, multi):
    """Profile key of a relation [schema, name, kind, catalog]: schema.name, or catalog.schema.name when the warehouse
    lists more than one catalog."""
    return ".".join(([r[3]] if multi and len(r) > 3 and r[3] else []) + [r[0], r[1]])


# ===================================================================== project files (regex scan)

def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _sql_config(text):
    cfg = {}
    for m in CFG_RE.finditer(text):
        for k, v in KV_RE.findall(m.group(1)):
            cfg.setdefault(k, v)
    return cfg


def _folder_config(section, project_name, parts):
    """dbt_project.yml `models:`/`seeds:`/`snapshots:` configs that apply to a file in folder `parts` (later levels
    win; `+key` and plain key forms)."""
    out = {}

    def take(d):
        for k, v in (d or {}).items():
            kk = str(k).lstrip("+")
            if kk in FOLDER_KEYS and not isinstance(v, dict):
                out[kk] = v
    if not isinstance(section, dict):
        return out
    take(section)
    node = section.get(project_name)
    for part in [None] + list(parts):
        if part is not None:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            break
        take(node)
    return out


def _ref_display(pkg, name, version, own):
    return f"{pkg + '.' if pkg and pkg != own else ''}{name}{' v' + str(version) if version not in (None, '') else ''}"


def _refs(text, own):
    ids = []
    for m in REF_RE.finditer(text):
        a, b, v = m.group(1), m.group(2), m.group(3)
        pkg, name = (None, a) if b is None else (a, b)
        rid = {"package": pkg, "name": name, "version": v}
        if rid not in ids:
            ids.append(rid)
    ids.sort(key=lambda r: (r["name"].lower(), r["package"] or "", str(r["version"] or "")))
    return [_ref_display(r["package"], r["name"], r["version"], own) for r in ids], ids


def _test_name(t):
    """A declared test -> (short text, kind, kwargs)."""
    if isinstance(t, str):
        return t, t, {}
    if isinstance(t, dict) and t:
        k = next(iter(t))
        kw = t[k] if isinstance(t[k], dict) else {}
        if "test_name" in kw:  # long form: {name: ..., test_name: ...}
            k = kw["test_name"]
        args = kw.get("arguments") if isinstance(kw.get("arguments"), dict) else kw
        if k == "accepted_values":
            vals = args.get("values") or []
            return f"accepted_values({', '.join(map(str, vals))})", k, args
        if k == "relationships":
            return f"relationships(to {args.get('to')} field {args.get('field')})", k, args
        bits = ", ".join(f"{a}={args[a]}" for a in sorted(args)
                         if a not in ("config", "severity", "name", "description", "model", "column_name"))
        return (f"{k}({bits})" if bits else str(k)), str(k), args
    return str(t), str(t), {}


def _columns_from_yaml(cols):
    out = []
    for c in cols or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        tests = [_test_name(t)[0] for t in (c.get("tests") or []) + (c.get("data_tests") or [])]
        out.append({"name": str(c["name"]), "description": str(c.get("description") or "").strip(),
                    "data_type": str(c.get("data_type") or ""), "tests": tests,
                    "_rels": [_test_name(t)[2] for t in (c.get("tests") or []) + (c.get("data_tests") or [])
                              if _test_name(t)[1] == "relationships"]})
    return out


def _base_project(proj, package):
    meta = core.project_meta(proj)
    p = {"root": proj, "name": meta.get("name"), "package": package, "notes": [], "models": [], "sources": [],
         "seeds": [], "snapshots": [], "macros": 0, "singular_tests": 0}
    if meta.get("error"):
        p["notes"].append(meta["error"])
    if package is None:
        prof, tgt, out = core.profile_of(proj)
        p.update(profile=prof, target=tgt, type=str(out.get("type") or "") or None, schema=out.get("schema"),
                 database=out.get("database") if out.get("type") == "snowflake" else None)
        try:
            pk = core.read_yaml(os.path.join(proj, "packages.yml")) if os.path.isfile(os.path.join(proj, "packages.yml")) else None
            p["packages_declared"] = [str(x.get("package") or x.get("git") or x.get("local") or x) for x in (pk or {}).get("packages") or []]
        except Exception as e:  # noqa: BLE001
            p["notes"].append(f"packages.yml unreadable: {str(e).splitlines()[0][:160]}")
            p["packages_declared"] = []
        pkg_dir = os.path.join(proj, "dbt_packages")
        p["packages_installed"] = sorted(d for d in os.listdir(pkg_dir) if os.path.isdir(os.path.join(pkg_dir, d))) \
            if os.path.isdir(pkg_dir) else []
    return p, meta


def _walk(proj, paths, ext):
    out = []
    for top in paths:
        base = os.path.join(proj, top)
        for d, dirs, files in os.walk(base):
            dirs[:] = sorted(x for x in dirs if x not in core.SKIP_DIRS and not x.startswith("."))
            out += [os.path.relpath(os.path.join(d, f), proj) for f in sorted(files) if f.endswith(ext)]
    return sorted(set(out))


def _under(rel, paths):
    """Folder parts of `rel` below the model/seed/snapshot path that contains it."""
    for top in paths:
        t = os.path.normpath(top)
        if rel == t or rel.startswith(t + os.sep):
            return os.path.dirname(os.path.relpath(rel, t)).split(os.sep) if os.path.dirname(os.path.relpath(rel, t)) else []
    return []


def scan_project(proj, package=None, stop_at=None):
    """-> project dict from files only (regex scan). package=None for the project itself, else the installed package
    name. Stops reading model files at stop_at and records how many were read."""
    p, meta = _base_project(proj, package)
    own = p.get("name")
    p["discovery"] = {"method": "regex scan", "text": SCAN_METHOD}
    try:
        pdoc = core.read_yaml(os.path.join(proj, "dbt_project.yml")) or {}
    except Exception:  # noqa: BLE001
        pdoc = {}
    yml_models, yml_sources, yml_snap, yml_seed = {}, [], {}, {}
    yml_files = []
    for d, dirs, files in os.walk(proj):
        dirs[:] = sorted(x for x in dirs if x not in core.SKIP_DIRS and not x.startswith("."))
        rel_d = os.path.relpath(d, proj)
        for f in sorted(files):
            if f.endswith((".yml", ".yaml")) and f not in ("dbt_project.yml", "profiles.yml", "packages.yml",
                                                             "package-lock.yml", "dependencies.yml", "selectors.yml"):
                yml_files.append(os.path.normpath(os.path.join(rel_d, f)))
    for rel in yml_files:
        if stop_at and time.time() > stop_at:
            p["scan_incomplete"] = f"YAML files: stopped by the time budget after {yml_files.index(rel)} of {len(yml_files)}"
            break
        try:
            doc = core.read_yaml(os.path.join(proj, rel)) or {}
        except Exception as e:  # noqa: BLE001
            p["notes"].append(f"{rel}: YAML not readable ({str(e).splitlines()[0][:120]})")
            continue
        if not isinstance(doc, dict):
            continue
        for m in doc.get("models") or []:
            if isinstance(m, dict) and m.get("name"):
                yml_models.setdefault(str(m["name"]).lower(), (m, rel))
        for s in doc.get("snapshots") or []:
            if isinstance(s, dict) and s.get("name"):
                yml_snap.setdefault(str(s["name"]).lower(), (s, rel))
        for s in doc.get("seeds") or []:
            if isinstance(s, dict) and s.get("name"):
                yml_seed.setdefault(str(s["name"]).lower(), (s, rel))
        for s in doc.get("sources") or []:
            if isinstance(s, dict) and s.get("name"):
                yml_sources.append((s, rel))

    model_files = _walk(proj, meta["model-paths"], (".sql", ".py"))
    for i, rel in enumerate(model_files):
        if stop_at and time.time() > stop_at:
            p["scan_incomplete"] = f"model files: stopped by the time budget after {i} of {len(model_files)}"
            break
        try:
            text = _read(os.path.join(proj, rel))
        except OSError as e:
            p["notes"].append(f"{rel}: unreadable ({e})")
            continue
        name = os.path.splitext(os.path.basename(rel))[0]
        cfg = _sql_config(text)
        y, yrel = yml_models.get(name.lower(), (None, None))
        version = None
        if y is None:  # versioned model file <name>_v<N>
            mv = re.match(r"(.+)_v(\d+)$", name)
            if mv and mv.group(1).lower() in yml_models:
                y, yrel = yml_models[mv.group(1).lower()]
                version = mv.group(2)
        y = y or {}
        ycfg = y.get("config") if isinstance(y.get("config"), dict) else {}
        fcfg = _folder_config(pdoc.get("models"), own, _under(rel, meta["model-paths"]))
        refs, ref_ids = _refs(text, own)
        srcs = sorted({f"{a}.{b}" for a, b in SRC_RE.findall(text)})
        mtests = [_test_name(t)[0] for t in (y.get("tests") or []) + (y.get("data_tests") or [])]

        def pick(k):
            return cfg.get(k) or ycfg.get(k) or (str(fcfg[k]) if fcfg.get(k) not in (None, "") else None)
        p["models"].append({
            "name": name, "path": rel, "yml": yrel, "language": "python" if rel.endswith(".py") else "sql",
            "materialized": pick("materialized"), "alias": pick("alias"), "schema_config": pick("schema"),
            "database_config": pick("database"), "unique_key": pick("unique_key"), "version": version,
            "enabled": fcfg.get("enabled"),
            "description": str(y.get("description") or "").strip(), "columns": _columns_from_yaml(y.get("columns")),
            "tests": mtests, "refs": refs, "ref_ids": ref_ids, "sources": srcs,
            "versions": [str(v.get("v")) for v in y.get("versions") or [] if isinstance(v, dict)]})
    for s, rel in yml_sources:
        for t in s.get("tables") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            p["sources"].append({
                "source": str(s["name"]), "name": str(t["name"]), "yml": rel,
                "schema": str(t.get("schema") or s.get("schema") or s["name"]),
                "database": str(t.get("database") or s.get("database") or "") or None,
                "identifier": str(t.get("identifier") or t["name"]),
                "description": str(t.get("description") or s.get("description") or "").strip(),
                "columns": _columns_from_yaml(t.get("columns")),
                "tests": [_test_name(x)[0] for x in (t.get("tests") or []) + (t.get("data_tests") or [])]})
    for r in _walk(proj, meta["seed-paths"], (".csv",)):
        name = os.path.splitext(os.path.basename(r))[0]
        y = (yml_seed.get(name.lower()) or ({}, None))[0]
        ycfg = y.get("config") if isinstance(y.get("config"), dict) else {}
        fcfg = _folder_config(pdoc.get("seeds"), own, _under(r, meta["seed-paths"]))
        p["seeds"].append({"name": name, "path": r, "alias": ycfg.get("alias") or fcfg.get("alias"),
                           "schema_config": ycfg.get("schema") or fcfg.get("schema"),
                           "description": str(y.get("description") or "").strip(),
                           "columns": _columns_from_yaml(y.get("columns"))})
    for r in _walk(proj, meta["snapshot-paths"], (".sql",)):
        text = _read(os.path.join(proj, r))
        fcfg = _folder_config(pdoc.get("snapshots"), own, _under(r, meta["snapshot-paths"]))
        for m in re.finditer(r"\{%-?\s*snapshot\s+(\w+)\s*-?%\}", text):
            cfg = _sql_config(text)
            refs, ref_ids = _refs(text, own)
            p["snapshots"].append({"name": m.group(1), "path": r, "refs": refs, "ref_ids": ref_ids,
                                   "target_schema": cfg.get("target_schema") or fcfg.get("target_schema"),
                                   "schema_config": cfg.get("schema") or fcfg.get("schema"),
                                   "alias": cfg.get("alias") or fcfg.get("alias"),
                                   "sources": sorted({f"{a}.{b}" for a, b in SRC_RE.findall(text)})})
    for name, (s, rel) in sorted(yml_snap.items()):
        if not any(x["name"].lower() == name for x in p["snapshots"]):
            rel_from = s.get("relation") or ""
            scfg = s.get("config") if isinstance(s.get("config"), dict) else {}
            p["snapshots"].append({"name": str(s["name"]), "path": rel, "refs": [], "ref_ids": [], "sources": [],
                                   "defined_in_yaml": True, "relation_expr": str(rel_from),
                                   "target_schema": scfg.get("target_schema"), "schema_config": scfg.get("schema")})
    macros = _walk(proj, meta["macro-paths"], (".sql",))
    p["macros"] = len(macros)
    over = set()
    for r in macros:
        try:
            over |= {m.group(1) for m in SCHEMA_MACRO_RE.finditer(_read(os.path.join(proj, r)))}
        except OSError:
            pass
    p["name_macros"] = sorted(over)  # generate_<schema|alias|database>_name overridden in the project (not evaluated)
    p["singular_tests"] = len(_walk(proj, meta["test-paths"], (".sql",)))
    return p


# ===================================================================== dbt manifest

def _project_node_files(proj, meta):
    return set(_walk(proj, meta["model-paths"], (".sql", ".py")) + _walk(proj, meta["seed-paths"], (".csv",))
               + _walk(proj, meta["snapshot-paths"], (".sql",)))


def _manifest_fresh(proj, path, man, meta, ptype):
    """-> None when target/manifest.json describes the project files as they are now, else the reason it does not."""
    md = man.get("metadata") or {}
    if ptype and md.get("adapter_type") and str(md["adapter_type"]).lower() != ptype.lower():
        return f"adapter {md['adapter_type']} differs from the profile type {ptype}"
    own = meta.get("name")
    in_man = {os.path.normpath(n.get("original_file_path") or "") for n in (man.get("nodes") or {}).values()
              if n.get("package_name") == own and n.get("resource_type") in ("model", "seed", "snapshot")}
    on_disk = {os.path.normpath(x) for x in _project_node_files(proj, meta)}
    if in_man != on_disk:
        return f"lists {len(in_man)} model/seed/snapshot files, the project has {len(on_disk)} (or other paths)"
    mt = os.path.getmtime(path)
    newest = max((os.path.getmtime(os.path.join(proj, r)) for r in core.project_files(proj)
                  if not r.startswith(("target" + os.sep, "logs" + os.sep))), default=0)
    if newest > mt:
        return "older than a project file"
    return None


def load_manifest(proj, cfg, stop_at, redact, parse=True, package_projects=None):
    """-> (manifest dict or None, discovery dict). Never writes inside the project directory. parse=False: only an
    existing manifest that matches the project files (no `dbt parse`)."""
    meta = core.project_meta(proj)
    ptype = core.project_backend(proj)
    tried = []
    tdir = meta.get("target-path") or os.path.join(proj, "target")
    path = os.path.join(tdir if os.path.isabs(tdir) else os.path.join(proj, tdir), "manifest.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                man = json.load(fh)
            why = _manifest_fresh(proj, path, man, meta, ptype)
            if why is None:
                return man, {"method": "manifest", "manifest": f"{os.path.relpath(path, proj)} (matches the project files)",
                             "dbt_version": (man.get("metadata") or {}).get("dbt_version"),
                             "generated_at": (man.get("metadata") or {}).get("generated_at")}
            tried.append(f"{os.path.relpath(path, proj)} not used: {why}")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{os.path.relpath(path, proj)} not readable: {_err(e, 120, redact)}")
    if not parse:
        tried.append("`dbt parse` not run: the project's profile type is not the active backend")
    elif cfg.get("manifest_parse", True):
        left = (stop_at - time.time()) if stop_at else 1e9
        timeout = min(float(cfg.get("manifest_parse_timeout_s", 60)), left * 0.6)
        if timeout < 5:
            tried.append("`dbt parse` not run: build time budget")
        else:
            tgt = core.state_path("sage", "dbt", re.sub(r"\W+", "_", proj).strip("_"), "target", "x")[:-2]
            env = {"DBT_PROFILES_DIR": core.profiles_dir(proj), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
            t0 = time.time()
            rc, out = core.sh(["dbt", "parse", "--target-path", tgt, "--log-path", os.path.join(os.path.dirname(tgt), "logs")],
                              cwd=proj, timeout=timeout, env=env)
            recovery = None
            if (rc != 0 and cfg.get("manifest_reuse_locked_packages", True)
                    and "package(s) specified in" in out and "only 0 package(s) installed" in out):
                # Reuse only an existing exact-pin cache in this environment. Parse in a
                # disposable project copy; never run deps or change the source project.
                from sage_package_cache import private_project
                try:
                    parse_end = min(t0 + timeout, stop_at or 1e18)
                    if parse_end - time.time() < 5:
                        raise TimeoutError("not enough manifest parse budget for package recovery")
                    with tempfile.TemporaryDirectory(prefix="package-recovery-", dir=os.path.dirname(tgt)) as tmp:
                        shadow, recovery = private_project(proj, package_projects or [],
                                                           os.path.join(tmp, "project"), parse_end - 3)
                        remaining = parse_end - time.time()
                        if remaining < 3:
                            raise TimeoutError("manifest parse budget exhausted during package recovery")
                        rc, out = core.sh(["dbt", "parse", "--no-partial-parse", "--target-path", tgt,
                                           "--log-path", os.path.join(os.path.dirname(tgt), "logs")],
                                          cwd=shadow, timeout=remaining, env=env)
                    tried.append("missing installed packages: parsed a private copy using an existing exact-pin cache")
                except Exception as e:  # noqa: BLE001 -- keep explicit regex fallback on unsupported recovery
                    tried.append(f"locked package recovery unavailable: {_err(e, 180, redact)}")
                    recovery = None
            secs = round(time.time() - t0, 1)
            mp = os.path.join(tgt, "manifest.json")
            if rc == 0 and os.path.isfile(mp):
                try:
                    with open(mp, encoding="utf-8") as fh:
                        man = json.load(fh)
                    return man, {"method": "manifest", "manifest": "written by `dbt parse` into the harness state directory",
                                 "dbt_version": (man.get("metadata") or {}).get("dbt_version"), "parse_secs": secs,
                                 "notes": tried, **({"package_recovery": recovery} if recovery else {})}
                except Exception as e:  # noqa: BLE001
                    tried.append(f"`dbt parse` manifest not readable: {_err(e, 120, redact)}")
            else:
                last = [ln for ln in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines() if ln.strip()][-1:] or [""]
                tried.append(f"`dbt parse` failed (exit {rc}, {secs} s): {redact(last[0][:200])}")
    return None, {"method": "regex scan", "text": SCAN_METHOD, "notes": tried}


def _mtest_text(tn):
    tm = tn.get("test_metadata") or {}
    kw = {k: v for k, v in (tm.get("kwargs") or {}).items() if k not in ("model", "column_name")}
    return _test_name({tm.get("name") or tn.get("name"): kw})


def project_from_manifest(proj, man):
    """-> (project dict, [package dicts]) from a dbt manifest."""
    p, meta = _base_project(proj, None)
    own = p.get("name")
    nodes = man.get("nodes") or {}
    srcs = man.get("sources") or {}

    def disp(uid):
        n = nodes.get(uid) or srcs.get(uid) or {}
        if uid.startswith("source."):
            return "source", f"{n.get('source_name')}.{n.get('name')}", None
        return "ref", _ref_display(n.get("package_name"), n.get("name"), n.get("version"), own), \
            {"package": n.get("package_name") if n.get("package_name") != own else None, "name": n.get("name"),
             "version": None if n.get("version") is None else str(n.get("version")), "unique_id": uid}
    col_tests, node_tests, rels = {}, {}, []
    singular = 0
    for uid, tn in nodes.items():
        if tn.get("resource_type") != "test":
            continue
        att = tn.get("attached_node") or next(iter((tn.get("depends_on") or {}).get("nodes") or []), None)
        tm = tn.get("test_metadata") or {}
        if not tm:
            singular += tn.get("package_name") == own
            continue
        text, kind, kw = _mtest_text(tn)
        col = tn.get("column_name") or (tm.get("kwargs") or {}).get("column_name")
        if col:
            col_tests.setdefault(att, {}).setdefault(str(col).lower(), []).append(text)
        else:
            node_tests.setdefault(att, []).append(text)
        if kind == "relationships" and col:
            other = [d for d in (tn.get("depends_on") or {}).get("nodes") or [] if d != att]
            rels.append({"left_uid": att, "left_column": str(col), "right_uid": other[0] if other else None,
                         "right_column": str(kw.get("field") or ""), "to": str(kw.get("to") or "")})

    def columns(uid, n):
        out, tests = [], col_tests.pop(uid, {})  # tests per node: a test may have no attached node (uid None)
        for c in (n.get("columns") or {}).values():
            out.append({"name": str(c.get("name")), "description": str(c.get("description") or "").strip(),
                        "data_type": str(c.get("data_type") or ""), "tests": tests.pop(str(c.get("name")).lower(), [])})
        for cn, ts in sorted(tests.items()):
            out.append({"name": cn, "description": "", "data_type": "", "tests": ts})
        return out

    def expected(n, name):
        return {"database": n.get("database"), "schema": n.get("schema"), "name": name, "rule": "dbt manifest"}
    pkgs = {}
    for uid, n in sorted(nodes.items(), key=lambda kv: (kv[1].get("original_file_path") or "", kv[0])):
        rt = n.get("resource_type")
        if rt not in ("model", "seed", "snapshot"):
            continue
        c = n.get("config") or {}
        if c.get("enabled") is False:
            continue
        deps = (n.get("depends_on") or {}).get("nodes") or []
        refs, ref_ids, sources = [], [], []
        for d in deps:
            k, text, rid = disp(d)
            (sources if k == "source" else refs).append(text)
            if rid:
                ref_ids.append(rid)
        uk = c.get("unique_key")
        x = {"name": n.get("name"), "unique_id": uid, "package": n.get("package_name"), "path": n.get("original_file_path"),
             "yml": (n.get("patch_path") or "").split("://")[-1] or None, "language": n.get("language") or "sql",
             "materialized": c.get("materialized"),
             "alias": n.get("alias") if n.get("alias") and n.get("alias") != n.get("name") else None,
             "schema_config": c.get("schema"), "unique_key": ", ".join(map(str, uk)) if isinstance(uk, list) else uk,
             "version": None if n.get("version") is None else str(n.get("version")),
             "description": str(n.get("description") or "").strip(), "columns": columns(uid, n),
             "tests": node_tests.get(uid, []), "refs": sorted(set(refs)), "ref_ids": ref_ids, "sources": sorted(set(sources)),
             "expected": expected(n, n.get("alias") or n.get("name"))}
        kind = rt + "s"
        if n.get("package_name") == own:
            p[kind].append(x)
        else:
            pkgs.setdefault(n.get("package_name"), {"models": [], "sources": []})
            if kind == "models":
                pkgs[n.get("package_name")]["models"].append(x)
    for uid, s in srcs.items():
        x = {"source": s.get("source_name"), "name": s.get("name"), "unique_id": uid, "package": s.get("package_name"),
             "yml": s.get("original_file_path"), "schema": s.get("schema"), "database": s.get("database"),
             "identifier": s.get("identifier") or s.get("name"),
             "description": str(s.get("description") or s.get("source_description") or "").strip(),
             "columns": columns(uid, s), "tests": node_tests.get(uid, []),
             "expected": expected(s, s.get("identifier") or s.get("name"))}
        if s.get("package_name") == own:
            p["sources"].append(x)
        else:
            pkgs.setdefault(s.get("package_name"), {"models": [], "sources": []})["sources"].append(x)
    macros = man.get("macros") or {}
    p["macros"] = len({m.get("original_file_path") for m in macros.values() if m.get("package_name") == own})
    p["name_macros"] = sorted({m.get("name")[9:-5] for m in macros.values() if m.get("package_name") == own and
                               re.fullmatch(r"generate_(schema|alias|database)_name", str(m.get("name")))})
    p["singular_tests"] = singular
    p["_rels"] = rels
    out_pk = [{"project": proj, "package": k, "name": k, "models": v["models"], "sources": v["sources"],
               "macros": len({m.get("original_file_path") for m in macros.values() if m.get("package_name") == k})}
              for k, v in sorted(pkgs.items())]
    return p, out_pk


# ===================================================================== warehouse

def _family(t):
    t = str(t or "")
    if COMPLEX.search(t):
        return "complex"
    if BOOLEAN.search(t):
        return "bool"
    if TEMPORAL.search(t) and not TEXTUAL.search(t):
        return "temporal"
    if NUMERIC.search(t):
        return "numeric"
    if TEXTUAL.search(t):
        return "text"
    return "other"


def profile_relation(wh, schema, name, cols, max_cols, top_max, database=None, stop_at=None, per_q=None, exact_columns=()):
    """One aggregate query (rows; per column non-null count, distinct count, min/max) + one query per
    low-cardinality text/bool column for its most frequent values. Full scans, no sampling. Distinct counts are exact
    on DuckDB and for explicitly requested exact_columns; otherwise approximate on other engines.
    Each column records which count was used; an approximate count is not a uniqueness test."""
    t0 = time.time()
    rel = core.qrel(schema, name, database)
    use = cols[:max_cols]
    exact = wh.engine == "duckdb"
    exact_columns = set(exact_columns)
    parts = ["count(*)"]
    for c, t in use:
        fam = _family(t)
        q = core.qident(c)
        distinct = f"count(distinct {q})" if exact or c in exact_columns else f"approx_count_distinct({q})"
        if fam == "complex":
            parts += [f"count({q})", "null", "null", "null"]
        elif fam == "bool":
            parts += [f"count({q})", distinct, "null", "null"]
        else:
            parts += [f"count({q})", distinct, f"cast(min({q}) as varchar)", f"cast(max({q}) as varchar)"]
    _, rows, _ = run_query(wh, "select " + ", ".join(parts) + f" from {rel}", 1, stop_at, per_q)
    r = rows[0]
    n = int(r[0] or 0)
    out = {"rows": n, "columns": [], "catalog_columns": [list(c) for c in cols],
           "columns_total": len(cols), "distinct_exact": exact, "measured_at": now_utc(),
           "method": "full scan (no sampling)"}
    for i, (c, t) in enumerate(use):
        nn, dist, lo, hi = r[1 + 4 * i: 5 + 4 * i]
        col = {"name": c, "type": t, "family": _family(t),
               "null_pct": round(100.0 * (n - int(nn or 0)) / n, 2) if n else None}
        if dist is not None:
            col["distinct"] = int(dist)
            col["distinct_exact"] = exact or c in exact_columns
        if lo is not None:
            col["min"], col["max"] = str(lo), str(hi)
        out["columns"].append(col)
    for col in out["columns"]:
        fam = _family(col["type"])
        if fam in ("text", "bool") and n and 0 < col.get("distinct", 0) <= top_max:
            q = core.qident(col["name"])
            try:
                _, vr, _ = run_query(wh, f"select cast({q} as varchar) v, count(*) n from {rel} group by 1 order by 2 desc, 1 "
                                         f"limit {top_max}", top_max, stop_at, per_q)
                col["top"] = [[None if v is None else str(v), int(k)] for v, k in vr]
            except TimeoutError:
                out["top_values_cut"] = "frequent values not recorded for every column: time limit"
                break
    if len(cols) > max_cols:
        out["columns_cut"] = f"statistics for the first {max_cols} of {len(cols)} columns"
    out["secs"] = round(time.time() - t0, 2)
    return out


def resolve(exp, by_name, wh_db, project_backend=None, warehouse_backend=None):
    """Expected identity {database, schema(s), name, rule} -> {status, relation?, candidates, how, expected}.
    Only an exact (case-insensitive) catalog/schema/name match resolves; no fallback to another schema."""
    name = str(exp.get("name") or "")
    schemas = [s for s in (exp.get("schemas") or [exp.get("schema")]) if s]
    db = exp.get("database")
    cands = by_name.get(name.lower()) or []
    expected = ".".join(x for x in ([db] if db else []) + ["|".join(schemas) or "?", name])
    out = {"expected": expected, "how": exp.get("rule")}
    if project_backend and warehouse_backend and project_backend.lower() != warehouse_backend.lower():
        out.update(status="inactive_backend", candidates=[], candidates_total=0,
                   why=f"project backend {project_backend} differs from active warehouse {warehouse_backend}")
        return out
    if db and wh_db and str(db).lower() != str(wh_db).lower():
        out.update(status="not_found", candidates=[list(c) for c in cands[:10]], candidates_total=len(cands),
                   why=f"expected in database {db}; the relation list covers database {wh_db} only")
        return out
    if not schemas:
        out.update(status="not_found", candidates=[list(c) for c in cands[:10]], candidates_total=len(cands),
                   why="target schema unknown")
        return out
    hit = [c for c in cands if c[0].lower() in [s.lower() for s in schemas]
           and (not db or not c[3] or str(c[3]).lower() == str(db).lower())]
    if len(hit) == 1:
        out.update(status="resolved", relation=list(hit[0]))
        return out
    if len(hit) > 1:
        out.update(status="ambiguous", candidates=[list(c) for c in hit[:10]], candidates_total=len(hit),
                   why=f"{len(hit)} relations match")
        return out
    out.update(status="not_found", candidates=[list(c) for c in cands[:10]], candidates_total=len(cands),
               why="no relation with this schema and name" + (" (same name in other schemas)" if cands else ""))
    return out


def expected_of(p, x, kind):
    """Expected warehouse identity of a node found by the regex scan (or the manifest's, when present)."""
    if x.get("expected"):
        return dict(x["expected"], schemas=[x["expected"].get("schema")])
    if kind == "sources":
        return {"database": x.get("database"), "schemas": [x["schema"]], "name": x["identifier"],
                "rule": "declared source schema and identifier"}
    target = p.get("schema") or ("main" if (p.get("type") or "duckdb") == "duckdb" else None)
    db = x.get("database_config") or p.get("database")
    name = x.get("alias") or x["name"]
    if kind == "snapshots" and x.get("target_schema"):
        return {"database": db, "schemas": [x["target_schema"]], "name": name, "rule": "snapshot target_schema"}
    custom = x.get("schema_config")
    if "schema" in (p.get("name_macros") or []):
        schemas = [s for s in ([custom, f"{target}_{custom}"] if custom else [target]) if s]
        rule = "the project overrides generate_schema_name (not evaluated); candidate schemas " + ", ".join(schemas)
    elif custom:
        schemas, rule = [f"{target}_{custom}"] if target else [], "dbt default schema rule (<target schema>_<custom schema>)"
    else:
        schemas, rule = [target] if target else [], "target schema"
    return {"database": db, "schemas": schemas, "name": name, "rule": rule + "; regex scan"}


def _apply(x, res):
    x["relation_status"] = res["status"]
    x["relation_expected"] = res["expected"]
    x["relation_match"] = res["how"]
    x["relation"] = res.get("relation")
    if res.get("candidates"):
        x["relation_candidates"] = res["candidates"]
        x["relation_candidates_total"] = res["candidates_total"]
    if res.get("why"):
        x["relation_why"] = res["why"]


# ===================================================================== offline layer

def load_offline(path):
    if not path:
        return None, "not configured"
    if not os.path.isfile(path):
        return None, f"configured file {path} not found"
    try:
        with open(path, encoding="utf-8") as fh:
            w = json.load(fh)
    except Exception as e:  # noqa: BLE001
        return None, f"unreadable ({type(e).__name__}: {str(e)[:120]})"
    if isinstance(w, dict) and w.get("format") == DBVIEW_FORMAT:  # a database profile view (sage_dbview.py)
        return w, "loaded"
    if not isinstance(w, dict) or not isinstance(w.get("tables"), dict):
        return None, "unexpected format (no 'tables' map)"
    return w, "loaded"


def offline_path(cfg):
    """[sage].warehouse_layer resolved against the harness directory ("" = none)."""
    wpath = str(cfg.get("warehouse_layer") or "")
    if wpath and not os.path.isabs(wpath):
        wpath = os.path.join(core.HARNESS, wpath)
    return wpath


def is_dbview(off):
    return isinstance(off, dict) and off.get("format") == DBVIEW_FORMAT


def live_check(wh, offline, live_cols, stop_at=None, per_q=None, max_counts=200):
    """Which offline tables exist in this warehouse with the same column names (case-insensitive), the same type
    families, and (for up to max_counts tables, within the budget) the same row count. Values are not compared, so
    the result is never 'verified': it says what was compared."""
    present, same_cols, same_types, missing, differ, type_differ, ambiguous = 0, 0, 0, [], [], [], []
    counted, same_rows, rows_differ = 0, 0, []
    by_sn = {}
    for (c, s, n), cols in live_cols.items():
        by_sn.setdefault((s, n), []).append((c, cols))
    budget_hit = False
    for key in sorted(offline.get("tables") or {}):
        sch, _, tb = key.rpartition(".")
        hits = by_sn.get((sch.lower(), tb.lower())) or []
        if not hits:
            missing.append(key)
            continue
        if len(hits) > 1:
            ambiguous.append(key)
            continue
        cat, live = hits[0]
        present += 1
        ot = offline["tables"][key]
        oc = {c.lower(): (v or {}).get("t") for c, v in (ot.get("cols") or {}).items()}
        lc = {c.lower(): t for c, t in live}
        if sorted(oc) == sorted(lc):
            same_cols += 1
            if all(_family(oc[c]) == _family(lc[c]) for c in oc):
                same_types += 1
            else:
                type_differ.append(key)
        else:
            differ.append(key)
        if counted < max_counts and not budget_hit and isinstance(ot.get("rows"), int) and not ot.get("sampled"):
            try:
                _, rows, _ = run_query(wh, f"select count(*) from {core.qrel(sch, tb)}", 1, stop_at, per_q)
                counted += 1
                if int(rows[0][0] or 0) == ot["rows"]:
                    same_rows += 1
                else:
                    rows_differ.append(key)
            except TimeoutError:
                budget_hit = True
            except Exception:  # noqa: BLE001
                rows_differ.append(key)
    return {"tables": len(offline.get("tables") or {}), "present": present, "same_column_names": same_cols,
            "same_type_families": same_types, "row_counts_compared": counted, "same_row_counts": same_rows,
            "missing": missing, "columns_differ": differ, "types_differ": type_differ, "rows_differ": rows_differ,
            "ambiguous": ambiguous, "budget_hit": budget_hit, "values_compared": False, "verdict": "unverified",
            "engine": wh.engine, "checked_at": now_utc()}


# ===================================================================== build

def _source_identity(wh):
    """Non-secret identity of the warehouse: engine, location (file path or database name), file size and mtime."""
    out = {"engine": wh.engine, "location": wh.location, "queried_at": now_utc()}
    if wh.engine == "duckdb":
        path = wh.location.split("duckdb file ", 1)[-1]
        try:
            st = os.stat(path)
            out["snapshot"] = {"file_bytes": st.st_size, "file_mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))}
        except OSError:
            pass
    else:
        out["snapshot"] = {"database": wh.prof.get("database"), "schema": wh.prof.get("schema")}
    return out


def build(cfg, deadline=None):
    """-> (layer dict, offline profile). cfg = harness [sage] section. Never raises for a part that fails: the part
    records an error."""
    t0 = time.time()
    budget = float(cfg.get("build_budget_s", 60))
    stop_at = min(deadline or 1e18, t0 + budget)
    per_q = float(cfg.get("query_timeout_s", 20))
    layer = {"format": FORMAT, "built_at": now_utc(), "limits": {
        "profile_max_relations": int(cfg.get("profile_max_relations", 150)),
        "profile_max_columns": int(cfg.get("profile_max_columns", 60)),
        "top_values_max_distinct": int(cfg.get("top_values_max_distinct", 12)),
        "join_max": int(cfg.get("join_max", 60)), "build_budget_s": budget, "query_timeout_s": per_q},
        "projects": [], "packages": [], "warehouse": {}, "profiles": {}, "joins": [], "offline": None, "errors": [],
        "budget": {"scan_incomplete": False, "budget_hit": False}}
    projects = [p for p in core.find_projects() if not OFF_LIMITS.match(p)]
    layer["backend"] = core.active_backend(projects)
    prefer = next((p for p in projects if core.project_backend(p) == layer["backend"]), None)
    redact = Redactor(prefer)
    for proj in projects:
        try:
            man, disc = load_manifest(proj, cfg, stop_at, redact,
                                      parse=not layer["backend"] or core.project_backend(proj) == layer["backend"],
                                      package_projects=projects)
            if man is not None:
                p, pks = project_from_manifest(proj, man)
                p["discovery"] = disc
                layer["packages"] += pks
            else:
                p = scan_project(proj, stop_at=stop_at)
                p["discovery"] = disc
                pks = []
                for pkg in p.get("packages_installed") or []:
                    pr = os.path.join(proj, "dbt_packages", pkg)
                    if os.path.isfile(os.path.join(pr, "dbt_project.yml")):
                        try:
                            pp = scan_project(pr, package=pkg, stop_at=stop_at)
                            layer["packages"].append({"project": proj, "package": pkg, "name": pp.get("name"),
                                                      "models": pp["models"], "sources": pp["sources"], "macros": pp["macros"]})
                        except Exception as e:  # noqa: BLE001
                            layer["errors"].append(f"package {pkg}: {_err(e, 160, redact)}")
            if p.get("scan_incomplete"):
                layer["budget"]["scan_incomplete"] = True
        except Exception as e:  # noqa: BLE001
            p = {"root": proj, "error": _err(e, 200, redact), "models": [], "sources": [],
                 "seeds": [], "snapshots": [], "notes": []}
        layer["projects"].append(p)
    # ---- warehouse
    wh = None
    wpath = offline_path(cfg)
    off, status = load_offline(wpath)
    if not projects and is_dbview(off) and not core.conn_profile():
        # a multi-database environment without a dbt project or a warehouse connection: its databases are described
        # by the offline database profile view; there is no single warehouse to connect to (not an error)
        layer["warehouse"] = {"absent": "no dbt project and no warehouse connection settings; the databases are "
                                        "described by the offline database profile view"}
        rels, live_cols = [], {}
    else:
        try:
            wh = core.Warehouse(prefer)
            layer["warehouse"] = {"engine": wh.engine, "location": wh.location, "source": _source_identity(wh)}
            rels = _relations(wh, stop_at, per_q)
            layer["warehouse"]["relations"] = [list(r) for r in rels]
            live_cols = _columns(wh, stop_at, per_q)
        except Exception as e:  # noqa: BLE001
            layer["warehouse"]["error"] = _err(e, 240, redact)
            rels, live_cols = [], {}
    cats = sorted({r[3] for r in rels})
    multi = len(cats) > 1
    layer["warehouse"]["catalogs"] = cats
    layer["warehouse"]["multi_catalog"] = multi
    # Retain the metadata already fetched, including columns outside the profiling cap.
    # This is a catalog, not a claim that values in every column were measured.
    layer["warehouse"]["columns"] = {
        rel_key(r, multi): [list(c) for c in live_cols.get((r[3].lower(), r[0].lower(), r[1].lower()), [])]
        for r in rels}
    wh_db = (wh.prof.get("database") if wh is not None and wh.engine == "snowflake" else None)
    by_name = {}
    for r in rels:
        by_name.setdefault(r[1].lower(), []).append(r)
    # ---- resolve dbt nodes to relations (own projects, then packages); unresolved nodes get candidates only
    order = []
    for p in layer["projects"]:
        for kind in ("models", "sources", "seeds", "snapshots"):
            for x in p.get(kind) or []:
                _apply(x, resolve(expected_of(p, x, kind), by_name, wh_db,
                                  p.get("type"), layer["warehouse"].get("engine")))
                if x.get("relation"):
                    order.append(x["relation"])
    for pk in layer["packages"]:
        p = next((q for q in layer["projects"] if q["root"] == pk["project"]), {})
        for kind in ("models", "sources"):
            for x in pk[kind]:
                _apply(x, resolve(expected_of(p, x, kind), by_name, wh_db,
                                  p.get("type"), layer["warehouse"].get("engine")))
                if x.get("relation"):
                    order.append(x["relation"])
    order += [list(r) for r in rels]
    seen, queue = set(), []
    for r in order:
        k = rel_key(r, True).lower()
        if k not in seen:
            seen.add(k)
            queue.append(r)
    lim = layer["limits"]
    cap = lim["profile_max_relations"]
    cov = {"relations_total": len(queue), "cap": cap, "profiled": 0, "errors": 0, "budget_hit": False, "timeouts": 0}
    if wh is not None:
        for r in queue[:cap]:
            if time.time() > stop_at:
                cov["budget_hit"] = True
                break
            key = rel_key(r, multi)
            try:
                layer["profiles"][key] = profile_relation(
                    wh, r[0], r[1], live_cols.get((r[3].lower(), r[0].lower(), r[1].lower()), []), lim["profile_max_columns"],
                    lim["top_values_max_distinct"], r[3] if multi else None, stop_at, per_q)
                cov["profiled"] += 1
            except TimeoutError as e:
                layer["profiles"][key] = {"error": _err(e, 200, redact), "timeout": True}
                cov["timeouts"] += 1
                if time.time() > stop_at:
                    cov["budget_hit"] = True
                    break
            except Exception as e:  # noqa: BLE001
                layer["profiles"][key] = {"error": _err(e, 200, redact)}
                cov["errors"] += 1
    cov["not_profiled"] = len(queue) - cov["profiled"] - cov["errors"] - cov["timeouts"]
    layer["profile_coverage"] = cov
    layer["budget"]["budget_hit"] = cov["budget_hit"]
    # ---- declared joins (relationships tests), resolved within their own project, measured on the warehouse
    declared = []
    for p in layer["projects"]:
        by_uid, by_nm = {}, {}
        for kind in ("models", "seeds", "snapshots", "sources"):
            for x in p.get(kind) or []:
                nm = f"{x['source']}.{x['name']}" if kind == "sources" else x["name"]
                if x.get("unique_id"):
                    by_uid[x["unique_id"]] = (nm, x)
                by_nm.setdefault(nm.lower(), (nm, x))
        for pk in layer["packages"]:
            if pk["project"] == p["root"]:
                for x in pk["models"]:
                    if x.get("unique_id"):
                        by_uid[x["unique_id"]] = (f"{pk['package']}.{x['name']}", x)
                    by_nm.setdefault(f"{pk['package']}.{x['name']}".lower(), (f"{pk['package']}.{x['name']}", x))
        for r in p.pop("_rels", []) or []:
            ln, lx = by_uid.get(r["left_uid"], (None, None))
            rn, rx = by_uid.get(r["right_uid"], (None, None)) if r["right_uid"] else (None, None)
            if ln:
                declared.append({"project": p["root"], "project_backend": p.get("type"),
                                 "left": ln, "left_column": r["left_column"],
                                 "right": rn or r["to"], "right_column": r["right_column"], "_l": lx, "_r": rx})
        for kind in ("models", "sources"):
            for m in p.get(kind) or []:
                left = m["name"] if kind == "models" else f"{m['source']}.{m['name']}"
                for c in m.get("columns") or []:
                    for kw in c.pop("_rels", []) or []:
                        to = str(kw.get("to") or "")
                        mm = REF_RE.search(to) or SRC_RE.search(to)
                        if not mm:
                            continue
                        if mm.re is REF_RE:
                            right = mm.group(1) if mm.group(2) is None else f"{mm.group(1)}.{mm.group(2)}"
                            if mm.group(2) is not None and mm.group(1) == p.get("name"):
                                right = mm.group(2)
                        else:
                            right = f"{mm.group(1)}.{mm.group(2)}"
                        declared.append({"project": p["root"], "project_backend": p.get("type"),
                                         "left": left, "left_column": c["name"], "right": right,
                                         "right_column": str(kw.get("field") or kw.get("column_name") or ""),
                                         "_l": m, "_r": by_nm.get(right.lower(), (None, None))[1]})
    def inactive(j):
        return bool(wh is not None and j.get("project_backend") and j["project_backend"].lower() != wh.engine.lower())

    declared.sort(key=inactive)  # Inactive declarations cannot consume the active warehouse's join allowance first.
    joins_cut = 0
    for index, j in enumerate(declared):
        lr, rr = (j["_l"] or {}).get("relation"), (j["_r"] or {}).get("relation")
        if inactive(j):
            j["measured"] = None
            j["why_not"] = f"project backend {j['project_backend']} differs from active warehouse {wh.engine}"
        elif index >= lim["join_max"]:
            j["measured"] = None
            j["why_not"] = "join measurement limit reached"
            joins_cut += 1
        elif wh is None or not lr or not rr or not j["right_column"]:
            j["measured"] = None
            j["why_not"] = ("relation not resolved in the warehouse" if (not lr or not rr) else "no field declared")
        elif time.time() > stop_at:
            j["measured"] = None
            j["why_not"] = "build time budget reached"
        else:
            try:
                # dbt's declared spelling is not necessarily the warehouse's quoted spelling
                # (Snowflake normally stores unquoted names in uppercase). Resolve against
                # the existing live inventory; do not change session identifier semantics.
                lc = resolve_join_column(live_cols, lr, j["left_column"])
                rc = resolve_join_column(live_cols, rr, j["right_column"])
                j["measured_columns"] = {"left": lc, "right": rc}
                j["measured"] = measure_join(wh, lr, lc, rr, rc, multi, stop_at, per_q)
            except Exception as e:  # noqa: BLE001
                j["measured"] = None
                j["why_not"] = _err(e, 160, redact)
                j["error"] = not isinstance(e, TimeoutError)
    for j in declared:
        j.pop("_l", None)
        j.pop("_r", None)
    layer["joins"] = declared
    layer["joins_cut"] = joins_cut
    # ---- offline layer (loaded above)
    layer["offline"] = {"path": str(cfg.get("warehouse_layer") or ""), "status": status}
    if is_dbview(off):
        n = sum(len(d.get("tables") or []) for d in off.get("databases") or [])
        layer["offline"].update(kind="database_view", dataset=off.get("dataset"), tables=n,
                                databases=len(off.get("databases") or []), joins=len(off.get("joins") or []),
                                source_files=(off.get("source") or {}).get("files"))
    elif off is not None:
        layer["offline"]["tables"] = len(off.get("tables") or {})
        layer["offline"]["built"] = (off.get("meta") or {}).get("built")
        if wh is not None and live_cols:
            try:
                layer["offline"]["live_check"] = live_check(wh, off, live_cols, stop_at, per_q,
                                                            int(cfg.get("live_check_max_counts", 200)))
            except Exception as e:  # noqa: BLE001
                layer["offline"]["live_check_error"] = _err(e, 160, redact)
    if wh is not None:
        wh.close()
    layer["build_secs"] = round(time.time() - t0, 1)
    return layer, off


def resolve_join_column(live_cols, relation, declared):
    """Return the actual quoted name only for a unique case-insensitive live match.

    Ambiguous quoted variants and absent columns remain errors, never guessed joins.
    Reuse build's inventory so resolution adds no warehouse queries or time budget.
    """
    key = (str(relation[3] or "").lower(), relation[0].lower(), relation[1].lower())
    matches = [name for name, _ in live_cols.get(key, []) if name.lower() == declared.lower()]
    if len(matches) != 1:
        state = "absent" if not matches else "ambiguous"
        raise ValueError(f"declared join column {declared!r} is {state} in live columns of {relation[0]}.{relation[1]}")
    return matches[0]


def measure_join(wh, lr, lcol, rr, rcol, multi, stop_at=None, per_q=None):
    """Declared join left.lcol -> right.rcol, NULL keys excluded on both sides, keys compared as text: distinct left
    keys found on the right, left rows with a match, rows per key on each side, rows the left join returns."""
    lq, rq = core.qident(lcol), core.qident(rcol)
    L = core.qrel(lr[0], lr[1], lr[3] if multi and len(lr) > 3 else None)
    Rr = core.qrel(rr[0], rr[1], rr[3] if multi and len(rr) > 3 else None)
    sql = (f"with l as (select cast({lq} as varchar) k, count(*) n from {L} where {lq} is not null group by 1), "
           f"r as (select cast({rq} as varchar) k, count(*) n from {Rr} where {rq} is not null group by 1) "
           "select count(*), count(r.k), max(r.n), sum(l.n), sum(case when r.k is not null then l.n else 0 end), "
           "sum(l.n * coalesce(r.n, 1)), max(l.n), "
           f"(select count(*) from {L} where {lq} is null), (select count(*) from r) "
           "from l left join r on l.k = r.k")
    _, rows, _ = run_query(wh, sql, 1, stop_at, per_q)
    tot, hit, fan, lrows, lmatched, joined, lmax, lnull, rkeys = (int(x or 0) for x in rows[0])
    return {"left_keys": tot, "matched": hit, "match_pct": round(100.0 * hit / tot, 2) if tot else None,
            "max_right_rows_per_key": fan, "right_keys": rkeys, "left_rows": lrows, "left_rows_matched": lmatched,
            "left_null_keys": lnull, "max_left_rows_per_key": lmax, "left_join_rows": joined,
            "fanout": round(joined / lrows, 4) if lrows else None, "measured_at": now_utc()}


def profile_named(cfg, layer, rels, deadline=None, task=None):
    """Profile the given relations ([schema, name, kind, catalog], e.g. those of task-named assets) that the start-up
    profile did not cover, within [sage].focus_profile_max_relations and focus_profile_budget_s. Updates the layer in
    place and records what was done in layer['focus_profiling']."""
    t0 = time.time()
    cap = int(cfg.get("focus_profile_max_relations", 8))
    stop_at = min(deadline or 1e18, t0 + float(cfg.get("focus_profile_budget_s", 20)))
    per_q = float(cfg.get("query_timeout_s", 20))
    multi = (layer.get("warehouse") or {}).get("multi_catalog")
    lim = layer.get("limits") or {}
    want = []
    for r in rels:
        k = rel_key(r, multi)
        if k not in (layer.get("profiles") or {}) and k not in [rel_key(w, multi) for w in want]:
            want.append(r)
    rec = {"requested": len(want), "cap": cap, "profiled": [], "failed": [], "not_profiled": [], "budget_hit": False}
    layer["focus_profiling"] = rec
    if not want or (layer.get("warehouse") or {}).get("error"):
        rec["not_profiled"] = [rel_key(r, multi) for r in want]
        return rec
    prefer = next((p["root"] for p in layer.get("projects") or [] if (p.get("type") or "") == layer.get("backend")), None)
    redact = Redactor(prefer)
    wh = None
    try:
        wh = core.Warehouse(prefer)
        for i, r in enumerate(want):
            key = rel_key(r, multi)
            if i >= cap:
                rec["not_profiled"].append(key)
                continue
            if time.time() > stop_at:
                rec["budget_hit"] = True
                rec["not_profiled"].append(key)
                continue
            try:
                sql = ("select column_name, data_type from information_schema.columns where table_schema = "
                       f"'{r[0].replace(chr(39), chr(39) * 2)}' and table_name = '{r[1].replace(chr(39), chr(39) * 2)}'"
                       + (f" and table_catalog = '{r[3].replace(chr(39), chr(39) * 2)}'" if len(r) > 3 and r[3] else "")
                       + " order by ordinal_position")
                _, crow, _ = run_query(wh, sql, 100000, stop_at, per_q)
                cols = [(str(c), str(t)) for c, t in crow]
                exact_cols = []
                if task:
                    from sage_question import column_order
                    import sage_render as render
                    cols = column_order(cols, task)
                    terms = render.task_idents(task)
                    exact_cols = [c for c, _ in cols if render._hit(terms, c) is not None]
                p = profile_relation(wh, r[0], r[1], cols, int(lim.get("profile_max_columns", 60)),
                                     int(lim.get("top_values_max_distinct", 12)), r[3] if multi else None, stop_at, per_q,
                                     exact_columns=exact_cols)
                p["profiled_for"] = "task-named asset"
                if task:
                    p["profiled_for"] = "question focus (named / upstream / explicitly labelled candidates)"
                    p["column_order"] = "literal task identifiers first, then remaining catalog order"
                    if p.get("columns_cut"):
                        p["columns_cut"] += "; " + p["column_order"]
                layer["profiles"][key] = p
                rec["profiled"].append(key)
            except TimeoutError as e:
                layer["profiles"][key] = {"error": _err(e, 200, redact), "timeout": True}
                rec["failed"].append(key)
                rec["budget_hit"] = rec["budget_hit"] or time.time() > stop_at
            except Exception as e:  # noqa: BLE001
                layer["profiles"][key] = {"error": _err(e, 200, redact)}
                rec["failed"].append(key)
    except Exception as e:  # noqa: BLE001
        rec["error"] = _err(e, 200, redact)
        rec["not_profiled"] += [rel_key(r, multi) for r in want if rel_key(r, multi) not in rec["profiled"]]
    finally:
        if wh is not None:
            wh.close()
    rec["secs"] = round(time.time() - t0, 2)
    return rec
