#!/usr/bin/env python3
"""Sage database profile view: a neutral, dataset-level rendering of a precomputed offline profile of a set of logical
databases (one profile directory per dataset, built offline by semantic/build_layer.py from the same data the agent
can query). Ported unchanged in content from harness/dab-untuned-sage/scripts/sage.py (docs/harness-dab-untuned-sage/
SAGE_PROVENANCE.md); in the core it is an OFFLINE LAYER INPUT of the Sage layer: [sage].warehouse_layer may name a
view file of this format, sage_build.load_offline() loads it, sage_render dispatches the overview and the status to
the functions below, and `sl` dispatches tables / table / column / joins / search here.

Two halves, both standard-library only:

  extract  (host side, run by the launcher or bundle before any trial starts)
      sage_dbview.py extract <layer_dir> <out.json>
      Reads profile.json, semantic_layer.json and validation.json of ONE dataset and writes a view file that keeps
      only measured content: databases, tables, row counts, per-column profile statistics, and the measured join
      candidates (key columns, the code-defined key normalisation, measured match counts). Everything an LLM wrote
      as prose (model descriptions, grain, primary-key claims, element definitions, join descriptions and
      cardinality claims, notes, warnings, audit text) is left out; a builder-written key expression is kept only
      where a measured join candidate uses it, because the measurement is defined by it.

  render   (in the trial; reads only the view file, never the task)
      overview | tables | table <name> [--offset N] | column <table>.<col> | joins [<table>] | search <term>
      The overview is a function of the view file and the size limit only, so every task of a dataset gets the same
      text. Nothing is ever dropped without saying so: every list that is cut states "showing N of M" and the command
      that shows the rest. The overview starts with a status line: ok, or degraded with the reasons (tables not
      profiled, partial coverage of a table group, a layer without a recorded measurement method). Labels: measured
      (row counts, column statistics, join match counts computed by code) and inferred (candidate keys from the
      profiled rows, join candidates proposed by the builder's LLM and then measured); nothing is declared by a
      project here.
"""
import hashlib
import json
import os
import re
import sys

VIEW_FORMAT = "scrybe-sage-view/1"
DEFAULT_FILE = "/sage/layer.json"
OVERVIEW_CHARS = 20000          # default size limit of the start-up overview (env SCRYBE_SAGE_OVERVIEW_CHARS)
PROFILE_ROWS = 5000             # build_layer.py: profile rows per table (load(db, tb, 5000))
VALUE_ROWS = 2000               # build_layer.py: top values / samples / patterns use the first 2000 non-null values
JOIN_ROWS = 50000               # build_layer.py: SL_MAX_ROWS default, rows per table used to measure joins
PAGE_COLUMNS = 80               # `sl table` columns per page
SEARCH_LIMIT = 60

COLUMN_KEYS = ("dtype", "null_pct", "distinct", "samples", "values", "range", "id_prefix", "stringified_dict",
               "free_text")
JOIN_MEASURES = ("left_keys", "right_keys", "left_rows", "left_null_keys", "right_rows", "right_null_keys",
                 "left_rows_matched", "max_left_rows_per_key", "max_right_rows_per_key", "left_join_rows", "fanout")
GROUP_KEYS = ("profiled", "layout_known", "layouts", "selection")
EXIT_NO_VIEW = 3
NORMALIZE_TEXT = {
    "none": "compared as trimmed text",
    "digits": "the first run of digits in the value",
    "strip_prefix": "trimmed, then leading letters/'#' and the separators after them removed",
    "lower_strip": "trimmed, lower-cased, '#' removed",
}
PLAIN_COLUMN = re.compile(r"""df\[\s*['"]([^'"]+)['"]\s*\]""")


# ===================================================================================== extract (host side)

def _sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _resolve_side(ref, models, profile):
    """'<model>.<name>' -> {db, table, column} for a profiled column, {db, table, expr, source_columns} for a
    builder-written expression, or {ref, unresolved} when the layer does not say what it is."""
    if not isinstance(ref, str) or "." not in ref:
        return {"ref": str(ref), "unresolved": True}
    mname, name = ref.split(".", 1)
    m = models.get(mname)
    if not m:
        return {"ref": ref, "unresolved": True}
    db, table = m.get("db"), m.get("table")
    cols = (((profile.get(db) or {}).get("tables") or {}).get(table) or {}).get("columns") or {}
    side = {"db": db, "table": table}
    if name in cols:                      # the builder reads a raw column before it looks for an element
        side["column"] = name
        return side
    for e in m.get("elements") or []:
        if e.get("name") == name and isinstance(e.get("expr"), str):
            expr = e["expr"].strip()
            plain = PLAIN_COLUMN.fullmatch(expr)
            if plain:
                side["column"] = plain.group(1)
            else:
                side["expr"] = expr
                side["source_columns"] = sorted(set(PLAIN_COLUMN.findall(expr)))
            return side
    side.update(name=name, unresolved=True)
    return side


