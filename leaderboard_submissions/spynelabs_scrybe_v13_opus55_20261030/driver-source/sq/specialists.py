"""Specialist tools for the worker: each is one focused, tool-less Opus call with its own prompt and output shape.

The worker used to plan, locate data, resolve ambiguity, join, label, compute and format inside one long session. These
calls split the judgement-heavy steps out, each with its own context:
  locate  Sage: given the question and the code-measured profile, which tables/columns/joins/definitions matter and
          which wording is ambiguous (reads the profile only, never data rows)
  verify  an independent reviewer: given the question, the declared answer shape, the candidate answer and the
          worker's account of how it was computed, is the answer complete, in shape and consistent with the method

The sandbox tools (sq/tool_sage.py, sq/tool_verify.py) post here through the host-side proxy (sq/jlabel_proxy.py).
Nothing here knows a dataset, a query or an expected answer.
"""
import json

SCHEMAS = {
    "locate": {
        "type": "object",
        "properties": {
            "relevant": {"type": "array", "items": {"type": "object", "properties": {
                "database": {"type": "string"}, "table": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
                "required": ["database", "table", "columns", "why"]}},
            "joins": {"type": "array", "items": {"type": "object", "properties": {
                "left": {"type": "string"}, "right": {"type": "string"}, "match": {"type": "string"},
                "caveat": {"type": "string"}}, "required": ["left", "right", "match", "caveat"]}},
            "definitions": {"type": "array", "items": {"type": "string"}},
            "ambiguities": {"type": "array", "items": {"type": "object", "properties": {
                "wording": {"type": "string"}, "readings": {"type": "array", "items": {"type": "string"}},
                "check_in_data": {"type": "string"}}, "required": ["wording", "readings", "check_in_data"]}},
            "pitfalls": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["relevant", "joins", "definitions", "ambiguities", "pitfalls"],
    },
    "judge": {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "label": {"type": "string"}}, "required": ["id", "label"]}}},
        "required": ["labels"],
    },
    "verify": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "fix"]},
            "strongest_alternative": {"type": "string"},
            "uncomputed_readings": {"type": "array", "items": {"type": "string"}},
            "unrequested_filters": {"type": "array", "items": {"type": "string"}},
            "altered_values": {"type": "array", "items": {"type": "string"}},
            "missing_values": {"type": "array", "items": {"type": "string"}},
            "shape_problems": {"type": "array", "items": {"type": "string"}},
            "method_concerns": {"type": "array", "items": {"type": "string"}},
            "suggested_answer_shape": {"type": "string"},
        },
        "required": ["verdict", "strongest_alternative", "uncomputed_readings", "unrequested_filters", "altered_values", "missing_values",
                     "shape_problems", "method_concerns", "suggested_answer_shape"],
    },
}

