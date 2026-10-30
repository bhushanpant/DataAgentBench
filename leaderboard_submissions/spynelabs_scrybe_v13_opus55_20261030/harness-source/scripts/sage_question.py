"""Question-driven retrieval of environment evidence, never expected answers.

Literal identifiers retain their provenance. Lexical candidates are labelled as hypotheses,
not resolved sources. No model calls, benchmark IDs, SQL generation or evaluator access.
"""
import re
from collections import Counter

import sage_render as R


def column_order(columns, task):
    """Stable task-identifier-first order; preserve exact live column spelling."""
    terms = R.task_idents(task)
    return sorted(columns, key=lambda c: (R._hit(terms, c[0]) is None,
                                         R._hit(terms, c[0]) or 0))


def make_focus(layer, off, task, max_assets=24, upstream_depth=2):
    """Named sources and bounded upstream lineage; omit unrelated downstream consumers.

    When the task names fewer than two existing assets, surface at most four discovery
    candidates sharing two literal column names, or one column plus a lexical table-name
    match. These never become named assets.
    """
    original = R.select_focus(layer, off, task, 100000)
    named = [it for it in original['items'] if it['why'] == 'named']
    items = list(named)
    seen = {it['id'] for it in items}
    frontier = list(named)
    for depth in range(max(0, min(upstream_depth, 4))):
        nxt = []
        for it in frontier:
            if it['type'] != 'node' or it.get('other_backend'):
                continue
            for dep in R.direct_deps(layer, it['root'], it['kind'], it['node']):
                if dep.pop('rel_kind') != 'upstream' or dep['id'] in seen:
                    continue
                seen.add(dep['id'])
                dep.update(why=f"upstream {depth + 1} of {it['name']}", other_backend=False)
                nxt.append(dep)
        items.extend(nxt)
        frontier = nxt
    items = sorted(R.fold(layer, items), key=R._tier)
    if len([it for it in named if not it.get('other_backend')]) < 2:
        terms = R.task_idents(task)
        # Explicit exploration scope is a preference, not permission to resolve a different source.
        schemas = {m.group(1).lower() for m in re.finditer(
            r'\bexplore\s+(?:the\s+)?[`\"]?([\w]+)[`\"]?\s+schema\b', task, re.I)}
        word_counts = Counter(w.rstrip('s') for w in re.findall(r'\b[a-zA-Z]{4,}\b', task.lower()))
        candidates = []
        for rel in (layer.get('warehouse') or {}).get('relations', []):
            cols = catalog_columns(layer, rel)
            hits = [c for c, _ in cols if R._hit(terms, c) is not None]
            name_words = set(re.findall(r'[a-zA-Z]{2,}', rel[1].lower()))
            concepts = {w.rstrip('s') for w in name_words} & set(word_counts)
            if len(hits) < 2 and not (hits and concepts):
                continue
            it = {'type': 'relation', 'name': R.rkey(layer, rel), 'rel': rel,
                  'id': ('rel', R.rkey(layer, rel).lower()),
                  'why': 'candidate from literal column matches: ' + ', '.join(hits),
                  'other_backend': False}
            if it['id'] not in seen:
                candidates.append((rel[0].lower() in schemas, concepts, len(hits), len(name_words), it))
        candidates.sort(key=lambda p: (-p[0], -bool(p[1]), p[3],
                                       -sum(word_counts[w] for w in p[1]), -p[2], p[4]['name']))
        # One exemplar per lexical concept/signature before near-duplicate variants. They remain
        # hypotheses; the agent must use catalog/values/joins to choose among alternatives.
        signatures, diverse, repeats = set(), [], []
        for _, concepts, _, _, it in candidates:
            signature = tuple(sorted(concepts)) or tuple(c.lower() for c, _ in catalog_columns(layer, it['rel'])
                                                       if R._hit(terms, c) is not None)
            if signature in signatures:
                repeats.append(it)
            else:
                signatures.add(signature)
                diverse.append(it)
        # Prefer distinct physical objects; folding also excludes duplicates of named/upstream items.
        folded = R.fold(layer, items + diverse + repeats)
        items += [it for it in folded if it['why'].startswith('candidate')][:4]
    omitted = [[it['name'], 'asset limit'] for it in items[max_assets:]]
    return {'items': items[:max_assets], 'named': original['named'],
            'dependencies': [it['name'] for it in items if it['why'].startswith('upstream')],
            'candidates': [it['name'] for it in items if it['why'].startswith('candidate')],
            'folded': {it['name']: it['also'] for it in items if it.get('also')},
            'omitted': omitted, 'max_assets': max_assets, 'question_driven': True,
            'task_text': task, 'upstream_depth': upstream_depth,
            'downstream_excluded': sum(it['why'].startswith('downstream') for it in original['items'])}