def extract(layer_dir):
    """-> the view dict for one dataset's layer directory (raises FileNotFoundError without profile.json)."""
    prof_p = os.path.join(layer_dir, "profile.json")
    profile = _load_json(prof_p)
    if not isinstance(profile, dict):
        raise FileNotFoundError(f"no readable profile.json in {layer_dir}")
    layer = _load_json(os.path.join(layer_dir, "semantic_layer.json")) or {}
    validation = _load_json(os.path.join(layer_dir, "validation.json")) or {}

    dbs = []
    for db in sorted(profile):
        p = profile[db] or {}
        tables = []
        for tb in sorted((p.get("tables") or {})):
            t = p["tables"][tb] or {}
            entry = {"name": tb}
            if "error" in t:
                entry["error"] = str(t["error"])
            if "rows" in t:
                entry["rows"] = t["rows"]
            if isinstance(t.get("sample"), dict):
                entry["sample"] = {k: t["sample"].get(k) for k in ("method", "rows", "of")}
            cols = []
            for cname, c in (t.get("columns") or {}).items():   # keep the table's own column order
                cols.append({"name": cname, **{k: c[k] for k in COLUMN_KEYS if k in c}})
            if "columns" in t:
                entry["columns"] = cols
            tables.append(entry)
        d = {"name": db, "engine": p.get("engine"), "tables": tables}
        g = p.get("table_group")
        if isinstance(g, dict):
            d["table_group"] = {"count": g.get("count"), "listed": list(g.get("examples") or []),
                                "profiled": [t["name"] for t in tables]}
            d["table_group"].update({k: g[k] for k in GROUP_KEYS if k in g and k != "profiled"})
        dbs.append(d)

    models = {m.get("name"): m for m in layer.get("models") or [] if isinstance(m, dict)}
    vj = validation.get("joins") or {}
    joins = []
    # SCRYBE_SAGE_LLM_JOINS=0: leave out the join candidates the builder's LLM proposed (measured or not), so the view
    # holds only code-measured profile content.
    llm_joins = os.environ.get("SCRYBE_SAGE_LLM_JOINS", "1") != "0"
    for j in (layer.get("joins") or []) if llm_joins else []:
        if not isinstance(j, dict):
            continue
        v = j.get("validation") or {}
        key = f"{j.get('left')} = {j.get('right')}"
        full = vj.get(key) or {}
        rec = {"left": _resolve_side(j.get("left"), models, profile), "right": _resolve_side(j.get("right"), models, profile),
               "normalize": j.get("normalize") or "none"}
        for k in ("matched", "left_match_pct", "right_match_pct"):
            if k in v:
                rec[k] = v[k]
        for k in JOIN_MEASURES:
            if k in full:
                rec[k] = full[k]
            elif k in v:
                rec[k] = v[k]
        joins.append(rec)
    unmeasured = []
    for s in ((layer.get("built") or {}).get("dropped") or []) if llm_joins else []:
        m = re.match(r"join (.+?)=(.+?): (.*)$", str(s), re.S)
        if not m:
            continue
        status = "zero_match" if m.group(3).startswith("weak join") else "not_measured"
        unmeasured.append({"left": _resolve_side(m.group(1).strip(), models, profile),
                           "right": _resolve_side(m.group(2).strip(), models, profile), "status": status})
    built = layer.get("built") or {}
    meta = _load_json(os.path.join(layer_dir, "profile_meta.json")) or built.get("profile_meta")
    prov = None
    if isinstance(meta, dict):
        prov = {k: meta.get(k) for k in ("format", "profiled_at", "profile_secs", "profile_rows", "value_rows", "join_rows",
                                         "max_tables", "sampling", "join_measure")}
        prov["databases"] = {db: {k: v for k, v in (x or {}).items() if k in ("engine", "file", "file_bytes", "file_mtime",
                                                                                 "tables_total", "tables_profiled")}
                             for db, x in (meta.get("databases") or {}).items()}
    return {"format": VIEW_FORMAT, "dataset": layer.get("dataset") or os.path.basename(os.path.normpath(layer_dir)),
            "source": {"builder": "semantic/build_layer.py", "join_proposer_model": built.get("model"),
                       "builder_lessons": bool((built.get("builder_lessons") or {}).get("used")),
                       "llm_join_candidates": llm_joins,
                       "files": {f: _sha(os.path.join(layer_dir, f))
                                 for f in ("profile.json", "semantic_layer.json", "validation.json", "profile_meta.json")}},
            "provenance": prov, "databases": dbs, "joins": joins, "joins_unmeasured": unmeasured}


