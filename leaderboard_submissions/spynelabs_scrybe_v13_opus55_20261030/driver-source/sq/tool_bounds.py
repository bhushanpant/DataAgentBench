#!/usr/bin/env python3
"""bounds: how much can the answer move because some rows are uncertain (unmatched join rows, low-confidence labels)?

  bounds --in rows.parquet --agg count|sum|mean [--value col] (--status col | --p col) [--group col] [--lo 0.2 --hi 0.8]

Each row is IN the answer, OUT of it, or UNKNOWN. --status names a column holding in/out/unknown; --p names a probability
column (jlabel's p_yes): p >= hi is in, p <= lo is out, in between is unknown. The tool prints the answer with the unknown
rows all out (low), all in (high), the best-case and worst-case for a mean, and the probability-weighted expectation.
If low and high give the same answer to the precision you report, the noise does not matter; if not, say so in the
RED FLAG line and report the best-supported reading. Python: from bounds import ranges.
"""
import argparse
import sys


def ranges(status, values=None, agg="count", p=None) -> dict:
    """status: list of 'in'|'out'|'unknown'. values: numbers per row (for sum/mean). p: optional probabilities."""
    n = len(status)
    vals = [0.0] * n if values is None else [float(v) for v in values]
    ins = [vals[i] for i in range(n) if status[i] == "in"]
    unk = [vals[i] for i in range(n) if status[i] == "unknown"]
    res = {"in": len(ins), "unknown": len(unk)}
    if agg == "count":
        res.update(low=len(ins), high=len(ins) + len(unk))
    elif agg == "sum":
        res.update(low=sum(ins) + sum(v for v in unk if v < 0), high=sum(ins) + sum(v for v in unk if v > 0))
    elif agg == "mean":
        def extreme(order):
            tot, cnt = sum(ins), len(ins)
            best = tot / cnt if cnt else None
            for v in order:
                tot, cnt = tot + v, cnt + 1
                m = tot / cnt
                if best is None or (m > best if order is up else m < best):
                    best = m
                else:
                    break
            return best
        up, down = sorted(unk, reverse=True), sorted(unk)
        res.update(low=extreme(down), high=extreme(up))
    else:
        raise ValueError(f"unknown agg {agg!r}")
    if p is not None:
        w = [float(x) for x in p]
        wsum = sum(w)
        res["expected"] = (sum(w) if agg == "count" else sum(w[i] * vals[i] for i in range(n)) if agg == "sum"
                           else (sum(w[i] * vals[i] for i in range(n)) / wsum if wsum else None))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--agg", required=True, choices=["count", "sum", "mean"])
    ap.add_argument("--value")
    ap.add_argument("--status")
    ap.add_argument("--p")
    ap.add_argument("--group")
    ap.add_argument("--lo", type=float, default=0.2)
    ap.add_argument("--hi", type=float, default=0.8)
    a = ap.parse_args(argv)
    if bool(a.status) == bool(a.p):
        ap.error("give exactly one of --status or --p")
    if a.agg != "count" and not a.value:
        ap.error("--value is required for sum and mean")
    import pandas as pd
    path = a.inp.lower()
    df = pd.read_parquet(a.inp) if path.endswith(".parquet") else pd.read_csv(a.inp) if path.endswith(".csv") else pd.read_json(a.inp)
    if a.status:
        status = df[a.status].astype(str).str.lower().tolist()
        bad = sorted(set(status) - {"in", "out", "unknown"})
        if bad:
            sys.exit(f"bounds: {a.status} must hold in/out/unknown, found {bad[:5]}")
        probs = None
    else:
        probs = df[a.p].astype(float).tolist()
        status = ["in" if x >= a.hi else "out" if x <= a.lo else "unknown" for x in probs]
    df = df.assign(_status=status, _p=probs if probs is not None else 0.0)
    groups = [(None, df)] if not a.group else list(df.groupby(a.group))
    shown = 0
    for key, g in groups:
        r = ranges(g["_status"].tolist(), g[a.value].tolist() if a.value else None, a.agg,
                   g["_p"].tolist() if probs is not None else None)
        tag = "" if key is None else f"{a.group}={key}: "
        exp = f", expected {r['expected']:.4g}" if r.get("expected") is not None else ""
        print(f"{tag}{a.agg}: low {r['low']:.6g} .. high {r['high']:.6g}{exp}   (in {r['in']}, unknown {r['unknown']} of {len(g)} rows)")
        shown += 1
        if shown >= 50:
            print("... (first 50 groups)")
            break
    tot_unk = sum(1 for s in status if s == "unknown")
    if tot_unk == 0:
        print("no unknown rows: the answer does not depend on uncertain rows.")
    else:
        print(f"{tot_unk} uncertain rows: if low and high differ at the precision you report, the answer depends on them.")


def _note(text: str):
    import json
    import os
    import urllib.request
    url, token = os.environ.get("JLABEL_URL"), os.environ.get("JLABEL_TOKEN")
    if not (url and token):
        return
    try:
        req = urllib.request.Request(url + "/ask", data=json.dumps({"tool": "note", "how": text}).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
    _note("bounds")
