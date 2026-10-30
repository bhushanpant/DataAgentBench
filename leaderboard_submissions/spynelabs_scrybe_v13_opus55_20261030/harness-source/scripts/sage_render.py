"""Sage renderer (untuned core): neutral text for the layer built by sage_build.py and for an optional offline
warehouse profile.

Rules this module keeps (tests: docs/harness-core-untuned/tests/test_sage*.py):
  * description only: it states what was measured, declared or inferred and how; it never advises, ranks, warns or
    flags;
  * task input only through the focus selection: select_focus() picks, by exact name match of the names the task text
    writes as identifiers (plain words do not name assets), the dbt nodes, relations and offline tables the task names
    plus their direct dependencies, one entry per table; every other function is a pure
    function of (layer, offline profile, focus selection, limits). The opt-in sage_question selector additionally
    ranks explicitly labelled discovery candidates and carries measured question-column evidence. Without a focus selection the text does not depend
    on the task;
  * no silent truncation: whenever a list, a text or a level of detail is reduced, the output says so and names the
    `sl` command that shows the rest; the overview never exceeds its size limit; the names of the facts left out of
    the focus section are recorded in the info dict (the start hook writes it to the state directory);
  * deterministic: the same inputs give the same text (sorted orders, no timestamps of the run);
  * a node's warehouse data is shown only for a relation resolved to it; candidates are named as candidates.
"""
import json
import re

