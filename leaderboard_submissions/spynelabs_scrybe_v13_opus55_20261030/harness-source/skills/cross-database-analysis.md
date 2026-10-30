---
name: cross-database-analysis
description: Answering one question from several databases in Python - inventory, key normalisation, match-rate checks, provenance
platforms: [python, any]
---
**1. Inventory every source before writing logic**
- For each database: list tables/collections, row counts, columns + types, 5 sample rows. Note which entity each source describes and at what grain (one row per what?).
- Identify the candidate join keys in each source and print 10 raw values of each side by side.
- Note the date fields, their format/type and time zone, and any free-text fields that may hold needed values.
- Re-read the question: list every entity, filter, metric and time window it mentions, and map each to a source + column.

**2. Extract only what is needed**
- Push filters and projections into each source query (SQL `where`, Mongo `$match`/projection). Do not pull whole tables "just in case".
- One function per source returning a DataFrame; print `shape` and `head()` after each.
- Read ids as strings (`dtype=str`, `::text`, `$toString`) so leading zeros and long ids survive.

**3. Normalise join keys (same rule on both sides)**
```python
def norm_key(s):
    s = s.astype("string").str.strip().str.upper()
    s = s.str.replace(r"\.0$", "", regex=True)       # float artefact 123.0
    s = s.str.replace(r"^(CUST|ID)[-_ ]?", "", regex=True)  # known prefix
    return s.str.lstrip("0")    # or .str.zfill(n); note "0" -> ""
```
- Decide padding direction from the data (strip zeros vs `zfill(width)`), not by habit.
- Ids embedded in text: `s.str.extract(r"#(\d{4,})", expand=False)`.
- Names: `casefold()`, collapse spaces `str.replace(r"\s+", " ", regex=True)`, strip punctuation; check homonyms before fuzzy matching. Avoid fuzzy matching unless exact matching provably fails.
- Check uniqueness of the key on the side that should be unique: `df.key.is_unique`, `df.key.duplicated().sum()`.

**4. Check match rates before trusting a join**
```python
m = a.merge(b, on="key", how="outer", indicator=True, validate="many_to_one")
print(m["_merge"].value_counts())
print(m.loc[m["_merge"] == "left_only", "key"].head(10).tolist())
```
- Low match rate = normalisation problem until proven otherwise. Inspect unmatched samples from both sides.
- Compare row counts before and after each join; growth means fan-out (duplicate keys).
- `validate="one_to_one"` / `"many_to_one"` raises on unexpected duplicates.

**5. Dedupe deliberately**
- Find exact duplicates (`df.duplicated().sum()`) and key duplicates separately. Choose the surviving row by a stated rule (latest timestamp, non-null fields) with `sort_values(...).drop_duplicates("key", keep="last")`.

**6. Dates and time zones**
- Parse with explicit formats per source: `pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")`; count resulting `NaT` and inspect the raw values that failed.
- Bring all timestamps to UTC (`utc=True`) before comparing; convert to the question's local zone only for day/month bucketing.
- Epoch columns: decide s vs ms by magnitude (`> 1e11` is ms).

**7. Free-text fields**
- Regex first; test the pattern on 20+ samples and print hits and misses before applying it to all rows.
- Report the extraction coverage (`notna().mean()`) and handle units/currencies/thousands separators explicitly.

**8. Provenance and reproducibility**
- Do the whole computation in ONE script that runs end to end from the sources; no hand-copied numbers.
- Carry a `source` column through concatenations; log each intermediate count (rows in, rows matched, rows filtered, reason).
- Print the final answer plus the components it was computed from (numerator, denominator, n).

**9. Sanity checks**
- Magnitude: does the total fit the row counts and typical values? Compare with an independent rough estimate.
- Recompute one or two entities by hand (direct per-source queries) and confirm they match the pipeline output.
- Units and scale: cents vs units, percent vs fraction, thousands.
- If assumptions were needed (ambiguous key, missing data), state them with their effect on the result.
