#!/usr/bin/env python3
"""joincheck: do two columns join, and after which clean-up? Measured on the data, no model involved.

  joincheck <db>.<table>.<column> <db>.<table>.<column> [--limit 200000]

Pulls the distinct values of both columns, prints the value formats side by side, the raw match rate, and the match rate
after each simple normalisation (trim, case, digits only, alphanumerics only, leading zeros, a shared prefix or suffix
removed, integer cast). Use it before joining on identifiers that look different in the two sources. Python:
from joincheck import compare.
"""
import argparse
import collections
import json
import os
import re
import sys

KIT = os.environ.get("KIT_HOME", "/home/ide/dab")
sys.path.insert(0, f"{KIT}/lib")


def _values(ref: str, limit: int):
    """Distinct non-null values of a column with their row counts -> (Counter of str -> rows, truncated)."""
    from multidb import dbs, q
    db, rest = ref.split(".", 1)
    table, col = rest.rsplit(".", 1)
    kind = dbs().get(db)
    if kind is None:
        sys.exit(f"joincheck: unknown database {db!r}; available: {', '.join(sorted(dbs()))}")
    if kind in ("mongo", "mongodb"):
        df = q(db, json.dumps({"collection": table, "pipeline": [
            {"$group": {"_id": f"${col}", "n": {"$sum": 1}}}, {"$limit": limit + 1}]}))
        pairs = list(zip(df["_id"].tolist(), df["n"].tolist())) if "_id" in df else []
    else:
        df = q(db, f'SELECT "{col}" AS v, COUNT(*) AS n FROM "{table}" WHERE "{col}" IS NOT NULL GROUP BY "{col}" LIMIT {limit + 1}')
        pairs = list(zip(df["v"].tolist(), df["n"].tolist()))
    return collections.Counter({str(v): int(n) for v, n in pairs if v is not None}), len(pairs) > limit


def shape(v: str) -> str:
    return re.sub(r"[a-z]+", "a", re.sub(r"[A-Z]+", "A", re.sub(r"\d+", "9", v)))[:30]


def _common(vals, end):
    key = (lambda v: v[:3]) if end == "p" else (lambda v: v[-3:])
    cnt = collections.Counter(key(v) for v in vals if len(v) > 3)
    if not cnt:
        return ""
    top = cnt.most_common(1)[0][0]
    same = [v if end == "p" else v[::-1] for v in vals if len(v) > 3 and key(v) == top]
    pre = os.path.commonprefix(same)
    return pre if end == "p" else pre[::-1]


NORMS = {
    "trim": lambda v: v.strip(),
    "lower": lambda v: v.strip().lower(),
    "digits only": lambda v: re.sub(r"\D", "", v),
    "alphanumerics only, lower": lambda v: re.sub(r"[^0-9a-z]", "", v.lower()),
    "digits only, leading zeros removed": lambda v: re.sub(r"\D", "", v).lstrip("0") or "0",
    "leading zeros removed": lambda v: v.strip().lstrip("0") or "0",
    "integer cast": lambda v: str(int(float(v))) if re.fullmatch(r"\s*-?\d+(\.0+)?\s*", v) else "\x00" + v,
}


def candidates(sa, sb):
    """The normalisations to try: (name, left fn, right fn). Includes a shared prefix/suffix on one side."""
    ident = lambda v: v  # noqa: E731
    rows = [("as stored", ident, ident)] + [(n, f, f) for n, f in NORMS.items()]
    for end in ("p", "s"):
        for side, vals in (("left", sa), ("right", sb)):
            c = _common(vals, end)
            if not c or sum(v.startswith(c) if end == "p" else v.endswith(c) for v in vals) <= 0.5 * len(vals):
                continue
            cut = (lambda v, c=c: v[len(c):] if v.startswith(c) else v) if end == "p" else \
                  (lambda v, c=c: v[:-len(c)] if v.endswith(c) else v)
            rows.append((f"{'prefix' if end == 'p' else 'suffix'} {c!r} removed from {side} only",
                         cut if side == "left" else ident, cut if side == "right" else ident))
    return rows


def _group(counts, fn):
    out = collections.Counter()
    for v, n in counts.items():
        out[fn(v)] += n
    return out


def analyze(ca, cb) -> dict:
    """ca, cb: Counter of value -> rows. Per normalisation: distinct-key match both ways, share of ROWS matched on each
    side, keys that stop being unique after the clean-up, and the fan-out of an inner join (joined rows / left rows)."""
    sa, sb = list(ca), list(cb)
    res = []
    for name, fl, fr in candidates(sa, sb):
        ga, gb = _group(ca, fl), _group(cb, fr)
        both = set(ga) & set(gb)
        rows_a, rows_b = sum(ga.values()), sum(gb.values())
        joined = sum(ga[k] * gb[k] for k in both)
        res.append({"norm": name, "keys_a": len(ga), "keys_b": len(gb), "matched_keys": len(both),
                    "rate_a": len(both) / len(ga) if ga else 0.0, "rate_b": len(both) / len(gb) if gb else 0.0,
                    "rows_matched_a": sum(ga[k] for k in both) / rows_a if rows_a else 0.0,
                    "rows_matched_b": sum(gb[k] for k in both) / rows_b if rows_b else 0.0,
                    "dup_keys_a": sum(1 for n in ga.values() if n > 1), "dup_keys_b": sum(1 for n in gb.values() if n > 1),
                    "fanout": joined / rows_a if rows_a else 0.0,
                    "collapsed_a": len(ca) - len(ga), "collapsed_b": len(cb) - len(gb)})
    best = max(res, key=lambda r: (min(r["rows_matched_a"], r["rows_matched_b"]), -r["collapsed_a"] - r["collapsed_b"]))
    return {"results": res, "best": best}