SYSTEM = {
    "locate": (
        "You are the data locator for an analyst answering a question from databases. You see the question and an "
        "offline, code-measured profile of the databases (tables, columns, null/distinct counts, ranges, top values, "
        "candidate keys, measured join match rates). You see no data rows beyond what the profile quotes.\n"
        "Return what the analyst needs to start: the tables and columns the question depends on (with the database "
        "each lives in and why), the joins that connect them (with the measured match rate, and any caveat such as a "
        "key written differently in the two sources, a low match rate, or a one-to-many fan-out), definitions that the "
        "data itself records for terms in the question (a column, a lookup table, a description), wording in the "
        "question that admits more than one reading (list the readings and say what to check in the data to decide), "
        "and pitfalls (duplicated rows, nulls in a filter column, values stored as text, free-text fields that hold "
        "the value). Quote names exactly as the profile spells them. Only claim what the profile supports; say "
        "'not in the profile' when it does not. Never state or guess the answer."),
    "judge": (
        "You label rows of data for a calibration check. You see the labelling question, the allowed labels (with "
        "descriptions when given) and a numbered list of rows. Label every row with exactly one allowed label, judging "
        "only from the row text and the question's wording. Read each row; do not infer from its position. Return one "
        "entry per row id. Never refuse a row: pick the closest allowed label."),
    "verify": "",
}


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "asks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "what": {"type": "string"}, "attribute_of": {"type": "string"}},
            "required": ["id", "what", "attribute_of"]}},
        "stated_filters": {"type": "array", "items": {"type": "string"}},
        "grain": {"type": "string"},
        "aggregation": {"type": "string"},
        "ordering": {"type": "string"},
        "readings": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
            "chosen": {"type": "string"}, "rule": {"type": "string"}, "check_in_data": {"type": "string"}},
            "required": ["term", "options", "chosen", "rule", "check_in_data"]}},
    },
    "required": ["asks", "stated_filters", "grain", "aggregation", "ordering", "readings"],
}
PRECEDENCE = (
    "Choose between readings with these rules, in this order:\n"
    "1. A definition the task itself gives (in the question or its hints) fixes that term's meaning everywhere the term "
    "appears, including filters and date windows. It outranks plain wording, column names and outside conventions. Never "
    "explain a task-given definition away as 'applying only to' something else; if it maps to one of the options, that "
    "option is chosen (or, when it cannot be decided without data, the data check must compute it).\n"
    "2. Otherwise take the most literal reading of the words.\n"
    "3. 'The X' with no condition means the whole named set: do not narrow it to a subset, and add no filter or "
    "exclusion the wording does not state (a restriction to rows that have some other record is a filter). A "
    "condition the question states for choosing or ranking items stays in force for every further figure asked about "
    "the chosen item, unless the question widens the set again.\n"
    "4. A format example or a length limit constrains the SHAPE of the answer, never which reading is chosen. Values are "
    "returned as stored; never alter a name or identifier to fit a separator. If an item can contain the separator, use "
    "a different separator or one item per line.\n"
    "5. A tie at a cutoff is resolved with a stated tie-break; never change a filter or a definition to remove a tie. "
    "Exception: when the question asks for one item at an extreme and one computed reading leaves a large share of the "
    "population tied there while another computed reading gives a single item, the question presupposes a unique "
    "answer: prefer the reading with a unique answer and state it.\n"
    "6. When the reading depends on a fact the data can show (which column carries a term, how values are stored), set "
    "chosen to 'decide from the data' and name that fact. When no fact in the data can decide it (a question of scope or "
    "wording), do not defer to the data: choose by rules 1 to 5.\n"
    "7. Every object of the main verb is a separate value to return ('identify X and Y' is two asks); never fold a second "
    "requested value into the description of the first.")
SYSTEM["verify"] = (
    "You are an independent, adversarial reviewer of a candidate answer to a data question. You did not produce it. "
    "You see the question, the declared answer shape, an interpretation brief written from the question's wording "
    "before any data was seen, the candidate answer, and the analyst's account of how it was computed. You cannot see "
    "the data.\n"
    "Work through these checks and name every problem specifically:\n"
    "1. strongest_alternative: the single strongest alternative reading of the question that would give a different "
    "answer, and whether the account rules it out with data or only assumes. Judge which reading is right with these "
    "rules:\n" + PRECEDENCE + "\n"
    "2. uncomputed_readings: every reading the brief lists (and any you named in item 1) that the account does not show "
    "a computed result for.\n"
    "3. unrequested_filters: every filter, exclusion or restriction in the account that neither the question nor the "
    "brief states (a reason such as 'not a valid X' or 'to keep only units with a record in table T' "
    "does not make it requested; a restriction that rule 5's exception supports is not unrequested). Only ever recommend REMOVING such a restriction. Never recommend adding a filter, "
    "exclusion or population restriction the question and brief do not state.\n"
    "4. altered_values: any value in the answer that differs from the stored form shown in the account (a name or "
    "identifier shortened, re-cased or reformatted to fit a separator or a length).\n"
    "5. missing_values: every value the question or the brief asks to be returned that is absent from the answer.\n"
    "6. shape_problems: types, separators, decimals, ordering, extra text.\n"
    "7. method_concerns: grain, aggregation, ties, nulls, keys written differently, counts that do not add up, a join or "
    "filter whose surviving share of the declared population is not reported or is very small (under about 20%), a "
    "stated filter that removes nothing.\n"
    "Say verdict fix when any of items 2 to 6 has an entry, or when the chosen reading conflicts with the brief or the "
    "rules above and the account gives no data showing that the brief's reading is impossible. A fix on items 3, 4 and 5 "
    "is never to be argued away by the analyst. Otherwise say ok. Never state or guess the true answer, and never put "
    "candidate values in suggested_answer_shape: describe the shape only. Judge the evidence given, not whether the "
    "number looks right.")