PAGE_LINES = 200
DESC_CHARS = {0: 300, 1: 120}
COLDOC_CHARS = 160
TOP_SHOWN = {0: 5}
SEARCH_LIMIT = 80
FOCUS_CHARS = 8000
FOCUS_MAX_ASSETS = 12
MIN_NAME = 3
TOKEN = re.compile(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*")
PATH_TOKEN = re.compile(r"[\w./-]+\.(?:sql|py|csv|ya?ml)\b")


# ===================================================================== small formatting helpers

def n_(x):
    if isinstance(x, bool):
        return str(x).lower()
    if isinstance(x, int):
        return f"{x:,}"
    if isinstance(x, float):
        return f"{x:,.0f}" if x.is_integer() and abs(x) >= 1000 else (str(int(x)) if x.is_integer() else f"{x:.4g}")
    return "?" if x is None else str(x)


def pct(x):
    if x is None:
        return "?"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{s}%"


def cut(text, limit, more_cmd):
    """Text cut to `limit` chars with an explicit notice naming where the rest is."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" [... {len(text) - limit} more chars: {more_cmd}]"


def qv(v, width=40):
    if v is None:
        return "NULL"
    s = json.dumps(str(v), ensure_ascii=False)
    return s if len(s) <= width else s[: width - 4] + '..."'


def page(lines, offset=0, size=PAGE_LINES, cmd="sl"):
    total = len(lines)
    chunk = lines[offset: offset + size]
    out = list(chunk)
    if offset or offset + size < total:
        out.append(f"[showing lines {offset + 1}-{offset + len(chunk)} of {total}; next: {cmd} --offset {offset + size}]"
                   if offset + size < total else f"[showing lines {offset + 1}-{total} of {total}]")
    return out


def multi(layer):
    return bool((layer.get("warehouse") or {}).get("multi_catalog"))


def rkey(layer, r):
    """Display / profile key of a relation [schema, name, kind, catalog]."""
    if not r:
        return None
    return ".".join(([r[3]] if multi(layer) and len(r) > 3 and r[3] else []) + [r[0], r[1]])


def rel_text(r, layer=None):
    return rkey(layer or {}, r) if r else None


# ===================================================================== dbt layer pieces

def profile_of(layer, rel):
    return (layer.get("profiles") or {}).get(rkey(layer, rel)) if rel else None


def bound(c, v):
    return str(v) if c.get("family") in ("numeric", "temporal") else qv(v, 32)


def col_stats(c, exact=None):
    exact = c.get('distinct_exact', exact)
    bits = [f"null {pct(c.get('null_pct'))}"]
    if "distinct" in c:
        bits.append(f"distinct {'' if exact else '~'}{n_(c['distinct'])}")
    if "min" in c:
        bits.append(f"min {bound(c, c['min'])} max {bound(c, c['max'])}")
    return "  ".join(bits)


def top_text(c, k=None):
    top = c.get("top") or []
    if not top:
        return ""
    shown = top if k is None else top[:k]
    more = f" (showing {len(shown)} of {len(top)} recorded values)" if len(shown) < len(top) else ""
    return "values: " + ", ".join(f"{qv(v, 30)} {n_(n)}" for v, n in shown) + more


def column_lines(node, prof, level, more_cmd):
    """Column lines for a model/source: profile columns (warehouse order) merged with YAML-declared columns."""
    decl = {c["name"].lower(): c for c in node.get("columns") or []}
    out = []
    seen = set()
    pcols = (prof or {}).get("columns") or []
    unmeasured = [(n, t) for n, t in (prof or {}).get('catalog_columns', [])
                  if n.lower() not in {c['name'].lower() for c in pcols}]
    if level >= 2:
        names = [f"{c['name']} {c.get('type', '')}".strip() for c in pcols] + \
                [f"{c['name']}{' ' + c['data_type'] if c.get('data_type') else ''} (declared)" for c in node.get("columns") or []
                 if c["name"].lower() not in {p['name'].lower() for p in pcols}]
        names += [f"{n} {t} (not measured)" for n, t in unmeasured if n.lower() not in decl]
        return ["  columns: " + ", ".join(names)] if names else []
    for c in pcols:
        seen.add(c["name"].lower())
        d = decl.get(c["name"].lower()) or {}
        line = f"  - {c['name']} {c.get('type', '')}  {col_stats(c, (prof or {}).get('distinct_exact'))}"
        if level == 0 and c.get("top"):
            line += "  " + top_text(c, TOP_SHOWN[0])
        if d.get("tests"):
            line += "  tests: " + ", ".join(d["tests"])
        if level == 0 and d.get("description"):
            line += "  doc: " + cut(d["description"], COLDOC_CHARS, more_cmd)
        out.append(line)
    for name, d in decl.items():
        if name in seen:
            continue
        catalog = {n.lower() for n, _ in (prof or {}).get('catalog_columns', [])}
        absence = '; not in the warehouse relation' if catalog and name not in catalog else '; statistics not measured'
        line = f"  - {d['name']}{' ' + d['data_type'] if d.get('data_type') else ''}  (declared in YAML{absence if prof else ''})"
        if d.get("tests"):
            line += "  tests: " + ", ".join(d["tests"])
        if level == 0 and d.get("description"):
            line += "  doc: " + cut(d["description"], COLDOC_CHARS, more_cmd)
        out.append(line)
    out += [f"  - {n} {t} (catalog; statistics not measured)" for n, t in unmeasured if n.lower() not in decl]
    if prof and prof.get("columns_cut"):
        out.append(f"  ({prof['columns_cut']}; `sql \"describe ...\"` lists every column)")
    return out


def candidates_text(layer, node, k=3):
    cs = node.get("relation_candidates") or []
    tot = node.get("relation_candidates_total", len(cs))
    if not cs:
        return ""
    shown = ", ".join(f"{rkey(layer, c)} ({c[2]})" for c in cs[:k])
    return f"; relations with the same name (candidates, not attached): {shown}" + (f" and {tot - k} more" if tot > k else "")


def relation_suffix(layer, node):
    st = node.get("relation_status")
    if st == "inactive_backend":
        return f"warehouse relation not linked: {node.get('relation_why', 'inactive project backend')}"
    r = node.get("relation")
    if not r:
        if st == "ambiguous":
            return (f"warehouse relation not resolved: {node.get('relation_why', 'several relations match')} for expected "
                    f"{node.get('relation_expected')}" + candidates_text(layer, node))
        exp = f" (expected {node['relation_expected']})" if node.get("relation_expected") else ""
        return "not found in the warehouse" + exp + candidates_text(layer, node)
    p = profile_of(layer, r)
    s = f"-> {rkey(layer, r)} ({r[2]})"
    if p is None:
        return s + ", not profiled"
    if p.get("error"):
        return s + f", profile failed: {p['error']}"
    return s + f", {n_(p['rows'])} rows, {n_(p.get('columns_total'))} columns"


def model_head(layer, m):
    bits = [m["name"] + (f" v{m['version']}" if m.get("version") else ""), m.get("path") or m.get("yml") or "?"]
    if m.get("materialized"):
        bits.append(f"materialized {m['materialized']}")
    if m.get("alias"):
        bits.append(f"alias {m['alias']}")
    if m.get("unique_key"):
        bits.append(f"unique_key {m['unique_key']}")
    if m.get("versions"):
        bits.append("versions " + ",".join(m["versions"]))
    if m.get("refs"):
        bits.append("refs " + ", ".join(m["refs"]))
    if m.get("sources"):
        bits.append("sources " + ", ".join(m["sources"]))
    return "- " + "  ".join(bits) + "  " + relation_suffix(layer, m)


def _dirs(models):
    d = {}
    for m in models:
        parts = (m.get("path") or "?").split("/")
        k = "/".join(parts[:2]) if len(parts) > 2 else parts[0] if len(parts) > 1 else "(project root)"
        d[k] = d.get(k, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items()))


def discovery_text(p):
    d = p.get("discovery") or {}
    if d.get("method") == "manifest":
        s = f"discovery: dbt manifest ({d.get('manifest')}{', dbt ' + d['dbt_version'] if d.get('dbt_version') else ''})"
    else:
        s = "discovery: " + (d.get("text") or "regex scan of the project files (partial discovery)")
    if p.get("scan_incomplete"):
        s += f"; {p['scan_incomplete']}"
    for nt in d.get("notes") or []:
        s += f"; {nt}"
    if p.get("name_macros"):
        s += "; the project defines " + ", ".join(f"generate_{m}_name" for m in p["name_macros"]) + " (not evaluated)" \
            if d.get("method") != "manifest" else "; the project defines " + ", ".join(f"generate_{m}_name" for m in p["name_macros"])
    return s


def project_block(layer, p, level):
    out = []
    head = f"## dbt project {p['root']}"
    meta = []
    for k in ("name", "profile", "target", "type", "schema"):
        if p.get(k):
            meta.append(f"{k} {p[k]}")
    out.append(head + (f" ({', '.join(meta)})" if meta else ""))
    if p.get("error"):
        out.append(f"(could not be read: {p['error']})")
        return out
    out.append(discovery_text(p))
    if p.get("packages_declared") is not None:
        out.append("packages.yml: " + (", ".join(p["packages_declared"]) or "none") + "; installed in dbt_packages: "
                   + (", ".join(p.get("packages_installed") or []) or "none"))
    for note in p.get("notes") or []:
        out.append(f"note: {note}")
    ms = p["models"]
    out.append(f"### models: {len(ms)}" + (f" ({_dirs(ms)})" if ms else ""))
    if level <= 3:
        for m in ms:
            out.append(model_head(layer, m))
            if level <= 1 and m.get("description"):
                out.append("  description: " + cut(m["description"], DESC_CHARS[level], f"sl model {m['name']}"))
            if level <= 2 and m.get("tests"):
                out.append("  model tests: " + ", ".join(m["tests"]))
            if level <= 2:
                out += column_lines(m, profile_of(layer, m.get("relation")), level, f"sl model {m['name']}")
    elif level == 4:
        out.append("  " + ", ".join(m["name"] for m in ms))
    srcs = p["sources"]
    out.append(f"### sources: {len(srcs)}")
    if level <= 3:
        for s in srcs:
            out.append(f"- {s['source']}.{s['name']}  (schema {s['schema']}, identifier {s['identifier']})  " + relation_suffix(layer, s))
            if level <= 1 and s.get("description"):
                out.append("  description: " + cut(s["description"], DESC_CHARS[level], f"sl source {s['source']}.{s['name']}"))
            if level <= 2:
                out += column_lines(s, profile_of(layer, s.get("relation")), level, f"sl source {s['source']}.{s['name']}")
    elif level == 4:
        out.append("  " + ", ".join(f"{s['source']}.{s['name']}" for s in srcs))
    for kind in ("seeds", "snapshots"):
        xs = p.get(kind) or []
        if xs:
            out.append(f"### {kind}: {len(xs)}")
            if level <= 4:
                out.append("  " + ", ".join(f"{x['name']} ({x['path']}; {relation_suffix(layer, x)})" if level <= 3 else x["name"]
                                            for x in xs))
    out.append(f"macros: {p.get('macros', 0)} files; singular tests: {p.get('singular_tests', 0)} files")
    return out


def packages_block(layer, level):
    pk = layer.get("packages") or []
    if not pk:
        return []
    out = [f"## installed packages: {len(pk)}"]
    for x in pk:
        line = f"- {x['package']} in {x['project']}: {len(x['models'])} models, {len(x['sources'])} sources, {x['macros']} macro files"
        if level <= 2 and x["models"]:
            built = [m for m in x["models"] if m.get("relation")]
            line += f"; {len(built)} of the models found in the warehouse"
        out.append(line)
    out.append("(`sl models <package>` lists package models)")
    return out


def mapped_relations(layer):
    keys = set()
    for _, _, x in all_nodes(layer):
        if x.get("relation"):
            keys.add(rkey(layer, x["relation"]).lower())
    return keys


def warehouse_block(layer, level, offline_keys=frozenset()):
    w = layer.get("warehouse") or {}
    out = []
    if w.get("error"):
        return [f"## warehouse: not reachable at start ({w['error']}); no relation list or profiles"]
    rels = w.get("relations") or []
    schemas = {}
    for r in rels:
        s = rkey(layer, r).rpartition(".")[0]
        e = schemas.setdefault(s, [0, 0])
        e[0 if r[2] == "table" else 1] += 1
    out.append(f"## warehouse: {w.get('engine')} ({w.get('location')}): {len(schemas)} schema{'s' if len(schemas) != 1 else ''}, "
               f"{sum(v[0] for v in schemas.values())} tables, {sum(v[1] for v in schemas.values())} views"
               + (f" in catalogs {', '.join(w.get('catalogs') or [])}" if multi(layer) else ""))
    out.append("schemas: " + ", ".join(f"{s} ({v[0]} tables, {v[1]} views)" for s, v in sorted(schemas.items())))
    cov = layer.get("profile_coverage") or {}
    out.append(f"profiled at start: {n_(cov.get('profiled', 0))} of {n_(cov.get('relations_total', 0))} relations "
               f"(limit {n_(cov.get('cap'))}; order: dbt models, sources, seeds, snapshots, package models, then the "
               f"rest alphabetically)" + (f"; {cov['errors']} profile queries failed" if cov.get("errors") else "")
               + (f"; {cov['timeouts']} stopped by the per-query time limit" if cov.get("timeouts") else "")
               + ("; stopped by the start-up time budget" if cov.get("budget_hit") else ""))
    fp = layer.get("focus_profiling") or {}
    if fp.get("profiled") or fp.get("failed"):
        out.append(f"profiled for the task focus: {len(fp.get('profiled') or [])} relations"
                   + (f"; {len(fp['failed'])} failed" if fp.get("failed") else ""))
    mapped = mapped_relations(layer)
    rest = [r for r in rels if rkey(layer, r).lower() not in mapped
            and (r[0].lower(), r[1].lower()) not in offline_keys]
    label = "relations not matched to a dbt node" + (" or to the offline profile" if offline_keys else "")
    if not rest:
        return out
    out.append(f"### {label}: {len(rest)}")
    if level <= 2:
        for r in rest:
            p = (layer.get("profiles") or {}).get(rkey(layer, r))
            extra = "" if p is None else (f", profile failed: {p['error']}" if p.get("error") else f", {n_(p['rows'])} rows, {n_(p.get('columns_total'))} columns")
            out.append(f"- {rkey(layer, r)} ({r[2]}{extra})")
            if level == 0 and p and not p.get("error"):
                out.append("  columns: " + ", ".join(f"{c['name']} {c.get('type', '')}" for c in p["columns"]))
    elif level == 3:
        by = {}
        for r in rest:
            k = rkey(layer, r)
            by.setdefault(k.rpartition(".")[0], []).append(r[1])
        for s in sorted(by):
            out.append(f"- {s}: " + ", ".join(by[s]))
    else:
        by = {}
        for r in rest:
            s = rkey(layer, r).rpartition(".")[0]
            by[s] = by.get(s, 0) + 1
        out.append("  " + ", ".join(f"{s} {c}" for s, c in sorted(by.items())) + " (`sl relations` lists them)")
    return out


def joins_block(layer, level):
    js = layer.get("joins") or []
    if not js:
        return []
    measured = sum(j.get("measured") is not None for j in js)
    out = [f"## declared relationships tests: {len(js)} (measured: {measured}"
           + (f"; {layer['joins_cut']} exceed the measurement limit" if layer.get("joins_cut") else "") + ")"]
    if level >= 4:
        out.append("  (`sl joins` lists them with measurements)")
        return out
    for j in js:
        out.append("- " + join_text(j))
    return out


def join_text(j):
    s = f"{j['left']}.{j['left_column']} -> {j['right']}.{j['right_column']}: "
    m = j.get("measured")
    if not m:
        return s + f"not measured ({j.get('why_not', '?')})"
    s += (f"{n_(m['matched'])} of {n_(m['left_keys'])} distinct non-null left values found on the right "
          f"({pct(m['match_pct'])}); at most {n_(m['max_right_rows_per_key'])} right rows per value")
    if "left_rows" in m:
        s += (f"; {n_(m['left_rows_matched'])} of {n_(m['left_rows'])} non-null left rows have a match, "
              f"{n_(m['left_null_keys'])} left rows have a NULL key; at most {n_(m['max_left_rows_per_key'])} left rows "
              f"per value; a left join returns {n_(m['left_join_rows'])} rows for the {n_(m['left_rows'])} non-null left "
              f"rows (fanout {n_(m['fanout'])})")
    return s


DBT_LEVELS = {
    0: "full detail (descriptions up to 300 chars, column statistics, up to 5 frequent values, column docs)",
    1: "reduced to fit the size limit: descriptions up to 120 chars, column statistics without frequent values or column docs",
    2: "reduced to fit the size limit: columns as name and type only, no descriptions",
    3: "reduced to fit the size limit: one line per model, source and relation, no columns",
    4: "reduced to fit the size limit: model and source names only",
    5: "reduced to fit the size limit: counts only",
}

DBT_LEGEND = ("How it was built: per project, models, sources, seeds and snapshots come from a dbt manifest when one "
              "matches the project files (target/manifest.json, or a manifest `dbt parse` writes into the harness state "
              "directory), else from a regex scan of the project files, which is partial discovery (Jinja, macros and vars "
              "are not evaluated); each project's discovery line says which. Labels: declared = from the project files or "
              "the manifest; measured = read-only warehouse queries at the start of this run; inferred = derived by a "
              "rule (relation matching). refs/sources = the node's ref()/source() dependencies, with package and version "
              "when given; materialization, alias and unique_key as configured. A node is matched to a warehouse relation "
              "by its expected catalog, schema and name (manifest: database, schema and alias; regex scan: declared "
              "source schema and identifier, models by dbt's default schema rule); when no relation or more than one "
              "relation has that identity the node has no relation, and relations with the same name elsewhere are "
              "listed as candidates only. Profiles: full-table queries: row count; per column NULL %, distinct count "
              "(exact on DuckDB; '~' marks approx_count_distinct, approximate), min/max (text: lexicographic order; text "
              "values quoted); frequent values for text/boolean columns with at most {top} distinct values. Declared "
              "relationships tests, NULL keys excluded and keys compared as text: distinct left values found on the "
              "right, left rows with a match, rows per value on each side, and the rows a left join returns (fanout = "
              "those rows / non-null left rows).")


def dbt_section(layer, level, offline_keys=frozenset()):
    out = []
    projects = layer.get("projects") or []
    if not projects:
        out.append("## dbt projects: none found under /app, /workspace or the working directory")
    for p in projects:
        if level >= 5:
            out.append(f"## dbt project {p['root']}: {len(p.get('models', []))} models, {len(p.get('sources', []))} sources, "
                       f"{len(p.get('seeds', []))} seeds, {len(p.get('snapshots', []))} snapshots (`sl models`, `sl sources`); "
                       + discovery_text(p))
        else:
            out += project_block(layer, p, level)
    out += packages_block(layer, level)
    out += warehouse_block(layer, min(level, 4), offline_keys)
    out += joins_block(layer, level)
    for e in layer.get("errors") or []:
        out.append(f"note: {e}")
    return out


# ===================================================================== offline warehouse profile

OFF_LEVELS = {
    0: "full detail (every column with type, NULL %, approximate distinct, min/max)",
    1: "reduced to fit the size limit: every table with its column names",
    2: "reduced to fit the size limit: one line per table (rows, columns, keys, description)",
    3: "reduced to fit the size limit: table names and row counts per schema",
    4: "reduced to fit the size limit: tables and rows per schema",
}


def live_check_text(lc):
    s = (f"Live check at start ({lc['engine']}): {lc['present']} of {lc['tables']} tables exist here, "
         f"{lc['same_column_names']} with the same column names (case-insensitive)")
    if "same_type_families" in lc:
        s += (f", {lc['same_type_families']} of those with the same type families; row counts compared for "
              f"{lc.get('row_counts_compared', 0)} tables (tables profiled on a sample are not compared), equal for "
              f"{lc.get('same_row_counts', 0)}" + ("; stopped by the time budget" if lc.get("budget_hit") else "")
              + (f"; {len(lc['ambiguous'])} tables exist in several catalogs" if lc.get("ambiguous") else ""))
    s += "; values are not compared, so the match of the profile to this data is unverified."
    return s


def off_legend(off, layer):
    meta = off.get("meta") or {}
    o = layer.get("offline") or {}
    s = (f"Offline warehouse profile `{o.get('path')}`: built {meta.get('built', '?')} on the host before any task by "
         f"semantic/build_sf_layer.py; the builder's note on the data: \"{meta.get('database', '?')}\"; reference dbt project: "
         f"{meta.get('reference_project', '?')}. Per table: rows; key = "
         "columns verified unique and non-null; other unique = further columns verified unique and non-null; declared key = "
         "columns with a unique test on a reference model that reads the table; description and source names from the "
         "reference project. Per column: type in the profiled database, % NULL, distinct count, min/max (text: "
         "lexicographic), most frequent values of low-cardinality columns, column doc. Joins: column pairs taken from the "
         "reference project's SQL joins (sql) and from shared column names (inferred), measured as % of distinct left values "
         "found on the right and the largest number of right rows for one value. As stored by the builder: descriptions "
         "up to 160 characters, at most 3 source names, 4 directly-selecting models and 12 lineage tables per entry, up to 5 "
         "frequent values per column with values up to 40 characters, missing values in those lists written as NULL; tables "
         "above 2,000,000 rows profiled on a sample; distinct counts exact for columns with frequent values, approximate "
         "otherwise; shared-name pairs with under 20% of left values found on the right not kept.")
    lc = o.get("live_check")
    if lc:
        s += " " + live_check_text(lc)
    elif o.get("live_check_error"):
        s += f" Live check at start failed: {o['live_check_error']}."
    return s


def off_table_line(key, t):
    bits = [f"{n_(t.get('rows'))} rows", f"{len(t.get('cols') or {})} columns"]
    if t.get("pk"):
        bits.append("key " + "+".join(t["pk"]))
    if t.get("alt_keys"):
        bits.append("other unique " + ", ".join(t["alt_keys"]))
    if t.get("declared_key") and t.get("declared_key") != t.get("pk"):
        dk = t["declared_key"]
        bits.append("declared key " + ("+".join(dk) if isinstance(dk, list) else str(dk)))
    return f"- {key}: " + ", ".join(bits)


def off_col(name, c, full=False):
    s = f"{name} {c.get('t', '?')} null {pct(c.get('null'))} distinct ~{n_(c.get('nd'))}"
    if "lo" in c:
        s += f" min {qv(c['lo'], 28)} max {qv(c['hi'], 28)}"
    if full:
        if c.get("top"):
            s += "  values: " + ", ".join(f"{'NULL' if v == 'NULL' else qv(v, 44)} {n_(k)}" for v, k in c["top"])
        if c.get("doc"):
            s += "  doc: " + " ".join(str(c["doc"]).split())
    return s


def offline_section(off, layer, level):
    tabs = off.get("tables") or {}
    by = {}
    for key in sorted(tabs):
        by.setdefault(key.rpartition(".")[0], []).append(key)
    joins = off.get("joins") or []
    out = [f"## offline warehouse profile: {len(tabs)} tables in {len(by)} schemas, {len(joins)} measured join pairs "
           f"(`sl tables`, `sl table <SCHEMA.TABLE>`, `sl joins <SCHEMA.TABLE>`, `sl search <term>`)"]
    for sch in sorted(by):
        keys = by[sch]
        rows = sum(int(tabs[k].get("rows") or 0) for k in keys)
        if level >= 4:
            out.append(f"- {sch}: {len(keys)} tables, {n_(rows)} rows")
            continue
        if level == 3:
            out.append(f"### {sch} ({len(keys)} tables): " + ", ".join(f"{k.rpartition('.')[2]} {n_(tabs[k].get('rows'))}" for k in keys))
            continue
        out.append(f"### {sch} ({len(keys)} tables, {n_(rows)} rows)")
        for k in keys:
            t = tabs[k]
            out.append(off_table_line(k, t))
            if level <= 2 and t.get("desc"):
                out.append("  description: " + cut(t["desc"], 120, f"sl table {k}"))
            if level == 1:
                out.append("  columns: " + ", ".join(t.get("cols") or {}))
            elif level == 0:
                out.append("  columns: " + "; ".join(off_col(n, c) for n, c in (t.get("cols") or {}).items()))
    return out


# ===================================================================== focus: assets named in the task text

# Code spans and quoted names of the task text: a fenced block, an inline `code` span, or a single word in quotes
QUOTED = re.compile(r"```.*?```|`[^`\n]+`|(?<![\w'])'[\w.$-]+'(?![\w'])|\"[\w.$-]+\"", re.S)


def task_idents(text):
    """{lower-cased term: {"pos", "ident", "whole", "cases"}} of the task text: every identifier with every dotted
    suffix (a.b.c -> a.b.c, b.c, c) and the stems of file paths. "ident": at least one occurrence is written as an
    identifier (it contains '_', '.' or a digit, stands in a code span or quotes, or is a file-path stem); "whole": it
    occurs as a whole identifier (not only as a dotted suffix); "cases": the spellings as written."""
    text = text or ""
    spans = [m.span() for m in QUOTED.finditer(text)]
    terms = {}

    def put(term, pos, ident, whole):
        if len(term) < MIN_NAME:
            return
        t = terms.setdefault(term.lower(), {"pos": pos, "ident": False, "whole": False, "cases": set()})
        t["pos"] = min(t["pos"], pos)
        t["ident"] = t["ident"] or ident
        t["whole"] = t["whole"] or whole
        t["cases"].add(term)
    for m in PATH_TOKEN.finditer(text):
        put(m.group(0).rsplit("/", 1)[-1].rsplit(".", 1)[0], m.start(), True, True)
    for m in TOKEN.finditer(text):
        tok = m.group(0)
        quoted = any(a <= m.start() and m.end() <= b for a, b in spans)
        ident = quoted or bool(re.search(r"[_.\d]", tok))
        parts = tok.split(".")
        for i in range(len(parts)):
            put(".".join(parts[i:]), m.start(), ident, i == 0)
    return terms


def task_terms(text, whole_only=False):
    """Lower-cased terms of the task text with their first position (all of task_idents; whole_only: whole
    identifiers only). Matching uses task_idents, which also records how each term is written."""
    return {t: v["pos"] for t, v in task_idents(text).items() if v["whole"] or not whole_only}


def _case_specific(name):
    """A name whose letter case carries information: all upper-case or mixed case beyond a capital first letter."""
    return any(c.isupper() for c in name) and not (name[:1].isupper() and name[1:] == name[1:].lower())


def _hit(T, name, whole=False):
    """First position at which the task text names `name`, or None. The text names an asset only where it writes the
    name as an identifier, or writes it in the asset's own upper / mixed case; a plain word of the same spelling
    (lower-case or capitalised) does not name it. whole: the name must occur as a whole identifier."""
    t = T.get(str(name).lower())
    if not t or (whole and not t["whole"]):
        return None
    if t["ident"] or (_case_specific(str(name)) and str(name) in t["cases"]):
        return t["pos"]
    return None


def _specific(term):
    return bool(re.search(r"[_.\d]", term))


def _node_names(kind, x):
    """The names a node can be written as in a task (original case): node name, name, alias, source identifier and
    schema.identifier, schema.name of its resolved relation, file stem."""
    names = {node_name(kind, x), str(x["name"])}
    if x.get("alias"):
        names.add(str(x["alias"]))
    if kind == "sources":
        names |= {str(x.get("identifier") or ""), f"{x.get('schema')}.{x.get('identifier')}"}
    if x.get("relation"):
        names.add(f"{x['relation'][0]}.{x['relation'][1]}")
    if x.get("path"):
        names.add(x["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return {n for n in names if len(n) >= MIN_NAME}


def project_type(layer, root):
    for p in layer.get("projects") or []:
        if root == p["root"] or root.startswith(p["root"] + "/dbt_packages/"):
            return (p.get("type") or "").lower()
    return ""


def _phys_keys(it):
    """Keys of the physical table an item describes: its resolved relation, a source's declared schema.identifier, a
    relation's or offline table's schema.name (case-insensitive)."""
    keys = set()
    if it["type"] == "node":
        x = it["node"]
        r = x.get("relation")
        if r:
            keys.add(("tbl", str(r[0]).lower(), str(r[1]).lower()))
        if it["kind"] == "sources" and x.get("schema"):
            keys.add(("tbl", str(x["schema"]).lower(), str(x.get("identifier") or x["name"]).lower()))
    elif it["type"] == "relation":
        keys.add(("tbl", str(it["rel"][0]).lower(), str(it["rel"][1]).lower()))
    else:
        s, _, t = it["name"].rpartition(".")
        keys.add(("tbl", s.lower(), t.lower()))
    return keys


def _prio(it):
    if it["type"] == "node":
        return 3 if it.get("other_backend") else 0
    return 1 if it["type"] == "relation" else 2


def _alt_text(layer, it):
    if it["type"] == "node":
        x = f"{it['kind'][:-1]} {it['name']} (project {it['root']}"
        return x + (f", type {project_type(layer, it['root']) or '?'})" if it.get("other_backend") else ")")
    return f"{'relation' if it['type'] == 'relation' else 'offline table'} {it['name']}"


def fold(layer, items):
    """Items describing the same table folded into one entry: the same physical table (resolved relation, declared
    source table, unmatched relation, offline table: schema and name, case-insensitive), or a node of a project of
    another backend with the same kind and name as another node. The entry kept is, in order, a node of an
    active-backend project, a relation, an offline table, a node of another backend; it lists the others in "also"."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)
    owner = {}
    for i, it in enumerate(items):
        for k in _phys_keys(it):
            if k in owner:
                union(i, owner[k])
            else:
                owner[k] = i
    by_name = {}
    for i, it in enumerate(items):
        if it["type"] == "node":
            by_name.setdefault((it["kind"], it["name"].lower(), str(it["node"].get("version") or "")), []).append(i)
    for idx in by_name.values():
        act = [i for i in idx if not items[i].get("other_backend")]
        for i in idx:
            if items[i].get("other_backend"):
                union(i, act[0] if act else idx[0])
    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in sorted(groups.values(), key=min):
        rep_i = min(members, key=lambda i: (_prio(items[i]), i))
        rep = dict(items[rep_i])
        named = [items[i] for i in members if items[i]["why"] == "named"]
        if named and rep["why"] != "named":
            first = min(named, key=lambda it: it["pos"])
            rep.update(why="named", term=first["term"], spec=first["spec"], pos=first["pos"])
        rep["also"] = list(rep.get("also") or []) + [x for i in members if i != rep_i
                                                     for x in [_alt_text(layer, items[i]), *(items[i].get("also") or [])]]
        out.append(rep)
    return out


def _tier(it):
    """Order of the focus entries: named assets of the active backend (and relations / offline tables), upstream
    dependencies of those, named nodes of another backend, direct children, dependencies of other-backend nodes."""
    if it["why"] == "named":
        return 2 if it.get("other_backend") else 0
    if it.get("other_backend"):
        return 4
    return 1 if it["why"].startswith("upstream") else 3


def select_focus(layer, off, text, max_assets=FOCUS_MAX_ASSETS):
    """-> focus dict: the dbt nodes, unmatched warehouse relations and offline tables that the task text names, then
    the direct dependencies of the named nodes (refs/sources, then direct children). A name counts as named only where
    the text writes it as an identifier (with '_', '.' or a digit, in a code span or quotes, as a file-path stem) or
    in the asset's own upper / mixed case (_hit); an asset by a qualified name (schema.name, source.table) or by a
    bare name written as a whole identifier (the last part of a.b names a node only as the qualified name a.b). Entries for the same table are folded into one (fold). Order (_tier): named assets
    of the active backend first (more specific names, then by first position in the text), their upstream
    dependencies, named nodes of another backend, direct children, the rest. Capped at max_assets; the rest is
    recorded as omitted."""
    T = task_idents(text)
    backend = (layer.get("backend") or "").lower()
    named, seen = [], set()

    def add(item, hits):
        if item["id"] in seen:
            return
        seen.add(item["id"])
        pos, term = min(hits)
        item.update(term=term.lower(), why="named", spec=_specific(term), pos=pos)
        named.append(item)
    for root, kind, x in all_nodes(layer):
        hits = [(p, n) for n in _node_names(kind, x) for p in [_hit(T, n, whole="." not in n)] if p is not None]
        if hits:
            add({"type": "node", "kind": kind, "root": root, "name": node_name(kind, x), "node": x,
                 "id": ("node", root, kind, node_name(kind, x), x.get("version")),
                 "other_backend": bool(backend) and project_type(layer, root) not in ("", backend)}, hits)
    mapped = mapped_relations(layer)
    for r in (layer.get("warehouse") or {}).get("relations") or []:
        k = rkey(layer, r)
        if k.lower() in mapped:
            continue
        hits = [(p, n) for n in (k, r[1]) for p in [_hit(T, n, whole="." not in n)] if p is not None]
        if hits:
            add({"type": "relation", "name": k, "rel": r, "id": ("rel", k.lower())}, hits)
    for key in sorted((off or {}).get("tables") or {}):
        hits = [(p, n) for n in (key, key.rpartition(".")[2]) for p in [_hit(T, n, whole="." not in n)] if p is not None]
        if hits:
            add({"type": "offline", "name": key, "id": ("off", key.lower())}, hits)
    named.sort(key=lambda it: (bool(it.get("other_backend")), not it["spec"], it["pos"], it["name"]))
    named = fold(layer, named)
    deps = []
    for it in named:
        if it["type"] != "node":
            continue
        for d in direct_deps(layer, it["root"], it["kind"], it["node"]):
            d["why"] = f"{d.pop('rel_kind')} of {it['name']}"
            d["other_backend"] = it.get("other_backend")
            if d["id"] not in seen:
                seen.add(d["id"])
                deps.append(d)
    deps.sort(key=lambda d: (not d["why"].startswith("upstream"), bool(d.get("other_backend"))))
    allit = sorted(fold(layer, named + deps), key=_tier)
    items = allit[:max_assets]
    omitted = [[it["name"], "asset limit"] for it in allit[max_assets:]]
    return {"items": items, "named": [it["name"] for it in allit if it["why"] == "named"],
            "dependencies": [d["name"] for d in allit if d["why"] != "named"],
            "folded": {it["name"]: it["also"] for it in allit if it.get("also")},
            "omitted": omitted, "max_assets": max_assets}


def direct_deps(layer, root, kind, x):
    """Upstream refs/sources of a node (resolved within its project and packages) and its direct children."""
    out = []
    nodes = [(r, k, n) for r, k, n in all_nodes(layer) if r == root or r.startswith(root + "/dbt_packages/")]
    for rid in x.get("ref_ids") or []:
        for r, k, n in nodes:
            if k != "sources" and n["name"].lower() == str(rid.get("name")).lower() and \
                    (not rid.get("version") or str(n.get("version")) == str(rid["version"])) and \
                    (not rid.get("package") or r.endswith("/dbt_packages/" + rid["package"]) or n.get("package") == rid["package"]):
                out.append({"type": "node", "kind": k, "root": r, "name": node_name(k, n), "node": n, "rel_kind": "upstream",
                            "id": ("node", r, k, node_name(k, n), n.get("version"))})
                break
    for s in x.get("sources") or []:
        for r, k, n in nodes:
            if k == "sources" and node_name(k, n).lower() == s.lower():
                out.append({"type": "node", "kind": k, "root": r, "name": s, "node": n, "rel_kind": "upstream",
                            "id": ("node", r, k, s, None)})
                break
    me = node_name(kind, x).lower()
    for r, k, n in nodes:
        if k == "sources":
            continue
        if any(str(rid.get("name")).lower() == me for rid in n.get("ref_ids") or []) or \
                me in [s.lower() for s in n.get("sources") or []]:
            out.append({"type": "node", "kind": k, "root": r, "name": node_name(k, n), "node": n, "rel_kind": "downstream",
                        "id": ("node", r, k, node_name(k, n), n.get("version"))})
    return out


def focus_relations(layer, focus):
    """Relations of focus items without a start-up profile, in focus order (for sage_build.profile_named)."""
    out = []
    for it in (focus or {}).get("items") or []:
        r = (it.get("node") or {}).get("relation") if it["type"] == "node" else it.get("rel")
        if r and profile_of(layer, r) is None and not it.get("other_backend"):
            out.append(r)
    return out


def _offline_for(off, node):
    if not off or not node.get("identifier"):
        return None
    want = f"{node.get('schema')}.{node.get('identifier')}".lower()
    hits = [k for k in off.get("tables") or {} if k.lower() == want]
    return hits[0] if len(hits) == 1 else None


def _focus_cols(layer, x, prof, level, cmd):
    decl = {c["name"].lower(): c for c in x.get("columns") or []}
    pcols = (prof or {}).get("columns") or []
    unmeasured = [(n, t) for n, t in (prof or {}).get("catalog_columns", [])
                  if n.lower() not in {c['name'].lower() for c in pcols}]
    if level >= 2:
        names = [f"{c['name']} {c.get('type', '')}".strip() for c in pcols] + \
                [f"{d['name']}{' ' + d['data_type'] if d.get('data_type') else ''} (declared)" for n, d in decl.items()
                 if n not in {c['name'].lower() for c in pcols}]
        names += [f"{n} {t} (not measured)" for n, t in unmeasured if n.lower() not in decl]
        return [names] if names else []
    out = []
    seen = set()
    for c in pcols:
        seen.add(c["name"].lower())
        d = decl.get(c["name"].lower()) or {}
        line = f"  - {c['name']} {c.get('type', '')}  {col_stats(c, prof.get('distinct_exact'))}"
        if level == 0 and c.get("top"):
            line += "  " + top_text(c, TOP_SHOWN[0])
        if d.get("data_type"):
            line += f"  declared type: {d['data_type']}"
        if d.get("tests"):
            line += "  tests: " + ", ".join(d["tests"])
        if d.get("description"):
            line += "  doc: " + cut(d["description"], COLDOC_CHARS if level == 0 else 80, cmd)
        out.append(line)
    for nm, d in decl.items():
        if nm in seen:
            continue
        catalog = {n.lower() for n, _ in (prof or {}).get('catalog_columns', [])}
        absence = '; not in the warehouse relation' if catalog and nm not in catalog else '; statistics not measured'
        line = f"  - {d['name']}{' ' + d['data_type'] if d.get('data_type') else ''}  (declared{absence if prof else ''})"
        if d.get("tests"):
            line += "  tests: " + ", ".join(d["tests"])
        if d.get("description"):
            line += "  doc: " + cut(d["description"], COLDOC_CHARS if level == 0 else 80, cmd)
        out.append(line)
    out += [f"  - {n} {t} (catalog; statistics not measured)" for n, t in unmeasured if n.lower() not in decl]
    return out


def focus_block(layer, off, it, level, col_limit=None):
    """Lines for one focus item at a detail level (0 full ... 3 one line). col_limit cuts the column list (stated).
    Entries folded into it (select_focus: the same table, or the same node in a project of another backend) are
    named on one line after the heading."""
    out = _focus_block(layer, off, it, level, col_limit)
    also = it.get("also") or []
    if also:
        shown = also[:6]
        out.insert(1, "  same table / node also as: " + "; ".join(shown)
                   + (f" (and {len(also) - 6} more; `sl search {it['name'].rpartition('.')[2]}`)" if len(also) > 6 else ""))
    return out


def _focus_block(layer, off, it, level, col_limit=None):
    why = "named in the task" if it["why"] == "named" else it["why"]
    if it["type"] == "offline":
        key, t = it["name"], off["tables"][it["name"]]
        out = [f"### offline table {key}  [{why}; offline profile]", "  " + off_table_line(key, t)[2:]]
        if level >= 3:
            return out
        if t.get("desc"):
            out.append("  description: " + cut(t["desc"], 300 if level == 0 else 120, f"sl table {key}"))
        cols = list((t.get("cols") or {}).items())
        cl = cols if col_limit is None else cols[:col_limit]
        if level == 2:
            out.append("  columns: " + ", ".join(f"{n} {c.get('t', '?')}" for n, c in cl))
        else:
            out += ["  - " + off_col(n, c, full=level == 0) for n, c in cl]
        if len(cl) < len(cols):
            out.append(f"  [showing {len(cl)} of {len(cols)} columns; `sl table {key}`]")
        js = [j for j in off.get("joins") or [] if key in (j["l"], j["r"])]
        if js and level == 0:
            out.append(f"  measured join pairs [offline]: {len(js)}" + (f" (showing 8; `sl joins {key}`)" if len(js) > 8 else ""))
            out += ["  - " + off_join_text(j) for j in js[:8]]
        return out
    if it["type"] == "relation":
        r = it["rel"]
        prof = profile_of(layer, r)
        out = [f"### relation {it['name']} ({r[2]})  [{why}; not matched to a dbt node]"]
        out.append("  " + ("not profiled" if prof is None else f"profile failed: {prof['error']}" if prof.get("error") else
                           f"profile [measured]: {n_(prof['rows'])} rows, {n_(prof.get('columns_total'))} columns"))
        if prof and not prof.get("error") and level < 3:
            cols = _focus_cols(layer, {}, prof, level, f"sl table {it['name']}")
            out += _limit_cols(cols, level, col_limit, f"sl table {it['name']}")
        return out
    x, kind = it["node"], it["kind"]
    cmd = f"sl {'source' if kind == 'sources' else 'model'} {it['name']}"
    label = kind[:-1]
    head = f"### {label} {it['name']}{' v' + x['version'] if x.get('version') else ''}  [{why}; project {it['root']}"
    if it.get("other_backend"):
        head += f"; project type {project_type(layer, it['root']) or '?'}, active backend {layer.get('backend')}"
    out = [head + "]"]
    disc = next((p.get("discovery") or {} for p in layer.get("projects") or [] if p["root"] == it["root"]), {})
    src = "manifest" if disc.get("method") == "manifest" else "regex scan"
    bits = []
    for k in ("path", "yml", "materialized", "alias", "schema_config", "unique_key", "schema", "identifier"):
        if x.get(k) and (k != "yml" or kind == "sources"):
            bits.append(f"{k} {x[k]}")
    if x.get("refs"):
        bits.append("refs " + ", ".join(x["refs"]))
    if x.get("sources"):
        bits.append("sources " + ", ".join(x["sources"]))
    if level >= 3:
        return [out[0], "  " + "; ".join(bits[:3]) + f"  {relation_suffix(layer, x)}"]
    out.append(f"  declared ({src}): " + "; ".join(bits))
    if x.get("description"):
        out.append(f"  description (declared): " + cut(x["description"], DESC_CHARS[min(level, 1)], cmd))
    if x.get("tests"):
        out.append("  tests (declared): " + ", ".join(x["tests"]))
    r = x.get("relation")
    prof = profile_of(layer, r)
    if r:
        line = f"  warehouse relation (inferred: {x.get('relation_match') or 'name'}): {rkey(layer, r)} ({r[2]})"
        if prof is None:
            line += "; not profiled"
        elif prof.get("error"):
            line += f"; profile failed: {prof['error']}"
        else:
            line += f"; profile (measured at start, {prof.get('method', 'full scan')}): {n_(prof['rows'])} rows, {n_(prof.get('columns_total'))} columns"
        out.append(line)
    else:
        out.append("  warehouse relation: " + relation_suffix(layer, x))
    cols = _focus_cols(layer, x, prof if prof and not prof.get("error") else None, level, cmd)
    if cols:
        if level < 2:
            out.append("  columns (type and statistics measured; declared type, tests and docs declared):")
        out += _limit_cols(cols, level, col_limit, cmd)
    off_key = _offline_for(off, x) if kind == "sources" else None
    if off_key:
        t = off["tables"][off_key]
        out.append(f"  offline profile entry with the same schema and table name ({off_key}; built before the run on the "
                   f"profiled database): " + off_table_line(off_key, t)[2 + len(off_key) + 2:])
        oc = list((t.get("cols") or {}).items())
        oc_l = oc if col_limit is None else oc[:col_limit]
        if level == 2:
            out.append("  offline columns: " + ", ".join(f"{n} {c.get('t', '?')}" for n, c in oc_l))
        else:
            out += ["  - " + off_col(n, c, full=level == 0) for n, c in oc_l]
        if len(oc_l) < len(oc):
            out.append(f"  [showing {len(oc_l)} of {len(oc)} offline columns; `sl table {off_key}`]")
    js = [j for j in layer.get("joins") or [] if it["name"].lower() in (j["left"].lower(), j["right"].lower())
          and j.get("project") == it["root"]]
    if js and level <= 1:
        out.append("  declared relationships tests (declared; measured):")
        out += ["  - " + join_text(j) for j in js]
    return out


def _limit_cols(cols, level, col_limit, cmd):
    if level >= 2:
        names = cols[0] if cols else []
        shown = names if col_limit is None else names[:col_limit]
        out = ["  columns: " + ", ".join(shown)] if shown else []
        if len(shown) < len(names):
            out.append(f"  [showing {len(shown)} of {len(names)} columns; `{cmd}`]")
        return out
    shown = cols if col_limit is None else cols[:col_limit]
    out = list(shown)
    if len(shown) < len(cols):
        out.append(f"  [showing {len(shown)} of {len(cols)} columns; `{cmd}`]")
    return out


def focus_section(layer, off, focus, budget):
    """-> (lines, info). Named assets first at the most detail that fits `budget` chars; lower-priority items are
    reduced first (direct dependencies down to one line, named assets down to column names and types), then column
    lists are cut, then items are left out; every reduction is stated and recorded in info."""
    items = list((focus or {}).get("items") or [])
    info = {"named": list((focus or {}).get("named") or []), "dependencies": list((focus or {}).get("dependencies") or []),
            "shown": [], "omitted": [list(x) for x in (focus or {}).get("omitted") or []], "columns_cut": [], "budget": budget,
            "folded": dict((focus or {}).get("folded") or {})}
    if not items:
        return [], info
    n_named = sum(1 for it in items if it["why"] == "named")
    head = [f"## Assets named in the task text: {len(info['named'])} named, {len(info['dependencies'])} direct dependencies "
            "(selected by name match, case-insensitive, of the names the task text writes as identifiers (with '_', '.' "
            "or a digit, in backticks or quotes, file-path stems) or in an asset's own upper / mixed case, against dbt "
            "node, warehouse relation and offline table names; one entry per table; dependencies = refs/sources and "
            "direct children of named dbt nodes)"]
    if focus.get("question_driven"):
        head = [f"## Question focus: {len(info['named'])} named assets, {len(info['dependencies'])} upstream assets "
                f"(depth at most {focus['upstream_depth']}); {len(focus.get('candidates', []))} column-match candidates. "
                "Named assets are literal identifier matches. Candidates are discovery hypotheses, not source requirements. "
                "Unrelated downstream consumers are excluded; `sl lineage <name>` lists consumers."]
    maxlv = [3 if it.get("other_backend") or it["why"] != "named" else 2 for it in items]
    lv = [0] * len(items)
    climit = [None] * len(items)
    cache = {}

    def block(i):
        k = (i, lv[i], climit[i])
        if k not in cache:
            cache[k] = focus_block(layer, off, items[i], lv[i], climit[i])
        return cache[k]

    def text(keep):
        lines = list(head)
        dropped = len(items) - keep + len(info["omitted"])
        for i in range(keep):
            lines += block(i)
        if dropped:
            lines.append(f"[{dropped} further matching assets not shown in this section (asset or size limit); "
                         "`sl search <name>` finds them]")
        return lines

    keep = len(items)
    while len("\n".join(text(keep))) > budget:
        i = next((i for i in reversed(range(keep)) if lv[i] < maxlv[i]), None)
        if i is not None:
            lv[i] += 1
            continue
        j = next((j for j in reversed(range(keep)) if items[j]["why"] != "named"), None)
        if j is not None and keep > n_named:
            keep -= 1
            info["omitted"].append([items[keep]["name"], "size limit"])
            continue
        c = next((c for c in reversed(range(keep)) if (climit[c] is None or climit[c] > 5)), None)
        if c is not None:
            full = len(block(c))
            climit[c] = max(5, (climit[c] or max(full, 6)) // 2)
            continue
        if keep > 1:
            keep -= 1
            info["omitted"].append([items[keep]["name"], "size limit"])
            continue
        break
    lines = text(keep)
    if len("\n".join(lines)) > budget:
        lines = [lines[0][: max(0, budget - 120)], f"[section cut to fit {budget} chars; `sl model <name>` and "
                                                   "`sl table <name>` show the named assets]"]
    for i in range(keep):
        info["shown"].append([items[i]["name"], lv[i]])
        if climit[i] is not None:
            info["columns_cut"].append([items[i]["name"], climit[i]])
    return lines, info


# ===================================================================== overview

def _fits(lines, limit):
    return len("\n".join(lines)) <= limit


def overview(layer, off=None, limit=20000, dbt_share=0.4, focus=None, focus_chars=FOCUS_CHARS):
    """-> (text, info). Deterministic; len(text) <= limit; any reduction is stated in the text. With a focus
    selection (select_focus) the assets named in the task come first, within min(focus_chars, limit / 2).
    An offline database profile view (sage_dbview) without a dbt project is rendered by sage_dbview.overview."""
    if is_dbview(off):
        return dbview_overview(layer, off, limit)
    head = ["# SAGE: description of this environment's dbt projects and warehouse",
            "Built by the harness at the start of this run from the dbt project files and read-only warehouse queries"
            + ("; the offline warehouse profile below was built before any task" if off else "")
            + ". The catalog below does not depend on the task" + ("; the first section lists the assets whose names occur "
                                                                     "in the task text" if focus and focus.get("items") else "")
            + ". Drill down with `sl help` (models, sources, relations, columns, joins, lineage, search, live values)."]
    legend = DBT_LEGEND.format(top=(layer.get("limits") or {}).get("top_values_max_distinct", "?"))
    offline_keys = frozenset(tuple(k.lower().rpartition(".")[::2]) for k in (off or {}).get("tables", {}))
    info = {"dbt_level": None, "offline_level": None, "cut": False}
    flines, finfo = focus_section(layer, off, focus, int(min(focus_chars, limit // 2))) if focus else ([], {})
    qtext = ""
    if focus and focus.get("evidence"):
        from sage_question import render_evidence
        qbudget = min(4000, limit // 4)
        qtext = render_evidence(focus['evidence'], qbudget)
        flines, finfo = focus_section(layer, off, focus, max(500, int(min(focus_chars, limit // 2)) - len(qtext)))
    if finfo:
        finfo["named_detail_unavailable"] = named_detail_unavailable(layer, focus)
    info["focus"] = finfo or None
    base = head + (["", qtext] if qtext else []) + [legend] + ([""] + flines if flines else [])
    rest = max(0, limit - len("\n".join(base)))
    budget_dbt = len("\n".join(base)) + int(rest * (dbt_share if off else 1.0))
    dbt_lines, dbt_level = None, None
    for lv in range(6):
        cand = [f"Detail (dbt part): {DBT_LEVELS[lv]}."] + dbt_section(layer, lv, offline_keys)
        if _fits(base + cand, budget_dbt) or lv == 5:
            dbt_lines, dbt_level = cand, lv
            break
    info["dbt_level"] = dbt_level
    text_lines = base + [""] + dbt_lines
    if off:
        ol = [off_legend(off, layer)]
        chosen = None
        for lv in range(5):
            cand = [""] + [f"Detail (offline part): {OFF_LEVELS[lv]}."] + ol + offline_section(off, layer, lv)
            if _fits(text_lines + cand, limit) or lv == 4:
                chosen, info["offline_level"] = cand, lv
                break
        text_lines = text_lines + chosen
    text = "\n".join(text_lines)
    if len(text) > limit:
        note = "\n[overview cut at {} of {} chars to fit the size limit; `sl overview --full` prints it uncut]"
        keep = max(0, limit - len(note) - 24)
        text = text[:keep] + note.format(keep, len(text))
        info["cut"] = True
    info["truncated"] = bool(info["cut"] or (dbt_level or 0) > 0 or (info["offline_level"] or 0) > 0
                             or (finfo and (finfo.get("omitted") or finfo.get("columns_cut")
                                            or any(lv for _, lv in finfo.get("shown") or []))))
    info["chars"] = len(text)
    info["limit"] = limit
    return text, info


def is_dbview(off):
    return isinstance(off, dict) and off.get("format") == "scrybe-sage-view/1"


def dbview_overview(layer, off, limit):
    """Overview with an offline database profile view: the view's own overview (sage_dbview, the same text for every
    task of the dataset), after the dbt part when the environment also has dbt projects."""
    import sage_dbview as V
    info = {"dbt_level": None, "offline_level": None, "cut": False, "focus": None, "offline_kind": "database_view"}
    pre = []
    if layer.get("projects"):
        pre, pinfo = overview(layer, None, limit // 2)
        info.update(dbt_level=pinfo.get("dbt_level"), focus=pinfo.get("focus"))
        pre = [pre, ""]
    text = "\n".join(pre + [V.overview(off, max(0, limit - len("\n".join(pre)) - 1))])
    info["cut"] = "[overview cut at " in text
    info["truncated"] = info["cut"] or "Detail: reduced" in text
    info["chars"] = len(text)
    info["limit"] = limit
    return text, info


def overview_full(layer, off=None):
    lines = [f"Detail (dbt part): {DBT_LEVELS[0]}."] + dbt_section(layer, 0, frozenset(
        tuple(k.lower().rpartition(".")[::2]) for k in (off or {}).get("tables", {})))
    if off:
        lines += ["", off_legend(off, layer)] + offline_section(off, layer, 0)
    return "\n".join(lines)


# ===================================================================== status

def status_of(layer, off, info):
    """-> ('ok' | 'degraded', [reasons]). ok only when every part was built and every fact the layer reports as
    collected is available: no warehouse error, no failed or stopped query, no missing configured offline profile, and
    detail available for every named asset that resolved to a relation."""
    if is_dbview(off) and not layer.get("projects"):
        import sage_dbview as V
        state, reasons = V.status_of(off)
        if (info or {}).get("cut"):
            reasons = reasons + ["overview_cut"]
        return ("degraded" if reasons else "ok"), reasons
    reasons = []
    w = layer.get("warehouse") or {}
    o = layer.get("offline") or {}
    cov = layer.get("profile_coverage") or {}
    if w.get("error"):
        reasons.append("warehouse_unreachable")
    if o.get("path") and o.get("status") != "loaded":
        reasons.append("offline_layer_missing")
    if o.get("live_check_error"):
        reasons.append("offline_live_check_failed")
    if any(p.get("error") for p in layer.get("projects") or []):
        reasons.append("project_unreadable")
    if (layer.get("budget") or {}).get("scan_incomplete"):
        reasons.append("project_scan_incomplete")
    if cov.get("errors") or cov.get("timeouts"):
        reasons.append("profile_queries_failed")
    if cov.get("budget_hit") or (layer.get("focus_profiling") or {}).get("budget_hit"):
        reasons.append("time_budget_reached")
    if any(j.get("error") for j in layer.get("joins") or []):
        reasons.append("join_queries_failed")
    if ((info or {}).get("focus") or {}).get("named_detail_unavailable"):
        reasons.append("named_asset_detail_unavailable")
    if (info or {}).get("cut"):
        reasons.append("overview_cut")
    return ("degraded" if reasons else "ok"), reasons


def named_detail_unavailable(layer, focus):
    """Named assets whose warehouse facts could not be delivered: relation ambiguous, or resolved but its profile
    failed or was not taken (a named asset with no relation of that identity is not counted: it may not exist yet)."""
    out = []
    for it in (focus or {}).get("items") or []:
        if it["why"] != "named" or it.get("other_backend"):
            continue
        if it["type"] == "node":
            x = it["node"]
            if x.get("relation_status") == "ambiguous":
                out.append(it["name"])
            elif x.get("relation"):
                p = profile_of(layer, x["relation"])
                if p is None or p.get("error"):
                    out.append(it["name"])
        elif it["type"] == "relation":
            p = profile_of(layer, it["rel"])
            if p is None or p.get("error"):
                out.append(it["name"])
    return out


def status_line(layer, off, info, enabled=True, error=None):
    if not enabled:
        return "SAGE status=disabled (harness.toml [sage].enabled = false): no semantic layer in this run"
    if error:
        return f"SAGE status=failed: the semantic layer could not be built ({error}); `sl` has nothing to show"
    if is_dbview(off) and not layer.get("projects"):
        import sage_dbview as V
        line = V.status_line(off)
        if (info or {}).get("cut"):
            line = line.replace("SAGE status=ok", "SAGE status=degraded", 1) + \
                (",overview_cut" if " degraded_by=" in line else " degraded_by=overview_cut")
        return line
    state, reasons = status_of(layer, off, info)
    ps = layer.get("projects") or []
    cov = layer.get("profile_coverage") or {}
    w = layer.get("warehouse") or {}
    o = layer.get("offline") or {}
    parts = ["SAGE status=" + state]
    parts.append(f"dbt_layer=files(projects={len(ps)} models={sum(len(p.get('models', [])) for p in ps)} "
                 f"sources={sum(len(p.get('sources', [])) for p in ps)})")
    meth = [(p.get("discovery") or {}).get("method") for p in ps if not p.get("error")]
    parts.append(f"discovery=manifest:{meth.count('manifest')},scan:{meth.count('regex scan')}")
    parts.append("warehouse=" + (f"error({w['error'][:120]})" if w.get("error") else
                                 f"{w.get('engine')}(relations={len(w.get('relations') or [])} "
                                 f"profiled={cov.get('profiled', 0)}/{cov.get('relations_total', 0)} cap={cov.get('cap')}"
                                 f"{' budget_hit' if cov.get('budget_hit') else ''})"))
    if o.get("status") == "loaded":
        lc = o.get("live_check") or {}
        parts.append(f"offline_layer=loaded({o.get('path')} tables={o.get('tables')}"
                     + (f" live_present={lc.get('present')}/{lc.get('tables')} same_columns={lc.get('same_column_names')}"
                        f" same_rows={lc.get('same_row_counts', '?')}/{lc.get('row_counts_compared', '?')} values=unverified" if lc else "")
                     + ")")
    elif o.get("path"):
        parts.append(f"offline_layer=MISSING({o.get('path')}: {o.get('status')})")
    else:
        parts.append("offline_layer=none")
    fi = (info or {}).get("focus") or {}
    if fi:
        parts.append(f"focus=named:{len(fi.get('named') or [])},shown:{len(fi.get('shown') or [])},"
                     f"omitted:{len(fi.get('omitted') or [])}")
    parts.append(f"overview_chars={info.get('chars')}/{info.get('limit')}")
    parts.append(f"detail_level=dbt:{info.get('dbt_level')}" + (f",offline:{info.get('offline_level')}" if off else ""))
    parts.append(f"truncated={'yes' if info.get('truncated') else 'no'}")
    if reasons:
        parts.append("degraded_by=" + ",".join(reasons))
    return " ".join(parts)


# ===================================================================== drill-down (sl)

def all_nodes(layer):
    for p in layer.get("projects") or []:
        for kind in ("models", "sources", "seeds", "snapshots"):
            for x in p.get(kind) or []:
                yield p["root"], kind, x
    for pk in layer.get("packages") or []:
        for m in pk["models"]:
            yield f"{pk['project']}/dbt_packages/{pk['package']}", "models", m
        for s in pk["sources"]:
            yield f"{pk['project']}/dbt_packages/{pk['package']}", "sources", s


def node_name(kind, x):
    return f"{x['source']}.{x['name']}" if kind == "sources" else x["name"]


def children_of(layer, name):
    out = []
    nl = name.lower()
    for root, kind, x in all_nodes(layer):
        refs = [str(r.get("name")).lower() for r in x.get("ref_ids") or []] or [r.lower() for r in x.get("refs") or []]
        if nl in refs or nl in [s.lower() for s in x.get("sources") or []]:
            out.append(f"{x['name']} ({root})")
    return out


def node_detail(layer, root, kind, x):
    name = node_name(kind, x)
    out = [f"{kind[:-1]} {name}  (project {root})"]
    for k in ("unique_id", "path", "yml", "language", "materialized", "alias", "schema_config", "unique_key", "schema",
              "identifier", "database", "version"):
        if x.get(k):
            out.append(f"  {k}: {x[k]}")
    if x.get("versions"):
        out.append("  versions: " + ", ".join(x["versions"]))
    if x.get("description"):
        out.append("  description: " + " ".join(x["description"].split()))
    if x.get("refs"):
        out.append("  refs: " + ", ".join(x["refs"]))
    if x.get("sources"):
        out.append("  sources: " + ", ".join(x["sources"]))
    ch = children_of(layer, name)
    out.append("  used by: " + (", ".join(ch) if ch else "no file in the scanned projects"))
    if x.get("tests"):
        out.append("  tests: " + ", ".join(x["tests"]))
    r = x.get("relation")
    if r:
        out.append(f"  warehouse relation: {rkey(layer, r)} ({r[2]}; inferred: {x.get('relation_match') or 'name'})")
    else:
        out.append("  warehouse relation: " + relation_suffix(layer, x))
        for c in x.get("relation_candidates") or []:
            out.append(f"  - candidate (not attached): {rkey(layer, c)} ({c[2]}); `sl table {rkey(layer, c)}` describes it")
    prof = profile_of(layer, r)
    if prof and prof.get("error"):
        out.append(f"  profile failed: {prof['error']}")
    elif prof:
        out.append(f"  rows: {n_(prof['rows'])}; columns: {n_(prof.get('columns_total'))} (measured at start, "
                   f"{prof.get('method', 'full scan')})")
    elif r:
        out.append("  not profiled at start (profile limit); `sl values <relation> <column>` queries live")
    decl = {c["name"].lower(): c for c in x.get("columns") or []}
    pcols = (prof or {}).get("columns") or []
    if pcols or decl:
        out.append("  columns:")
    seen = set()
    for c in pcols:
        seen.add(c["name"].lower())
        d = decl.get(c["name"].lower()) or {}
        line = f"  - {c['name']} {c.get('type', '')}  {col_stats(c, (prof or {}).get('distinct_exact'))}"
        if c.get("top"):
            line += "  " + top_text(c)
        if d.get("tests"):
            line += "  tests: " + ", ".join(d["tests"])
        if d.get("data_type"):
            line += f"  declared type: {d['data_type']}"
        if d.get("description"):
            line += "  doc: " + " ".join(d["description"].split())
        out.append(line)
    for nm, d in decl.items():
        if nm not in seen:
            out.append(f"  - {d['name']}  (declared in YAML{'; not in the warehouse relation' if pcols else ''})"
                       + (f"  declared type: {d['data_type']}" if d.get("data_type") else "")
                       + (f"  tests: {', '.join(d['tests'])}" if d.get("tests") else "")
                       + (f"  doc: {' '.join(d['description'].split())}" if d.get("description") else ""))
    if prof and prof.get("columns_cut"):
        out.append(f"  ({prof['columns_cut']})")
    js = [j for j in layer.get("joins") or [] if name.lower() in (j["left"].lower(), j["right"].lower())]
    if js:
        out.append("  declared relationships:")
        out += ["  - " + join_text(j) for j in js]
    return out


def off_table_detail(off, key):
    t = off["tables"][key]
    out = [off_table_line(key, t)[2:]]
    if t.get("desc"):
        out.append("  description: " + " ".join(str(t["desc"]).split()))
    if t.get("source"):
        out.append("  reference-project source names: " + ", ".join(t["source"]))
    if t.get("staging"):
        out.append("  reference-project models that select it directly: " + ", ".join(t["staging"]))
    if "used_by" in t:
        out.append(f"  reference-project models reading it (count): {t['used_by']}")
    out.append("  columns:")
    out += ["  - " + off_col(n, c, full=True) for n, c in (t.get("cols") or {}).items()]
    js = [j for j in off.get("joins") or [] if key in (j["l"], j["r"])]
    if js:
        out.append(f"  measured join pairs: {len(js)}")
        out += ["  - " + off_join_text(j) for j in js]
    lin = sorted(m for m, ts in (off.get("lineage") or {}).items() if key in ts)
    if lin:
        out.append(f"  reference-project models whose lineage reaches it: {len(lin)}: " + ", ".join(lin))
    return out


def off_join_text(j):
    s = (f"{j['l']}.{j['lc']} -> {j['r']}.{j['rc']} ({j.get('src')}): {pct(j.get('match'))} of {n_(j.get('keys'))} "
         f"distinct left values found on the right; at most {n_(j.get('fan'))} right rows per value")
    if "lrows" in j:
        s += (f"; {n_(j.get('lmatched'))} of {n_(j['lrows'])} non-null left rows have a match, at most "
              f"{n_(j.get('lfan'))} left rows per value, a left join returns {n_(j.get('jrows'))} rows")
    return s