def unmatched(ca, cb, fl, fr, k: int = 8):
    """Sample unmatched keys on each side under the chosen clean-up, each with its closest key on the other side."""
    import difflib
    ga, gb = {fl(v): v for v in ca}, {fr(v): v for v in cb}
    out = []
    for side, mine, other in (("left", ga, gb), ("right", gb, ga)):
        miss = sorted((k2 for k2 in mine if k2 not in other), key=lambda x: -(ca if side == "left" else cb)[mine[x]])[:k]
        pool = list(other)[:5000]
        for m in miss:
            near = difflib.get_close_matches(m, pool, n=1, cutoff=0.6)
            out.append((side, mine[m], other[near[0]] if near else None))
    return out


def compare(a_ref: str, b_ref: str, limit: int = 200000) -> str:
    (ca, ta), (cb, tb) = _values(a_ref, limit), _values(b_ref, limit)
    out = [f"{a_ref}: {len(ca)} distinct keys / {sum(ca.values())} rows{' (truncated; rates are estimates)' if ta else ''}, e.g. {list(ca)[:4]}",
           f"{b_ref}: {len(cb)} distinct keys / {sum(cb.values())} rows{' (truncated; rates are estimates)' if tb else ''}, e.g. {list(cb)[:4]}"]
    for name, c in ((a_ref, ca), (b_ref, cb)):
        top = collections.Counter(shape(v) for v in c).most_common(3)
        out.append(f"formats in {name}: " + ", ".join(f"{s} x{n}" for s, n in top))
    if not ca or not cb:
        return "\n".join(out + ["one side is empty"])
    r = analyze(ca, cb)
    out.append("per clean-up: share of keys found on the other side (left/right), share of ROWS that join (left/right), "
               "fan-out (joined rows per left row):")
    for x in r["results"]:
        out.append(f"  keys {x['rate_a']:5.1%}/{x['rate_b']:5.1%}  rows {x['rows_matched_a']:5.1%}/{x['rows_matched_b']:5.1%}  "
                   f"fan-out {x['fanout']:.2f}  {x['norm']}" + (f"  [merges {x['collapsed_a'] + x['collapsed_b']} distinct keys]" if x["collapsed_a"] + x["collapsed_b"] else ""))
    b = r["best"]
    out.append(f"best: {b['norm']}: {b['rows_matched_a']:.1%} of left rows and {b['rows_matched_b']:.1%} of right rows join; fan-out {b['fanout']:.2f}.")
    if b["fanout"] > 1.05 or b["dup_keys_a"] and b["dup_keys_b"]:
        out.append(f"WARNING many-to-many: {b['dup_keys_a']} left and {b['dup_keys_b']} right keys repeat. A join multiplies rows "
                   f"(x{b['fanout']:.2f}); aggregate each side to one row per key first, or count distinct entities.")
    low = min(b["rows_matched_a"], b["rows_matched_b"])
    if low < 0.98:
        fl = fr = None
        for name, f1, f2 in candidates(list(ca), list(cb)):
            if name == b["norm"]:
                fl, fr = f1, f2
        out.append("unmatched keys under the best clean-up (most frequent first), with the closest key on the other side:")
        for side, k, near in unmatched(ca, cb, fl, fr):
            out.append(f"  {side}: {k!r}  closest other-side: {near!r}")
        out.append(f"{1 - low:.1%} of rows on the weaker side do not join. Decide what they are (a different key format, "
                   f"records that truly have no partner, or rows lost): then run `bounds` to see whether the answer depends on them.")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--limit", type=int, default=200000)
    a = ap.parse_args(argv)
    try:
        out = compare(a.a, a.b, a.limit)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - never leave the worker with silent empty output
        _note(f"joincheck {a.a} {a.b} ERROR {type(e).__name__}")
        sys.exit(f"joincheck: could not compare ({type(e).__name__}: {str(e)[:300]}). Check the column references and "
                 f"types (it needs <db>.<table>.<column>); free-text or very wide columns are not supported; compare a "
                 f"key you extract into a small query result instead.")
    if not out.strip():
        sys.exit("joincheck: produced no output; treat the join keys as unverified and check them with a query.")
    print(out)
    _note(f"joincheck {a.a} {a.b}")


def _note(text: str):
    """Tell the host-side service this tool was used (logged for the trial's signals); best effort."""
    url, token = os.environ.get("JLABEL_URL"), os.environ.get("JLABEL_TOKEN")
    if not (url and token):
        return
    try:
        import urllib.request
        req = urllib.request.Request(url + "/ask", data=json.dumps({"tool": "note", "how": text}).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