# ===================================================================================== formatting helpers

def _num(x):
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, (int, float)):
        if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
            return str(int(x))
        if isinstance(x, float):
            return f"{x:.6g}"
        return str(x)
    return str(x)


def _int(x):
    return f"{x:,}" if isinstance(x, int) else _num(x) if x is not None else "?"


def _q(s, n=None):
    s = str(s)
    if n and len(s) > n:
        s = s[: n - 3] + "..."
    return json.dumps(s, ensure_ascii=False)


def _pct(x):
    return f"{_num(x)}%" if isinstance(x, (int, float)) else "?"


def sample_rows(t):
    smp = t.get("sample") or {}
    if isinstance(smp.get("rows"), int):
        return smp["rows"]
    r = t.get("rows")
    return min(r, PROFILE_ROWS) if isinstance(r, int) else None


def status_of(view):
    """-> ('ok' | 'degraded', [reasons]) of a view: degraded when a table was not profiled, a table group is only
    partly profiled, the layer has no recorded measurement method (format 1 builder), or no join file was given."""
    reasons = []
    if any(t.get("error") for _, t in _all_tables(view)):
        reasons.append("tables_not_profiled")
    for d in view.get("databases") or []:
        g = d.get("table_group")
        if g and isinstance(g.get("count"), int) and g["count"] > len(g.get("profiled") or []):
            reasons.append("table_group_partly_profiled")
            break
    if not view.get("provenance"):
        reasons.append("measurement_method_unrecorded")
    if not ((view.get("source") or {}).get("files") or {}).get("semantic_layer.json"):
        reasons.append("no_join_file")
    return ("degraded" if reasons else "ok"), reasons


def status_line(view):
    state, reasons = status_of(view)
    ntab = sum(len(d.get("tables") or []) for d in view.get("databases") or [])
    return (f"SAGE status={state} dataset={view.get('dataset')} databases={len(view.get('databases') or [])} "
            f"profiled_tables={ntab} joins={len(view.get('joins') or [])}" + (f" degraded_by={','.join(reasons)}" if reasons else ""))


def candidate_keys(t):
    """Columns that are non-null and unique within the profiled rows (measured, not claimed)."""
    n = sample_rows(t)
    if not n:
        return []
    return [c["name"] for c in t.get("columns") or []
            if c.get("null_pct") == 0 and isinstance(c.get("distinct"), int) and c["distinct"] == n]


def _side(s, with_expr=False):
    if s.get("unresolved"):
        where = ".".join(x for x in (s.get("db"), s.get("table"), s.get("name")) if x) or s.get("ref", "?")
        return f"{where} (not described in the layer)"
    base = f"{s['db']}.{s['table']}"
    if "column" in s:
        return f"{base}.{s['column']}"
    src = ",".join(s.get("source_columns") or []) or "?"
    if with_expr:
        return f"{base}.<expression over {src}: {s['expr']}>"
    return f"{base}.{{{src}}}~"


def _join_line(j, with_expr=False):
    norm = j.get("normalize", "none")
    out = f"{_side(j['left'], with_expr)} = {_side(j['right'], with_expr)}  [key: {norm}]"
    if "matched" in j:
        lk, rk = j.get("left_keys"), j.get("right_keys")
        lpart = f"{_int(j['matched'])}/{_int(lk)} " if lk is not None else ""
        rpart = f"{_int(j['matched'])}/{_int(rk)} " if rk is not None else ""
        out += (f"  matched {_int(j['matched'])} distinct keys; left {lpart}({_pct(j.get('left_match_pct'))}), "
                f"right {rpart}({_pct(j.get('right_match_pct'))})")
        if "left_rows" in j:
            out += (f"; left rows with a match {_int(j.get('left_rows_matched'))}/{_int(j['left_rows'])}, NULL keys left "
                    f"{_int(j.get('left_null_keys'))} right {_int(j.get('right_null_keys'))}; at most "
                    f"{_int(j.get('max_left_rows_per_key'))} left and {_int(j.get('max_right_rows_per_key'))} right rows per "
                    f"key; left join rows {_int(j.get('left_join_rows'))} (fanout {_num(j.get('fanout'))})")
    else:
        out += "  (no measurement recorded)"
    return out


