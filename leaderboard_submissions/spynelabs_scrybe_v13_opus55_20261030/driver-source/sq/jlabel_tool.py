#!/usr/bin/env python3
"""jlabel: label rows of text with Jev, a fast calibrated classifier, instead of reading and judging them one by one.

  jlabel --in rows.parquet --fields title,description \\
         --categories "A,B,C" [--describe "A=what A covers|B=what B covers"] \\
         [--question "Which category does this belong to?"] [--out labeled.parquet] [--uncertain 10]
  jlabel --in rows.parquet --fields review --yes-no "Does the review complain about delivery?" [--out p.parquet]

--in takes parquet, csv, json or jsonl (a dbq result is saved as parquet). --fields are the columns the label may
depend on (default: every text column). You define the categories and the question; jlabel adds `label` and
`label_conf` (or `p_yes`) columns and prints counts per label plus the least certain rows to check by hand.
Identical rows are sent once. Large inputs take several calls: when it prints PARTIAL, run the same command again.
Python: from jlabel import label_rows
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("JLABEL_URL", "http://host.docker.internal:18765")
TOKEN = os.environ.get("JLABEL_TOKEN", "")
BATCH = 400
TIME_BUDGET_S = float(os.environ.get("JLABEL_TIME_BUDGET", "150"))  # a tool call running past ~4 minutes kills the whole worker session
CONFIDENT = 0.9
CACHE = os.environ.get("JLABEL_CACHE", "/home/ide/dab-work/.jlabel_cache.jsonl")


def _post(body: dict) -> dict:
    for attempt in range(4):
        req = urllib.request.Request(URL + "/label", data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + TOKEN)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 503:
                sys.exit("jlabel: the label service is out of credits right now; label by reading the text instead (do not retry)")
            if e.code in (400, 401, 429):  # not retryable
                sys.exit(f"jlabel: {e.code} {detail}")
            err = f"{e.code} {detail}"
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
        if attempt == 3:
            sys.exit(f"jlabel: label service unreachable ({err}); fall back to labelling by reading the text")
        time.sleep(2 * (attempt + 1))


def _key(item, instructions, categories, yes_no) -> str:
    raw = json.dumps([item, instructions, categories, yes_no], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _read_cache() -> dict:
    done = {}
    try:
        with open(CACHE) as f:
            for line in f:
                k, v = json.loads(line)
                done[k] = v
    except (OSError, ValueError):
        pass
    return done


def label_rows(items: list, instructions: str, categories: dict = None, yes_no: bool = False,
               time_budget: float = None) -> tuple:
    """Label each item (a dict of named fields, or a string). Returns (results in order, complete).

    Results are cached on disk as they arrive and the call stops at the time budget; calling again with the same
    arguments continues where it stopped, so no single call runs long enough to be killed."""
    budget = TIME_BUDGET_S if time_budget is None else time_budget
    t0 = time.time()
    keys = [_key(i, instructions, categories, yes_no) for i in items]
    done = _read_cache()
    todo = list(dict.fromkeys(k for k in keys if k not in done))
    first = {k: i for k, i in zip(keys, items)}
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    for start in range(0, len(todo), BATCH):
        if time.time() - t0 > budget:
            break
        chunk = todo[start:start + BATCH]
        body = {"mode": "noul" if yes_no else "choice", "instructions": instructions,
                "items": [first[k] for k in chunk]}
        if not yes_no:
            body["categories"] = categories
        results = _post(body)["results"]
        with open(CACHE, "a") as f:
            for k, r in zip(chunk, results):
                if "error" not in r:  # failed rows are retried on the next call
                    done[k] = r
                    f.write(json.dumps([k, r]) + "\n")
    out = [done.get(k) for k in keys]
    return out, all(o is not None for o in out)


def ranges_choice(res: list, names: list, sure: float = CONFIDENT) -> dict:
    """Per category: rows surely in (label chosen with confidence >= sure), rows possibly in (its probability >= 0.1), and
    the probability-weighted expectation. The count of a category lies between low and high."""
    out = {}
    for n in names:
        low = sum(1 for r in res if r and r.get("label") == n and (r.get("confidence") or 0) >= sure)
        high = sum(1 for r in res if r and (r.get("probabilities") or {}).get(n, 0.0) >= 0.1 or
                   (r and r.get("label") == n and (r.get("confidence") or 0) >= sure))
        exp = sum((r.get("probabilities") or {}).get(n, 0.0) for r in res if r)
        out[n] = {"low": low, "high": max(high, low), "expected": exp}
    return out


def ranges_yes_no(ps: list, lo: float = 0.2, hi: float = 0.8) -> dict:
    ps = [p for p in ps if p is not None]
    return {"low": sum(p >= hi for p in ps), "high": sum(p > lo for p in ps), "expected": sum(ps)}


def _judge(sub, fields, names, instructions, desc, chunk: int = 60):
    """Independent judge labels the rows of `sub` (a DataFrame slice); returns {row position in sub: label}."""
    shape = "\n".join(f"- {k}" + (f": {desc[k]}" if desc.get(k) else "") for k in names)
    out = {}
    for start in range(0, len(sub), chunk):
        part = sub.iloc[start:start + chunk]
        rows = [[start + i, {f: str(r[f])[:500] for f in fields}] for i, (_, r) in enumerate(part.iterrows())]
        body = {"tool": "judge", "question": "", "focus": instructions, "shape": shape,
                "profile": json.dumps(rows, ensure_ascii=False)}
        req = urllib.request.Request(URL + "/ask", data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + TOKEN)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=200) as r:
                for x in json.loads(json.loads(r.read())["text"]):
                    if x["label"] in names:
                        out[int(x["id"])] = x["label"]
        except Exception:  # noqa: BLE001 - review is optional; the labels stand
            break
    return out


def review(df, fields, names, instructions, desc, yes_no: bool, limit: int = 120):
    """Send the rows jlabel is unsure about (confidence < 0.9, or 0.2 < p_yes < 0.8) to the judge and keep its label for
    them. Adds label_final / label_source (choice) or yes_final / label_source (yes/no). Returns a printable summary."""
    if yes_no:
        unsure = df[df["p_yes"].between(0.2, 0.8)]
    else:
        unsure = df[df["label_conf"] < CONFIDENT]
    unsure = unsure.sort_values("p_yes" if yes_no else "label_conf", key=(lambda c: (c - 0.5).abs()) if yes_no else None).head(limit)
    final = (df["p_yes"] >= 0.5).map({True: "yes", False: "no"}) if yes_no else df["label"].astype(object)
    source = ["jev"] * len(df)
    flipped = 0
    if len(unsure):
        got = _judge(unsure, fields, ["yes", "no"] if yes_no else names, instructions, {} if yes_no else desc)
        pos = {idx: k for k, idx in enumerate(df.index)}
        for i, (idx, _) in enumerate(unsure.iterrows()):
            if i in got:
                k = pos[idx]
                flipped += final.iloc[k] != got[i]
                final.iloc[k] = got[i]
                source[k] = "judge"
    df["yes_final" if yes_no else "label_final"] = final.values
    df["label_source"] = source
    n = sum(1 for x in source if x == "judge")
    counts = df["yes_final" if yes_no else "label_final"].value_counts().to_string().replace("\n", "; ")
    return (f"REVIEW: {len(unsure)} uncertain rows went to an independent judge ({n} answered), which changed {int(flipped)} "
            f"label(s). Counts after review (column {'yes_final' if yes_no else 'label_final'}): {counts}")


def calibrate(df, fields, names, instructions, desc, yes_no, n: int):
    """A stratified sample of the labelled rows goes to an independent judge. Reports agreement, the confusion between
    jlabel's labels and the judge's, and counts per label corrected for that confusion with an approximate 95% interval.
    The correction is relative to the judge, which is the same kind of model as the labeller but reads each row afresh with
    the category descriptions; a large gap between raw and corrected counts means the labels are biased."""
    import math
    col = None
    if yes_no:
        keys = ["yes" if (p or 0) >= 0.5 else "no" for p in df["p_yes"]]
    else:
        keys = [str(x) for x in df["label"]]
    df = df.assign(_k=keys)
    groups = {k: g for k, g in df.groupby("_k")}
    total = len(df)
    take = {}
    for k, g in groups.items():  # proportional share of n, at least 15 (or the whole group) so small classes are measured
        take[k] = min(len(g), max(15, round(n * len(g) / total)))
    sample = df.iloc[0:0]
    parts = [g.sample(n=take[k], random_state=7) for k, g in groups.items()]
    import pandas as pd
    sample = pd.concat(parts)
    got = _judge(sample, fields, names, instructions, {} if yes_no else desc)
    if not got:
        return ("CALIBRATION FAILED: the independent judge could not be reached. Do not report these label counts as "
                "reliable: say so in a RED FLAG line, or label the decisive rows by reading them.")
    judged = [(sample.iloc[i]["_k"], got[i]) for i in range(len(sample)) if i in got]
    agree = sum(a == b for a, b in judged) / len(judged)
    lines = [f"CALIBRATION: an independent judge labelled {len(judged)} rows sampled from every label; agreement with jlabel {agree:.0%}."]
    est, var = {k: 0.0 for k in names}, {k: 0.0 for k in names}
    conf = {}
    for k, g in groups.items():
        js = [b for a, b in judged if a == k]
        if not js:
            continue
        conf[k] = {j: js.count(j) / len(js) for j in set(js)}
        for j in names:
            p = js.count(j) / len(js)
            est[j] += len(g) * p
            var[j] += len(g) ** 2 * p * (1 - p) / len(js)
    for k in sorted(conf):
        lines.append(f"  jlabel '{k}' ({len(groups[k])} rows, {sum(1 for a, b in judged if a == k)} judged): judge says " +
                     ", ".join(f"{j} {100 * v:.0f}%" for j, v in sorted(conf[k].items(), key=lambda kv: -kv[1])))
    raw = {k: len(groups.get(k, [])) for k in names}
    lines.append("CORRECTED COUNTS (relative to the judge): " + "; ".join(
        f"{k}: raw {raw[k]} -> {est[k]:.0f} (95% {max(0, est[k] - 1.96 * math.sqrt(var[k])):.0f}..{est[k] + 1.96 * math.sqrt(var[k]):.0f})" for k in names))
    gap = max((abs(est[k] - raw[k]) for k in names), default=0)
    if agree < 0.9 or gap > 0.02 * total:
        lines.append("  The labels are biased or noisy at this precision: an answer that depends on an exact count or share should "
                     "use the corrected counts, name the interval in a RED FLAG line, and sharpen --describe for the class pair "
                     "that disagrees most, then rerun.")
    else:
        lines.append("  labels look reliable at this sample size.")
    return "\n".join(lines)


def _load(path: str):
    import pandas as pd
    p = path.lower()
    if p.endswith(".parquet"):
        return pd.read_parquet(path)
    if p.endswith(".csv"):
        return pd.read_csv(path)
    if p.endswith(".jsonl"):
        return pd.read_json(path, lines=True)
    return pd.read_json(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--fields", help="comma-separated columns the label depends on (default: all text columns)")
    ap.add_argument("--categories", help="comma-separated category names")
    ap.add_argument("--describe", help="optional 'name=description|name=description' for the categories")
    ap.add_argument("--yes-no", dest="yes_no", help="a yes/no condition; adds p_yes (probability of yes)")
    ap.add_argument("--question", help="instruction for a category label (default: pick the best category)")
    ap.add_argument("--out", help="output file (parquet); default /home/ide/dab-work/results/labeled_<time>.parquet")
    ap.add_argument("--uncertain", type=int, default=10, help="show the N least certain rows (default 10)")
    ap.add_argument("--calibrate", type=int, default=150,
                    help="rows an independent judge re-labels, sampled from every label, to correct the counts (default 150; 0 = off; skipped under 60 rows)")
    ap.add_argument("--no-review", dest="no_review", action="store_true",
                    help="do not send the uncertain rows to the independent judge (default: they are sent)")
    a = ap.parse_args(argv)
    if bool(a.categories) == bool(a.yes_no):
        ap.error("give exactly one of --categories or --yes-no")
    if not TOKEN:
        sys.exit("jlabel: no label token in this sandbox; label by reading the text instead")
    df = _load(a.inp)
    fields = [f.strip() for f in a.fields.split(",")] if a.fields else [c for c in df.columns if df[c].dtype == object]
    missing = [f for f in fields if f not in df.columns]
    if missing or not fields:
        sys.exit(f"jlabel: columns not found {missing}; available: {list(df.columns)}")
    items = [{f: ("" if row[f] is None else str(row[f])) for f in fields} for row in df.to_dict("records")]
    if a.yes_no:
        res, complete = label_rows(items, a.yes_no, yes_no=True)
    else:
        names = [c.strip() for c in a.categories.split(",") if c.strip()]
        desc = dict(p.split("=", 1) for p in (a.describe or "").split("|") if "=" in p)
        cats = {n: desc.get(n) for n in names}
        q = a.question or "Which category does this item belong to? Pick the best one."
        res, complete = label_rows(items, q, categories=cats)
    if not complete:
        have = sum(r is not None for r in res)
        print(f"PARTIAL: {have} of {len(res)} rows labelled in this call (progress is saved). "
              f"Run the SAME command again to continue; nothing is written until every row is labelled.")
        sys.exit(3)
    if a.yes_no:
        df["p_yes"] = [r.get("probability") for r in res]
        bad = int(df["p_yes"].isna().sum())
    else:
        df["label"] = [r.get("label") for r in res]
        df["label_conf"] = [r.get("confidence") for r in res]
        for n in names:  # probability of each category: `bounds --p p_<category>` turns this into a count range
            df[f"p_{n}"] = [(r.get("probabilities") or {}).get(n, 0.0) for r in res]
        bad = int(df["label"].isna().sum())
    out = a.out or f"/home/ide/dab-work/results/labeled_{int(time.time())}.parquet"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df.to_parquet(out)
    print(f"rows {len(df)}, labelled {len(df) - bad}, failed {bad}; saved {out}")
    if a.yes_no:
        print(f"p_yes>=0.5: {int((df['p_yes'] >= 0.5).sum())}; unsure (0.2<p<0.8): {int(df['p_yes'].between(0.2, 0.8).sum())}")
        unsure = df.assign(u=(df["p_yes"] - 0.5).abs()).sort_values("u").head(a.uncertain)
    else:
        print(df["label"].value_counts(dropna=False).to_string())
        print(f"mean confidence {df['label_conf'].mean():.3f}; rows below 0.9: {int((df['label_conf'] < 0.9).sum())}")
        unsure = df.sort_values("label_conf").head(a.uncertain)
    if a.yes_no:
        rg = ranges_yes_no(df["p_yes"].tolist())
        print(f"RANGE yes: sure {rg['low']} .. possible {rg['high']}; expected {rg['expected']:.1f} of {len(df)} rows")
    else:
        for n, rg in ranges_choice(res, names).items():
            print(f"RANGE {n}: sure {rg['low']} .. possible {rg['high']}; expected {rg['expected']:.1f}")
        print("(an answer that moves between the sure and possible counts depends on uncertain labels; `bounds --p p_<label>` shows how)")
    if not a.no_review and (len(df) >= 30):
        print(review(df, fields, ["yes", "no"] if a.yes_no else names, a.yes_no or q, {} if a.yes_no else desc,
                     bool(a.yes_no)))
        df.to_parquet(out)
    if a.calibrate and len(df) >= 60:
        print(calibrate(df, fields, ["yes", "no"] if a.yes_no else names, a.yes_no or q,
                        {} if a.yes_no else desc, bool(a.yes_no), a.calibrate))
    if a.uncertain and len(unsure):
        print(f"\nleast certain {len(unsure)} rows (check these by hand):")
        for _, row in unsure.iterrows():
            shown = " | ".join(str(row[f])[:100] for f in fields)
            tail = f"p_yes={row['p_yes']:.2f}" if a.yes_no else f"{row['label']} ({row['label_conf']:.2f})"
            print(f"  {tail}  <- {shown}")
    if bad > 0.05 * len(df):
        sys.exit(f"jlabel: {bad} of {len(df)} rows failed; do not rely on this output")


if __name__ == "__main__":
    main()