def catalog_columns(layer, rel):
    cat = (layer.get('warehouse') or {}).get('columns') or {}
    if R.rkey(layer, rel) in cat:
        return cat[R.rkey(layer, rel)]
    # Compatibility with old layers: a partial profile is not a complete catalog.
    return [[c['name'], c.get('type', '')] for c in (R.profile_of(layer, rel) or {}).get('columns', [])]


def evidence(layer, focus):
    terms = R.task_idents(focus.get('task_text', ''))
    out = {'method': 'literal task identifiers matched to live catalog columns; no semantic completeness claim',
           'assets': [], 'omitted_assets': focus.get('omitted', [])}
    for it in focus.get('items', []):
        if it.get('other_backend'):
            continue
        rel = (it.get('node') or {}).get('relation') if it['type'] == 'node' else it.get('rel')
        if not rel:
            continue
        prof = R.profile_of(layer, rel) or {}
        stats = {c['name']: c for c in prof.get('columns', [])}
        cols = catalog_columns(layer, rel)
        matched = []
        for name, dtype in column_order(cols, focus.get('task_text', '')):
            if R._hit(terms, name) is not None:
                matched.append({'name': name, 'type': dtype, 'statistics': stats.get(name)})
        out['assets'].append({'name': it['name'], 'relation': R.rkey(layer, rel), 'selection': it['why'],
                              'catalog_complete': R.rkey(layer, rel) in (layer.get('warehouse', {}).get('columns') or {}),
                              'columns_total': len(cols), 'rows': prof.get('rows'),
                              'distinct_exact': prof.get('distinct_exact'), 'profile_error': prof.get('error'),
                              'matched_columns': matched})
    return out


def render_evidence(ev, budget=None):
    lines = ['## Question-driven data evidence',
             'Literal column matches below are evidence to inspect, not a complete interpretation of the question. '
             'Candidate sources need confirmation. Profiles describe startup data; refresh after rebuilding. '
             'Missing statistics mean unmeasured, not absent. Min/max dates do not establish coverage within a requested window.']
    entries = []
    for asset in ev['assets']:
        entries.append(f"- {asset['relation']} [{asset['selection']}]: rows={R.n_(asset['rows'])}; "
                       f"catalog columns={asset['columns_total']} ({'complete' if asset['catalog_complete'] else 'partial'})")
        for col in asset['matched_columns']:
            st = col['statistics']
            entries.append(f"  {col['name']} {col['type']}: " +
                           (R.col_stats(st, asset.get('distinct_exact')) if st else 'not measured'))
    omitted = 0
    for i, line in enumerate(entries):
        if budget is not None and len('\n'.join(lines + [line])) > max(0, budget - 150):
            omitted = len(entries) - i
            break
        lines.append(line)
    if omitted:
        lines.append(f'[{omitted} evidence lines omitted; `sl task` shows all recorded evidence]')
    if ev.get('omitted_assets'):
        lines.append(f"[{len(ev['omitted_assets'])} assets excluded by focus cap; `sl search <name>`]")
    text = '\n'.join(lines)
    if budget is not None and len(text) > budget:
        return '[question evidence omitted by context budget; `sl task`]'[:budget]
    return text