def _involves(j, table):
    return any(_matches_table(j[s], table) for s in ("left", "right"))


def _matches_table(side, table):
    if side.get("unresolved") and not side.get("table"):
        return False
    full = f"{side.get('db')}.{side.get('table')}"
    return table.lower() in (str(side.get("table")).lower(), full.lower())


def _flags(c):
    out = []
    if c.get("id_prefix"):
        pre = ", ".join(f"{_q(k)} {_num(v)}" for k, v in c["id_prefix"].items())
        out.append(f"letters+digits pattern, prefixes: {pre}")
    if c.get("stringified_dict"):
        out.append("most values start with '{'")
    if c.get("free_text"):
        out.append("long text (mean length > 60 chars)")
    return out


# ===================================================================================== column rendering

def column_line(c, level):
    """One overview line per column. level 0 = most detail ... 3 = name and type only."""
    name, dtype = c["name"], c.get("dtype", "?")
    if level >= 3:
        return f"{name}:{dtype}"
    parts = [f"{name}  {dtype}", f"null {_pct(c.get('null_pct'))}", f"distinct {_int(c.get('distinct'))}"]
    if "range" in c and isinstance(c["range"], list) and len(c["range"]) == 2:
        parts.append(f"min {_num(c['range'][0])} max {_num(c['range'][1])}")
    vals = c.get("values") or {}
    if vals and level <= 1:
        k = 10 if level == 0 else 5
        items = list(vals.items())
        shown = ", ".join(f"{_q(v, 40)} {_num(n)}" for v, n in items[:k])
        more = f" (showing {k} of {len(items)} listed values)" if len(items) > k else ""
        d = c.get("distinct")
        if isinstance(d, int) and len(items[:k]) < d and not more:
            more = f" ({len(items)} of {d} distinct values occur in the first {VALUE_ROWS:,} non-null values)"
        parts.append(f"values: {shown}{more}")
    elif level == 0 and c.get("samples"):
        parts.append("e.g. " + ", ".join(_q(s, 40) for s in c["samples"][:2]))
    elif level == 1 and c.get("samples") and "range" not in c:
        parts.append("e.g. " + _q(c["samples"][0], 30))
    if level <= 1:
        parts += _flags(c)
    return "  ".join(parts)


LEVEL_NOTE = {
    0: "full overview detail (top values up to 10, two sample values per column)",
    1: "reduced to fit the size limit: top values up to 5, at most one sample value, no samples for numeric columns",
    2: "reduced to fit the size limit: no top values, samples or value patterns (sl table <name> shows them)",
    3: "reduced to fit the size limit: columns listed as name:type only (sl table <name> shows their statistics)",
    4: "reduced to fit the size limit: tables with row and column counts only (sl table <name> shows the columns)",
}


# ===================================================================================== overview

def _legend(view, used_norms, any_expr):
    pv = view.get("provenance")
    labels = ("Labels: measured = row counts, column statistics and join match counts computed by code; inferred = "
              "candidate keys (from the profiled rows only) and join candidates (pairs proposed by the builder's LLM, "
              "then measured).")
    if pv:
        lines = [
            f"How it was measured (recorded by the builder, profiled {pv.get('profiled_at') or '?'}): row counts are full "
            "counts (MongoDB: estimated document count). Column statistics per table over the rows named in its line "
            f"(all rows up to {_int(pv.get('profile_rows'))}, else the sample method stated there): null % and distinct "
            f"count (values compared as text); top values, samples and patterns over the first {_int(pv.get('value_rows'))} "
            "non-null of those values; min/max for numeric columns. MongoDB `_id` fields are not profiled.",
            "Join candidates: measured by code on up to "
            f"{_int(pv.get('join_rows'))} rows of each table: NULL keys removed before the key normalisation, keys "
            "compared as text after it; matched = distinct keys found on both sides; side % = matched / that side's "
            "distinct keys; rows per key, left rows with a match and the rows a left join returns (fanout) where "
            "recorded.", labels]
        if used_norms:
            lines.append("Key normalisations: " + "; ".join(f"{n} = {NORMALIZE_TEXT.get(n, 'as recorded by the builder')}"
                                                           for n in sorted(used_norms)) + ".")
        if any_expr:
            lines.append("`{col}~` = the key is computed from that column by an expression the builder's LLM wrote; "
                         "`sl joins <table>` shows the expression.")
        return lines
    lines = [
        "This layer records no measurement method; the description below is that of the builder version that wrote "
        "such layers.",
        "How it was measured: row counts are full counts (MongoDB: estimated document count). Column statistics use "
        f"the first {PROFILE_ROWS:,} rows each table returned (unordered LIMIT): null % and distinct count over those "
        f"rows (values compared as text); top values, samples and patterns over the first {VALUE_ROWS:,} non-null "
        "values; min/max for numeric columns. MongoDB `_id` fields are not profiled. Candidate keys = columns with "
        "no nulls and all values distinct in the profiled rows.",
        "Join candidates: column pairs proposed by the builder's LLM from the database description and the profile, "
        f"then measured by code on the first {JOIN_ROWS:,} rows of each table: distinct key values of each side are "
        "compared as text after the key normalisation; matched = keys found on both sides; side % = matched / that "
        "side's distinct keys (a missing value is compared as the text 'nan' or 'None').",
        labels,
    ]
    if used_norms:
        lines.append("Key normalisations: " + "; ".join(f"{n} = {NORMALIZE_TEXT.get(n, 'as recorded by the builder')}"
                                                       for n in sorted(used_norms)) + ".")
    if any_expr:
        lines.append("`{col}~` = the key is computed from that column by an expression the builder's LLM wrote; "
                     "`sl joins <table>` shows the expression.")
    return lines