BRIEF_SYSTEM = (
    "You prepare an interpretation brief for an analyst who will answer a data question. You see only the question "
    "(with any hints) and the declared answer shape; you see no data. Return: every distinct value the question asks to be "
    "returned as an ask (ids A1, A2, ... in the order asked; an entity and a requested attribute of it are two asks; "
    "'attribute_of' names the entity or ''); the filters and conditions the wording states; the grain of a row, the "
    "aggregation level and the ordering or ranking if stated ('' if not); and for each term or phrase that admits more "
    "than one reading that would change the answer, the options, the reading you choose by the rules below, the rule "
    "number you applied, and what to check in the data. Only list real ambiguities: at most four, only those whose choice would plausibly change the answer (do not list case-sensitivity, null handling or ties unless the wording raises them). Add nothing the question does not "
    "say. Never write format rules such as 'exactly N comma-separated items'. Never guess the answer.\n\n" + PRECEDENCE)
BRIEF_MERGE_SYSTEM = (
    "You merge several independently written interpretation briefs for the same question into one. Keep every ask that "
    "any brief names unless it duplicates another ask; renumber A1, A2, ... in the order asked. Keep the stated "
    "filters that appear in at least two briefs or follow plainly from the question. For each ambiguous term keep the "
    "option set (union) and choose the reading by the majority of the briefs; where there is no majority apply these "
    "rules.\n\n" + PRECEDENCE + "\nAdd nothing the question does not say.")


def make_brief(question: str, shape: str, call, samples: int = 3) -> tuple:
    """Host side, before the worker starts: `samples` independent briefs from the question alone, merged into one.
    Returns (rendered brief, cost, merged obj)."""
    from concurrent.futures import ThreadPoolExecutor
    user = f"## Question\n\n{question}\n\n## Declared answer shape\n\n{shape or '(not declared)'}\n"
    with ThreadPoolExecutor(samples) as ex:
        got = list(ex.map(lambda _: call(BRIEF_SYSTEM, user, BRIEF_SCHEMA, budget_usd=0.35), range(samples)))
    cost = sum(float(c or 0) for _, c in got)
    briefs = [o for o, _ in got]
    merged, c2 = call(BRIEF_MERGE_SYSTEM, user + "\n## Briefs\n\n" + json.dumps(briefs, ensure_ascii=False),
                      BRIEF_SCHEMA, budget_usd=0.35)
    return render_brief(merged), cost + float(c2 or 0), merged


def render_brief(obj: dict) -> str:
    out = ["VALUES TO RETURN (all of them, in this order)"]
    out += [f"- {a['id']}: {a['what']}" + (f" (of {a['attribute_of']})" if a.get("attribute_of") else "") for a in obj.get("asks", [])]
    if obj.get("stated_filters"):
        out += ["FILTERS AND CONDITIONS THE QUESTION STATES (apply no others)"] + [f"- {x}" for x in obj["stated_filters"]]
    for key, title in (("grain", "GRAIN OF A ROW"), ("aggregation", "AGGREGATION"), ("ordering", "ORDERING")):
        if obj.get(key):
            out.append(f"{title}: {obj[key]}")
    rd = obj.get("readings") or []
    if rd:
        out.append("READINGS DECIDED FROM THE WORDING (fixed rules; the data may overrule only by showing a reading impossible)")
        for r in rd:
            out.append(f"- '{r['term']}': options: " + " | ".join(r["options"]) + f" -> {r['chosen']}"
                       + (f" [rule {r['rule']}]" if r.get("rule") else "") + (f"; check: {r['check_in_data']}" if r.get("check_in_data") else ""))
    return "\n".join(out)


