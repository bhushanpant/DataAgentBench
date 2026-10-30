"""Answer lint: deterministic checks of a candidate answer against the declared shape and the requirement contract.

No model, no data. Returns a list of short problem strings; empty means nothing found. Used by the sandbox `verify`
tool (before the review call) and by the host for per-trial signals.
"""
import re


def _decimals(fmt: str):
    m = re.search(r"(\d+|one|two|three|four|five|six)\s+decimal", fmt, re.I)
    if not m:
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    return int(m.group(1)) if m.group(1).isdigit() else words[m.group(1).lower()]


def lint(answer: str, fmt: str = "", asks: int = 0, how: str = "") -> list:
    a = (answer or "").strip()
    out = []
    if not a:
        return ["answer is empty"]
    if re.search(r"\*\*|^#{1,4} |```|^\s*[-*] ", a, re.M):
        out.append("markdown in the answer")
    if re.match(r"(?i)\s*(final )?(answer|result)\s*[:=-]", a):
        out.append("answer starts with a label such as 'Answer:'")
    words = len(a.split())
    if words > 60 and "\n" not in a and "," not in a:
        out.append(f"answer is {words} words of prose")
    if re.search(r"(?i)\b(because|therefore|based on|i found|note that)\b", a):
        out.append("explanation words in the answer")
    d = _decimals(fmt or "")
    if d is not None:
        for n in re.findall(r"(?<![\w.])-?\d[\d,]*\.\d+", a):
            if len(n.split(".")[1]) != d:
                out.append(f"{n} does not have {d} decimals")
                break
    if re.search(r"\d,\d{3}\b", a) and not re.search(r"(?i)thousand|comma", fmt or ""):
        out.append("thousands separators in the answer")
    if asks > 1:
        parts = [p for p in re.split(r"[;\n|]+|,\s+(?=[A-Za-z0-9])", a) if p.strip()]
        if len(parts) < asks:
            out.append(f"the contract has {asks} values to return, the answer shows {len(parts)} item(s)")
    if re.search(r"\.\.\.|\u2026|\betc\b", a):
        out.append("ellipsis or 'etc' in the answer: list every value")
    out += _shortened(a, how or "")
    return out


def _items(how: str) -> set:
    return {x.strip() for x in re.split(r"[\n|;\t\"']|\s{2,}", how) if x.strip()}


def _shortened(answer: str, how: str) -> list:
    """A value in the answer that is a strict prefix of a stored value shown in the account: a name or identifier cut to fit
    a separator or a length."""
    if not how:
        return []
    items = _items(how)
    out = []
    for p in {x.strip() for x in re.split(r"[;\n|]+", answer) if x.strip()}:
        if len(p) >= 3 and p not in items:
            longer = [i for i in items if len(i) > len(p) and i.startswith(p) and not i[len(p):].lstrip().startswith(',')]
            if longer:
                out.append(f"'{p}' looks shortened: your account shows '{longer[0]}'; return values as stored")
    suffix = r",?\s+(?:Inc|Corp|Corporation|Ltd|Limited|LLC|LLP|Co|Company|PLC|AG|SA|NV|Jr|Sr)\b\.?"
    for h in {x.strip() for x in re.split(r"[;\n|]+|,\s+", answer) if len(x.strip()) >= 3}:
        m = re.search(re.escape(h) + suffix, how)
        if m and m.group(0) not in answer:
            out.append(f"'{h}' is stored as '{m.group(0).strip()}': return the stored value, not a shortened one")
    return list(dict.fromkeys(out))[:6]