def _tables_block(view, level):
    out = []
    for d in view.get("databases") or []:
        ts = d.get("tables") or []
        g = d.get("table_group")
        head = f"## {d['name']} ({d.get('engine') or '?'})"
        if g:
            listed = g.get("listed") or []
            head += (f": {_int(g.get('count'))} tables; the profile covers {len(ts)} of them "
                     f"({', '.join(g.get('profiled') or [])}) and records {len(listed)} of the {_int(g.get('count'))} "
                     f"names: {', '.join(listed)}")
            head += (f"; selection: {g['selection']}" if g.get("selection") else
                     "; selection: the first 3 names in the listing (the layer records no check that they represent the others)")
            for lay in g.get("layouts") or []:
                head += (f"; layout of {lay.get('columns') if lay.get('columns') is not None else '?'} columns: "
                         f"{_int(lay.get('tables'))} tables, profiled {len(lay.get('profiled') or [])}")
        else:
            head += f": {len(ts)} table{'s' if len(ts) != 1 else ''}"
        out.append(head)
        for t in ts:
            cols = t.get("columns")
            th = f"### {t['name']}: {_int(t.get('rows'))} rows"
            smp = t.get("sample") or {}
            if smp.get("method") and smp.get("method") not in ("all rows", "all documents"):
                th += f"; statistics over {_int(smp.get('rows'))} rows: {smp['method']}"
            if t.get("error"):
                th += f"; not profiled ({t['error'][:200]})"
            if level >= 4:
                out.append(th + (f", {len(cols)} columns" if cols is not None else ""))
                continue
            out.append(th)
            if not cols:
                continue
            if level == 3:
                out.append("  " + ", ".join(column_line(c, 3) for c in cols))
            else:
                out += ["  - " + column_line(c, level) for c in cols]
            keys = candidate_keys(t)
            if keys:
                out.append("  candidate keys: " + ", ".join(keys))
    return out


def _joins_block(view, limit=None):
    js = view.get("joins") or []
    um = view.get("joins_unmeasured") or []
    out = []
    if js:
        shown = js if limit is None else js[:limit]
        head = f"## Join candidates ({len(js)} measured"
        head += f"; showing {len(shown)} of {len(js)}, `sl joins` lists all)" if len(shown) < len(js) else ")"
        out.append(head)
        out += ["- " + _join_line(j) for j in shown]
    else:
        out.append("## Join candidates: none measured in this profile")
    if um:
        zero = [j for j in um if j["status"] == "zero_match"]
        nm = [j for j in um if j["status"] != "zero_match"]
        if zero:
            out.append(f"Proposed pairs with 0 matched keys ({len(zero)}): " +
                       "; ".join(f"{_side(j['left'])} = {_side(j['right'])}" for j in zero))
        if nm:
            out.append(f"Proposed pairs the builder could not measure ({len(nm)}): " +
                       "; ".join(f"{_side(j['left'])} = {_side(j['right'])}" for j in nm))
    return out