def render(tool: str, obj: dict) -> str:
    out = []
    if tool == "locate":
        for r in obj.get("relevant", []):
            out.append(f"- {r['database']}.{r['table']} [{', '.join(r['columns'])}]: {r['why']}")
        if obj.get("joins"):
            out.append("JOINS")
            out += [f"- {j['left']} = {j['right']}: {j['match']}" + (f" (caveat: {j['caveat']})" if j.get("caveat") else "")
                    for j in obj["joins"]]
        if obj.get("definitions"):
            out.append("DEFINITIONS RECORDED IN THE DATA")
            out += [f"- {d}" for d in obj["definitions"]]
        if obj.get("ambiguities"):
            out.append("AMBIGUOUS WORDING (decide from the data, do not pick by convention)")
            out += [f"- '{a['wording']}': " + " | ".join(a["readings"]) + f" -> check: {a['check_in_data']}"
                    for a in obj["ambiguities"]]
        if obj.get("pitfalls"):
            out.append("PITFALLS")
            out += [f"- {p}" for p in obj["pitfalls"]]
        return "RELEVANT\n" + "\n".join(out)
    if tool == "judge":
        return json.dumps(obj.get("labels", []), ensure_ascii=False)
    parts = [f"VERDICT: {obj.get('verdict')}"]
    if obj.get("strongest_alternative"):
        parts.append("STRONGEST ALTERNATIVE READING: " + obj["strongest_alternative"])
    for key, title in (("uncomputed_readings", "READINGS WITH NO COMPUTED RESULT"), ("unrequested_filters", "FILTERS NOT ASKED FOR"), ("altered_values", "VALUES ALTERED FROM STORED FORM"),
                       ("missing_values", "MISSING VALUES"), ("shape_problems", "SHAPE PROBLEMS"),
                       ("method_concerns", "METHOD CONCERNS")):
        if obj.get(key):
            parts.append(title + "\n" + "\n".join(f"- {x}" for x in obj[key]))
    if obj.get("suggested_answer_shape"):
        parts.append("SUGGESTED SHAPE: " + obj["suggested_answer_shape"])
    return "\n".join(parts)


def user_message(tool: str, req: dict) -> str:
    if tool == "judge":
        return (f"## Labelling question\n\n{req['focus']}\n\n## Allowed labels\n\n{req['shape']}\n\n"
                f"## Rows (JSON list of [id, row])\n\n{req['profile']}\n")
    if tool == "locate":
        return (f"## Question\n\n{req['question']}\n\n## What the analyst wants to find out\n\n{req.get('focus') or '(the whole question)'}"
                f"\n\n## Profile\n\n{req['profile']}\n")
    return (f"## Question\n\n{req['question']}\n\n## Required answer shape\n\n{req.get('shape') or '(not declared)'}\n\n"
            f"## Interpretation brief (written from the question's wording, before any data)\n\n{req.get('contract') or '(none)'}\n\n"
            f"## Candidate answer\n\n{req['answer']}\n\n## How it was computed\n\n{req.get('how') or '(not given)'}\n")


def ask(tool: str, req: dict, call) -> tuple:
    """Run one specialist: call(system, user, schema, budget_usd=..) -> (obj, cost). Returns (text, cost, raw obj)."""
    if tool not in SCHEMAS:
        raise ValueError(f"unknown specialist {tool!r}")
    obj, cost = call(SYSTEM[tool], user_message(tool, req), SCHEMAS[tool], budget_usd=0.40)
    return render(tool, obj), cost, obj


def limits(tool: str) -> int:
    return {"locate": 4, "verify": 3, "judge": 8}[tool]


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