def overview(view, limit=OVERVIEW_CHARS):
    """The start-up overview: dataset-level, deterministic, at most `limit` chars (explicit notices on any cut)."""
    used = {j.get("normalize", "none") for j in view.get("joins") or []}
    any_expr = any("expr" in j[s] for j in view.get("joins") or [] for s in ("left", "right"))
    ntab = sum(len(d.get("tables") or []) for d in view.get("databases") or [])
    head = [status_line(view),
            f"# SAGE PROFILE: dataset {view.get('dataset')} ({len(view.get('databases') or [])} databases, {ntab} "
            "profiled tables)",
            "An offline profile of the provided databases, computed before this task from the same data. "
            "Drill down with `sl tables`, `sl table <name>`, `sl column <table>.<col>`, `sl joins [<table>]`, "
            "`sl search <term>`."]
    legend = _legend(view, used, any_expr)
    joins_full = _joins_block(view)
    for level in range(5):
        body = _tables_block(view, level)
        text = "\n".join(head + [f"Detail: {LEVEL_NOTE[level]}."] + legend + [""] + body + [""] + joins_full)
        if len(text) <= limit:
            return text
    # Still too long at the coarsest level: cut the join list, then the table list, saying so.
    body = _tables_block(view, 4)
    base = head + [f"Detail: {LEVEL_NOTE[4]}."] + legend + [""] + body + [""]
    js = view.get("joins") or []
    for k in range(len(js) - 1, -1, -1):
        text = "\n".join(base + _joins_block(view, limit=k))
        if len(text) <= limit:
            return text
    text = "\n".join(base + _joins_block(view, limit=0))
    note = ("\n[overview cut at {} of {} chars to fit the size limit; `sl tables` lists every table and "
            "`sl joins` every join candidate]")
    keep = max(0, limit - len(note) - 20)
    return text[:keep] + note.format(keep, len(text))


# ===================================================================================== drill-down

def _all_tables(view):
    for d in view.get("databases") or []:
        for t in d.get("tables") or []:
            yield d, t


def find_tables(view, name):
    name_l = name.lower()
    exact = [(d, t) for d, t in _all_tables(view) if name in (t["name"], f"{d['name']}.{t['name']}")]
    if exact:
        return exact
    return [(d, t) for d, t in _all_tables(view) if name_l in (t["name"].lower(), f"{d['name']}.{t['name']}".lower())]


def cmd_tables(view):
    out = []
    for d in view.get("databases") or []:
        g = d.get("table_group")
        out.append(f"{d['name']} ({d.get('engine') or '?'})" +
                   (f": {_int(g.get('count'))} tables in the database; profiled: "
                    f"{', '.join(g.get('profiled') or [])}; names recorded by the profile "
                    f"({len(g.get('listed') or [])} of {_int(g.get('count'))}): {', '.join(g.get('listed') or [])}"
                    if g else ""))
        for t in d.get("tables") or []:
            cols = t.get("columns")
            out.append(f"  {t['name']}: {_int(t.get('rows'))} rows" + (f", {len(cols)} columns" if cols is not None else "") +
                       (f"; not profiled ({t['error'][:200]})" if t.get("error") else ""))
    return "\n".join(out)


def _column_full(c):
    lines = [f"{c['name']}: type {c.get('dtype', '?')}, null {_pct(c.get('null_pct'))}, distinct {_int(c.get('distinct'))}"]
    if isinstance(c.get("range"), list) and len(c["range"]) == 2:
        lines.append(f"  min {_num(c['range'][0])}, max {_num(c['range'][1])}")
    vals = c.get("values") or {}
    if vals:
        d = c.get("distinct")
        note = (f" ({len(vals)} of {d} distinct values occur in the first {VALUE_ROWS:,} non-null values)"
                if isinstance(d, int) and len(vals) < d else "")
        lines.append(f"  value counts (first {VALUE_ROWS:,} non-null values){note}: " +
                     ", ".join(f"{_q(v)} {_num(n)}" for v, n in vals.items()))
    if c.get("samples"):
        lines.append("  first distinct sample values (each cut at 80 chars by the builder): " +
                     ", ".join(_q(s) for s in c["samples"]))
    for f in _flags(c):
        lines.append("  " + f)
    return "\n".join(lines)


def cmd_table(view, name, offset=0):
    hits = find_tables(view, name)
    if not hits:
        return None, f"no table {name!r} in the profile; `sl tables` lists them, `sl search {name}` searches names"
    if len(hits) > 1:
        return None, f"{name!r} is in several databases; use one of: " + ", ".join(f"{d['name']}.{t['name']}" for d, t in hits)
    d, t = hits[0]
    cols = t.get("columns") or []
    out = [f"{d['name']}.{t['name']} ({d.get('engine') or '?'}): {_int(t.get('rows'))} rows, {len(cols)} columns"]
    if t.get("error"):
        out.append(f"not profiled: {t['error']}")
    n = sample_rows(t)
    if n:
        out.append(f"column statistics over the first {n:,} rows")
    page = cols[offset: offset + PAGE_COLUMNS]
    if offset or len(page) < len(cols):
        nxt = offset + PAGE_COLUMNS
        out.append(f"showing columns {offset + 1}-{offset + len(page)} of {len(cols)}" +
                   (f"; next page: sl table {d['name']}.{t['name']} --offset {nxt}" if nxt < len(cols) else ""))
    out += [_column_full(c) for c in page]
    keys = candidate_keys(t)
    out.append("candidate keys (no nulls, all values distinct in the profiled rows): " + (", ".join(keys) if keys else "none"))
    js = [j for j in view.get("joins") or [] if _involves(j, f"{d['name']}.{t['name']}")]
    out.append(f"join candidates involving this table ({len(js)}):" if js else "join candidates involving this table: none")
    out += ["- " + _join_line(j, with_expr=True) for j in js]
    return "\n".join(out), None


def cmd_column(view, ref):
    parts = ref.split(".")
    tried = []
    for i in range(len(parts) - 1, 0, -1):
        tname, cname = ".".join(parts[:i]), ".".join(parts[i:])
        hits = find_tables(view, tname)
        tried.append(tname)
        if len(hits) > 1:
            return None, f"{tname!r} is in several databases; use one of: " + ", ".join(f"{d['name']}.{t['name']}.{cname}" for d, t in hits)
        if len(hits) == 1:
            d, t = hits[0]
            col = next((c for c in t.get("columns") or [] if c["name"] == cname), None) or \
                next((c for c in t.get("columns") or [] if c["name"].lower() == cname.lower()), None)
            if not col:
                return None, f"no column {cname!r} in {d['name']}.{t['name']}; `sl table {d['name']}.{t['name']}` lists its columns"
            out = [f"{d['name']}.{t['name']}.{col['name']} (table: {_int(t.get('rows'))} rows; statistics over the first "
                   f"{_int(sample_rows(t))} rows)", _column_full(col)]
            if col["name"] in candidate_keys(t):
                out.append("candidate key: no nulls, all values distinct in the profiled rows")
            js = [j for j in view.get("joins") or [] if any(
                _matches_table(j[s], f"{d['name']}.{t['name']}") and
                (j[s].get("column") == col["name"] or col["name"] in (j[s].get("source_columns") or []))
                for s in ("left", "right"))]
            out.append(f"join candidates using this column ({len(js)}):" if js else "join candidates using this column: none")
            out += ["- " + _join_line(j, with_expr=True) for j in js]
            return "\n".join(out), None
    return None, f"no table for {ref!r} (expected <table>.<column> or <db>.<table>.<column>); `sl tables` lists tables"


def cmd_joins(view, table=None):
    js = view.get("joins") or []
    um = view.get("joins_unmeasured") or []
    if table:
        hits = find_tables(view, table)
        if not hits:
            return None, f"no table {table!r} in the profile; `sl tables` lists them"
        keys = {f"{d['name']}.{t['name']}" for d, t in hits}
        js = [j for j in js if any(_involves(j, k) for k in keys)]
        um = [j for j in um if any(_involves(j, k) for k in keys)]
    used = {j.get("normalize", "none") for j in js}
    out = [f"{len(js)} measured join candidate{'s' if len(js) != 1 else ''}" + (f" involving {table}" if table else "") +
           f" (keys from the first {JOIN_ROWS:,} rows of each table, compared as text after the key normalisation; "
           "a side written <expression over ...> is computed by an expression the builder's LLM wrote):"]
    out += ["- " + _join_line(j, with_expr=True) for j in js]
    if used:
        out.append("key normalisations: " + "; ".join(f"{n} = {NORMALIZE_TEXT.get(n, 'as recorded by the builder')}" for n in sorted(used)))
    for status, title in (("zero_match", "proposed pairs with 0 matched keys"),
                          ("not_measured", "proposed pairs the builder could not measure")):
        sel = [j for j in um if j["status"] == status]
        if sel:
            out.append(f"{title} ({len(sel)}):")
            out += [f"- {_side(j['left'], True)} = {_side(j['right'], True)}" for j in sel]
    return "\n".join(out), None


def cmd_search(view, term):
    tl = term.lower()
    hits = []
    for d in view.get("databases") or []:
        if tl in d["name"].lower():
            hits.append(f"database  {d['name']}")
        for nm in (d.get("table_group") or {}).get("listed") or []:
            if tl in nm.lower():
                hits.append(f"table name recorded in {d['name']}  {nm}")
        for t in d.get("tables") or []:
            tf = f"{d['name']}.{t['name']}"
            if tl in t["name"].lower():
                hits.append(f"table     {tf}")
            for c in t.get("columns") or []:
                cf = f"{tf}.{c['name']}"
                if tl in c["name"].lower():
                    hits.append(f"column    {cf}")
                vals = list((c.get("values") or {}).keys())
                seen = set(vals)
                for v in vals + [s for s in c.get("samples") or [] if s not in seen]:
                    if tl in str(v).lower():
                        hits.append(f"value     {cf}  {_q(v, 100)}")
    out = [f"{len(hits)} match{'es' if len(hits) != 1 else ''} for {term!r} in database, table and column names and in "
           f"profiled top/sample values (values only cover what the profile recorded, not the full data)"]
    shown = hits[:SEARCH_LIMIT]
    out += shown
    if len(shown) < len(hits):
        out.append(f"[showing {len(shown)} of {len(hits)} matches; use a longer term to narrow]")
    return "\n".join(out), None


USAGE = """sl: offline profile of the provided databases (tables, columns, types, row counts, null and distinct counts,
sample values, key and join candidates with measured match rates), computed before this task from the same data.
  sl overview                       the start-up overview
  sl tables                         every database and table with row and column counts
  sl table <table> [--offset N]     every column of one table with its statistics, candidate keys and joins
  sl column <table>.<column>        one column in full
  sl joins [<table>]                measured join candidates (all, or those involving one table)
  sl search <term>                  search database/table/column names and profiled values
<table> may be written <db>.<table>."""


def load_view(path=None):
    p = path or os.environ.get("SCRYBE_SAGE_FILE") or DEFAULT_FILE
    try:
        with open(p, encoding="utf-8") as fh:
            v = json.load(fh)
    except FileNotFoundError:
        return None, f"no profile is available for this dataset ({p} not found)"
    except (OSError, ValueError) as e:
        return None, f"the profile file {p} could not be read ({type(e).__name__})"
    if not isinstance(v, dict) or v.get("format") != VIEW_FORMAT:
        return None, f"the profile file {p} is not a {VIEW_FORMAT} view"
    return v, None


def is_view(obj):
    return isinstance(obj, dict) and obj.get("format") == VIEW_FORMAT


COMMANDS = ("overview", "tables", "table", "column", "joins", "search")


def command(view, cmd, rest, overview_limit=OVERVIEW_CHARS):
    """Run one drill-down command on a loaded view -> exit code (prints the output)."""
    if cmd == "overview":
        print(overview(view, overview_limit))
        return 0
    if cmd == "tables":
        print(cmd_tables(view))
        return 0
    if cmd in ("table", "column", "search") and not rest:
        print(f"sl {cmd}: missing argument\n{USAGE}")
        return 2
    if cmd == "table":
        offset = 0
        if "--offset" in rest:
            i = rest.index("--offset")
            try:
                offset = max(0, int(rest[i + 1]))
            except (IndexError, ValueError):
                print("sl table: --offset needs a number")
                return 2
            rest = rest[:i] + rest[i + 2:]
        out, err = cmd_table(view, " ".join(rest), offset)
    elif cmd == "column":
        out, err = cmd_column(view, " ".join(rest))
    elif cmd == "joins":
        out, err = cmd_joins(view, " ".join(rest) or None)
    elif cmd == "search":
        out, err = cmd_search(view, " ".join(rest))
    else:
        print(f"sl: unknown command {cmd!r}\n{USAGE}", file=sys.stderr)
        return 2
    if err:
        print(f"sl: {err}")
        return 1
    print(out)
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "extract":
        if len(argv) != 4:
            print("usage: sage_dbview.py extract <layer_dir> <out.json>", file=sys.stderr)
            return 2
        try:
            v = extract(argv[2])
        except FileNotFoundError as e:
            print(f"sage extract: {e}", file=sys.stderr)
            return 3
        tmp = argv[3] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, indent=1, sort_keys=False)
        os.replace(tmp, argv[3])
        return 0
    args = argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd not in COMMANDS:
        print(f"sl: unknown command {cmd!r}\n{USAGE}", file=sys.stderr)
        return 2
    view, err = load_view()
    if view is None:
        if cmd == "overview":          # the start-up output states that no profile is available
            state = "missing" if "not found" in err else "failed"
            print(f"SAGE status={state}: {err}; this run has no Sage profile view")
            print(f"sl: {err}", file=sys.stderr)
            return EXIT_NO_VIEW
        print(f"sl: {err}")
        return 1
    try:
        limit = int(os.environ.get("SCRYBE_SAGE_OVERVIEW_CHARS") or OVERVIEW_CHARS)
    except ValueError:
        limit = OVERVIEW_CHARS
    return command(view, cmd, rest, limit)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
