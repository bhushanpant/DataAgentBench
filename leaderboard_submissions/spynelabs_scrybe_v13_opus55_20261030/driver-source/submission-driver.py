#!/usr/bin/env python3
"""Freeze and run DAB Solo V5 using an explicitly selected harness checkout.

No scores are used for scheduling or selection. Existing completed trials are never
replaced. Model/tool transcripts are collected before sandbox cleanup.
Prepare with --harness-source PATH to use a patched checkout, --trials N to choose
trials per query, and --dry-run to inspect preparation without writing or running.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse

ROOT = Path(os.environ.get('SCRYBE_SUBMISSION_ROOT', Path(__file__).resolve().parent.parent))
SQ = ROOT.parent / 'scrybe-sq'
DEFAULT = ROOT / 'runs' / 'dab-solo-v5-submission-20261025'
SOURCE_SERVER = ROOT.parent / 'multica/.claude/worktrees/quorum-sizes/server'
GATES = {'q_label': False, 'repeat_trigger': 'red_flag', 'threshold': 0.4, 'single_attempt': True, 'audit': False, 'tools': True, 'contract': True, 'stage_gate': False}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')
    tmp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def configure(base):
    runtime = base / 'runtime'
    sys.path.insert(0, str(runtime / 'scripts'))
    # SCRYBE_PROVIDER=anthropic: direct Anthropic (needs ANTHROPIC_API_KEY); default stays OpenRouter.
    mode = os.environ.get('SCRYBE_PROVIDER', 'openrouter')
    if mode == 'runtime':  # Claude Code login (OAuth) for `claude -p`; no API key in the environment
        os.environ.pop('SQ_PROVIDER', None)
        for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL'):
            os.environ.pop(k, None)
    elif mode == 'anthropic':
        os.environ.pop('SQ_PROVIDER', None)
        if not os.environ.get('ANTHROPIC_API_KEY'):
            for line in (ROOT / '.env').read_text().splitlines():
                if line.startswith('ANTHROPIC_API_KEY='):
                    os.environ['ANTHROPIC_API_KEY'] = line.split('=', 1)[1].strip().strip('\'"')
    else:
        os.environ['SQ_PROVIDER'] = 'openrouter'
    os.environ.update(MULTICA_API='http://localhost:8081',
                      QUORUM_JWT_FILE=str(ROOT / 'runs/quorum-dab/.jwt'),
                      DAB_ROOT=str(ROOT / 'vendor/DataAgentBench'),
                      DAB_DB_ACCOUNTS='per-dataset', SCRYBE_SEMANTIC='0')
    return runtime


SERVER_FILES = ['internal/ship/quorum_prompt.go', 'internal/ship/quorum_api.go',
                'internal/ship/quorum_run.go', 'internal/quorum/charter.go']


def instrument_frontdoor(source):
    """Keep existing evidence hooks exactly once; instrument older harnesses."""
    hooks = ('submission_evidence.opus_request(', 'submission_evidence.opus_response(')
    counts = [source.count(hook) for hook in hooks]
    if counts == [1, 1] and 'from sq import submission_evidence' in source:
        return source
    if any(counts):
        raise RuntimeError('Incomplete or duplicate evidence hooks in harness frontdoor.py')
    anchor = 'from sq import provider\n'
    before = '        with tempfile.TemporaryDirectory(prefix="sq-intake-") as cwd:\n'
    after = '                               env=provider.claude_env())\n'
    if any(source.count(text) != 1 for text in (anchor, before, after)):
        raise RuntimeError('Unrecognized frontdoor.py; cannot safely install evidence hooks')
    source = source.replace(anchor, anchor + 'from sq import submission_evidence\n', 1)
    source = source.replace(before, '        submission_evidence.opus_request(system, user, schema, budget_usd)\n' + before, 1)
    return source.replace(after, after + '        submission_evidence.opus_response(r)\n', 1)


def preparation_plan(base, harness_source, trials):
    if trials < 1:
        raise ValueError('--trials must be at least 1')
    if base.exists():
        raise RuntimeError(f'Refusing to overwrite {base}')
    required = [harness_source / 'scripts/sq' / name for name in
                ('frontdoor.py', 'task.py', 'quorum.py', 'agree.py', 'provider.py')]
    required += [harness_source / 'scripts/dab_common.py', ROOT / 'vendor/DataAgentBench',
                 ROOT / 'runs/bundles/7409ad29afc6d9c98ffa/install', ROOT / 'runs/quorum-dab/site',
                 ROOT / 'runs/dab-schemas', Path.home() / '.multica/bin/spyne-server-qs']
    required += [SOURCE_SERVER / name for name in SERVER_FILES]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError('Missing preparation inputs:\n' + '\n'.join(missing))
    # Validate instrumentation before creating a partially prepared directory.
    instrument_frontdoor((harness_source / 'scripts/sq/frontdoor.py').read_text())
    return {'base': str(base), 'harness_source': str(harness_source),
            'asset_root': str(ROOT), 'trials_per_query': trials,
            'evidence_source': ('harness' if (harness_source / 'scripts/sq/submission_evidence.py').is_file()
                                else 'legacy embedded capture'),
            'starts_evaluation': False}


def prepare(base, harness_source=None, trials=5, dry_run=False):
    harness_source = Path(harness_source or SQ).expanduser().resolve()
    plan = preparation_plan(base, harness_source, trials)
    if dry_run:
        print(json.dumps(dict(plan, dry_run=True), indent=2))
        return
    runtime = base / 'runtime'
    (runtime / 'scripts').mkdir(parents=True)
    shutil.copytree(harness_source / 'scripts/sq', runtime / 'scripts/sq', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(harness_source / 'scripts/dab_common.py', runtime / 'scripts/dab_common.py')
    shutil.copy2(__file__, runtime / 'scripts/submission-driver.py')
    monitor = Path(__file__).with_name('dab-solo-monitor.py')
    if monitor.is_file():
        shutil.copy2(monitor, runtime / 'scripts/dab-solo-monitor.py')
    shutil.copytree(ROOT / 'runs/bundles/7409ad29afc6d9c98ffa/install',
                    runtime / 'runs/bundles/7409ad29afc6d9c98ffa/install')
    (runtime / 'runs/quorum-dab').mkdir(parents=True)
    # stage() archives this path without dereferencing symlinks. It must be a
    # real directory or the sandbox receives a dangling host-only link.
    shutil.copytree(ROOT / 'runs/quorum-dab/site', runtime / 'runs/quorum-dab/site')
    shutil.copytree(ROOT / 'runs/dab-schemas', runtime / 'runs/dab-schemas')
    # Preserve the chosen harness's evidence fixes and avoid double instrumentation.
    fp = runtime / 'scripts/sq/frontdoor.py'
    fp.write_text(instrument_frontdoor(fp.read_text()))
    evidence = runtime / 'scripts/sq/submission_evidence.py'
    if not evidence.exists():
        evidence.write_text(EVIDENCE_SOURCE)
    configure(base)
    import dab_common as dc
    tasks = dc.task_ids('scored')
    assert len(tasks) == 54 and len(set(t.split('/')[0] for t in tasks)) == 12
    inputs = base / 'benchmark-inputs'
    for dataset in dc.SCORED:
        d = dc.dataset_dir(dataset)
        for name in ['db_description.txt', 'db_description_withhint.txt', 'db_config.yaml']:
            target = inputs / dataset / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / name, target)
        for q in dc.queries(dataset):
            target = inputs / dataset / q / 'query.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / q / 'query.json', target)
    # Exact server prompt/tool implementation is included as disclosure evidence.
    for name in SERVER_FILES:
        target = base / 'server-source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_SERVER / name, target)
    commit = subprocess.check_output(['git', '-C', str(harness_source), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', '-C', str(harness_source), 'status', '--porcelain'], text=True).strip())
    benchmark_commit = subprocess.check_output(['git', '-C', str(dc.DAB_ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    manifest = {'created_at': now(), 'team': 'SpyneLabs', 'agent': 'Scrybe V1', 'lineage': 'solo-v5',
                'provider': 'OpenRouter', 'models': {'worker': 'anthropic/claude-sonnet-5.5',
                'intake_and_selection': 'anthropic/claude-opus-5.5'}, 'hints': True,
                'prompt_category': 'benchmark-informed', 'source_commit': commit,
                'source_root': str(harness_source), 'source_dirty': dirty,
                'benchmark_commit': benchmark_commit, 'trials_per_query': trials, 'tasks': tasks,
                'gates': GATES, 'nominal_trial_budget_usd': 1.0, 'attempt_wall_seconds': 1800,
                'schedule': [{'task': t, 'run': r} for r in range(trials) for t in tasks],
                'scoring': 'Deferred until all generation is complete; no score-based stopping or reruns.',
                'internal_retries': 'Existing solo-v5 format-gate/red-flag repeats (up to two extras), bounded infrastructure retries, and Opus selection; every internal attempt retained.',
                'trace_changes': 'Record tool-less Opus request/raw response and worker message/tool transcripts before cleanup; no prompt changes.',
                'server_binary_sha256': digest(Path.home() / '.multica/bin/spyne-server-qs')}
    files = list((runtime / 'scripts').rglob('*.py')) + [p for p in inputs.rglob('*') if p.is_file()]
    files += [p for p in (base / 'server-source').rglob('*') if p.is_file()]
    files += [p for p in (runtime / 'runs/bundles').rglob('*') if p.is_file()]
    manifest['sha256'] = {str(p.relative_to(base)): digest(p) for p in sorted(files)}
    write(base / 'manifest.json', manifest)
    print(json.dumps({'prepared': str(base), 'trials': len(manifest['schedule']), 'models': manifest['models']}))


EVIDENCE_SOURCE = r'''
"""Host-only evidence capture. Never adds evidence or scores to model context."""
import json, os, re, time, threading, urllib.parse
from pathlib import Path

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, default=str))
    tmp.replace(path)

def opus_request(system,user,schema,budget):
    p=Path(os.environ['SQ_EVIDENCE_DIR'])/'opus.jsonl'
    with p.open('a') as f:
        f.write(json.dumps({'type':'request','time':time.time(),'model':'anthropic/claude-opus-5.5',
            'provider':'openrouter','endpoint':'https://openrouter.ai/api','system':system,'user':user,
            'schema':schema,'max_budget_usd':budget},ensure_ascii=False)+'\n')

def opus_response(r):
    p=Path(os.environ['SQ_EVIDENCE_DIR'])/'opus.jsonl'
    with p.open('a') as f:
        f.write(json.dumps({'type':'response','time':time.time(),'returncode':r.returncode,
            'stdout':r.stdout,'stderr':r.stderr},ensure_ascii=False)+'\n')

def install(q):
    raw=q.api
    records={}
    guard=threading.RLock()
    def capture(rid, final=False):
        rec=records[rid];out=rec['out']
        try:
            v=raw('GET',f'/api/ship/runs/{rid}/quorum',timeout=30)
            save(out/'quorum-view.json',v)
            ide=v.get('ide_id') or rec.get('ide')
            rec['ide']=ide
            if final:
                save(out/'kernel-trace.json',raw('GET',f'/api/ship/runs/{rid}/quorum/trace',timeout=30))
            # Sessions are discovered from the run's assignment IDs, never other tasks.
            for a in v.get('assignments') or []:
                aid=a['assignment_id']
                sd=raw('GET',f'/api/ship/runs/{rid}/quorum/assignments/{aid}/steps',timeout=30)
                sid=sd.get('session_id') or rec.get('sessions',{}).get(aid)
                if not sid or not ide: continue
                rec.setdefault('sessions',{})[aid]=sid
                target=out/('session-'+sid+'.json')
                old=json.loads(target.read_text()) if target.exists() else {'messages':[]}
                messages={m['id']:m for m in old['messages']}
                cursor=None;seen=set()
                for page in range(100):
                    suffix='order=asc&limit=200'
                    if cursor:suffix+='&cursor='+urllib.parse.quote(cursor,safe='')
                    d=raw('GET',f'/api/ide/w/{ide}/oc/api/session/{sid}/message?'+suffix,timeout=30)
                    batch=d.get('data') or []
                    messages.update({m['id']:m for m in batch})
                    next_cursor=(d.get('cursor') or {}).get('next')
                    if not batch or len(batch)<200 or not next_cursor or next_cursor in seen:break
                    seen.add(next_cursor);cursor=next_cursor
                else:raise RuntimeError('Message pagination did not terminate')
                save(target,{'assignment_id':aid,'session_id':sid,'captured_at':time.time(),
                    'final_capture':final,'messages':sorted(messages.values(),key=lambda m:m.get('time',{}).get('created',0))})
            save(out/'capture-status.json',{'run_id':rid,'ide_id':ide,'final_capture':final,
                'sessions':rec.get('sessions',{}),'time':time.time()})
        except Exception as e:
            with (out/'capture-errors.jsonl').open('a') as f:
                f.write(json.dumps({'time':time.time(),'final':final,'error':str(e)})+'\n')
    def wrapped(method,path,body=None,timeout=60):
        if method=='POST' and path=='/api/ship/jev':
            p=Path(os.environ['SQ_EVIDENCE_DIR'])/'jev.jsonl'
            with p.open('a') as f:
                f.write(json.dumps({'type':'request','time':time.time(),'endpoint':path,'body':body})+'\n')
        if method=='DELETE' and path.startswith('/api/ide/workspaces/'):
            ide=path.rsplit('/',1)[1]
            for rid,rec in list(records.items()):
                if rec.get('ide')==ide:capture(rid,True)
        if method=='POST' and path.endswith('/stop'):
            rid=path.split('/')[-2]
            if rid in records:capture(rid,True)
        d=raw(method,path,body,timeout)
        if method=='POST' and path=='/api/ship/jev':
            with (Path(os.environ['SQ_EVIDENCE_DIR'])/'jev.jsonl').open('a') as f:
                f.write(json.dumps({'type':'response','time':time.time(),'body':d})+'\n')
        if method=='POST' and path=='/api/ship/objectives':
            rid=d['run_id'];out=Path(os.environ['SQ_EVIDENCE_DIR'])/'workers'/rid
            out.mkdir(parents=True,exist_ok=True);save(out/'request.json',body)
            with guard:records[rid]={'out':out,'last':0}
        m=re.fullmatch(r'/api/ship/runs/([0-9a-f-]{36})(/quorum)?',path)
        if method=='GET' and m and m.group(1) in records:
            rid=m.group(1);rec=records[rid]
            if time.time()-rec['last']>=15 or m.group(2):
                rec['last']=time.time();capture(rid,bool(m.group(2)))
        return d
    q.api=wrapped
    return records
'''


def verify_frozen(base):
    m = json.loads((base / 'manifest.json').read_text())
    bad = [p for p, sha in m['sha256'].items() if digest(base / p) != sha]
    if bad:
        raise RuntimeError('Frozen source changed: ' + ', '.join(bad[:5]))
    for name, sha in m['sha256'].items():
        if name.startswith('benchmark-inputs/'):
            parts=Path(name).parts
            live=ROOT/'vendor/DataAgentBench'/('query_'+parts[1])/Path(*parts[2:])
            if digest(live)!=sha:raise RuntimeError('Benchmark input changed: '+str(live))
    return m


def trial_dir(base, tid, run):
    return base / 'runtime/runs' / f'submission-r{run}' / tid.replace('/', '__')


def settle_labels(tdir, result, hashes):
    """Revoke the trial's label tokens, keep their request/response log with the traces, and count their cost."""
    from sq import jlabel_proxy
    calls = items = billing = 0
    cost = 0.0
    with (tdir / 'jlabel.jsonl').open('w') as out:
        for h in hashes:
            s = jlabel_proxy.finish(h, ROOT_RUNTIME_LABELS(tdir))
            calls, items, cost = calls + s['calls'], items + s['items'], cost + s['cost_usd']
            billing += s.get('billing_errors', 0)
            log = ROOT_RUNTIME_LABELS(tdir) / f'{h}.jsonl'
            if log.exists():
                out.write(log.read_text())
    if not calls:
        (tdir / 'jlabel.jsonl').unlink()
        return
    result['jlabel'] = {'calls': calls, 'items': items, 'cost_usd': cost, 'billing_errors': billing}
    c = result.setdefault('cost', {})
    c['jev'] = (c.get('jev') or 0.0) + cost
    if c.get('total') is not None:
        c['total'] += cost


def ROOT_RUNTIME_LABELS(tdir):
    return Path(tdir).parents[1] / 'jlabel'  # runtime/runs/jlabel; trial dirs are runtime/runs/submission-rN/<task>


def worker(base, tid, run):
    verify_frozen(base)
    runtime = configure(base)
    from sq import task, quorum, frontdoor, agree, submission_evidence
    import dab_common as dc
    tdir = trial_dir(base, tid, run)
    tdir.mkdir(parents=True, exist_ok=True)
    if (tdir / 'submission-result.json').exists():
        return
    if (tdir / 'started.json').exists():
        raise RuntimeError('Trial already started; refusing to replace or rerun it')
    write(tdir / 'started.json', {'task': tid, 'run': run, 'started_at': now(), 'pid': os.getpid()})
    os.environ['SQ_EVIDENCE_DIR'] = str(tdir)
    submission_evidence.install(quorum)
    original_build = quorum.build_kit
    def build(ds, query, out):
        kit, server = original_build(ds, query, out)
        for f in ['TASK.md','SCHEMA.md']:
            if (kit / f).exists(): shutil.copy2(kit / f, tdir / f)
        shutil.copy2(kit / 'sage/layer.json', tdir / 'sage-profile.json')
        if (kit / '.jlabel_hash').exists():
            jlabel_hashes.append((kit / '.jlabel_hash').read_text().strip())
        return kit, server
    jlabel_hashes = []
    deps = task.Deps(build_kit=build, score=lambda ds,q,answer: {'reward':None,'reason':'Deferred for submission'})
    try:
        result = task.run_task('solo', tid, runtime / 'runs' / f'submission-r{run}',
                               deps=deps, gates=GATES, wall_s=1800)
        result.update(submission_run=run, finished_at=now())
    except Exception as e:
        known_cost = getattr(e, 'cost_usd', None)
        result = {'task':tid,'submission_run':run,'answer':'','reward':None,
                  'error':f'{type(e).__name__}: {e}','finished_at':now(),
                  'cost':{'total':known_cost,'complete':known_cost is not None}}
        write(tdir / 'failure.json', result)
    settle_labels(tdir, result, jlabel_hashes)
    (tdir / 'answer.txt').write_text(result.get('answer') or '')
    write(tdir / 'submission-result.json', result)
    print(json.dumps({'task':tid,'run':run,'answer_chars':len(result.get('answer') or ''),
                      'cost_usd':result.get('cost',{}).get('total'),'error':result.get('error')}),flush=True)


def progress(base):
    m = json.loads((base / 'manifest.json').read_text())
    rows=[];incomplete=[]
    for item in m['schedule']:
        f=trial_dir(base,item['task'],item['run'])/'submission-result.json'
        if f.exists():rows.append(json.loads(f.read_text()))
        else:
            started = f.parent / 'started.json'
            if started.exists():
                record = json.loads(started.read_text())
                incomplete.append({**item, 'pid': record.get('pid'), 'started_at': record.get('started_at')})
    state_path = base / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    return {'time':now(),'completed':len(rows),'total':len(m['schedule']),
            'status':state.get('status','prepared'), 'runner_pid':state.get('pid'),
            'started':len(rows)+len(incomplete),'incomplete_trials':incomplete,
            'not_started':len(m['schedule'])-len(rows)-len(incomplete),
            'nonempty_answers':sum(bool(r.get('answer')) for r in rows),
            'generation_errors':sum(bool(r.get('error')) for r in rows),
            'cost_usd':round(sum(r.get('cost',{}).get('total') or 0 for r in rows),4),
            'cost_scope':'completed trials only',
            'cost_complete':all(r.get('cost',{}).get('total') is not None and
                                r.get('cost',{}).get('complete',True) for r in rows)}


def emit_progress(base, event, persist=False):
    snapshot = dict(progress(base), event=event)
    if persist:
        write(base / 'progress.json', snapshot)
    active = ', '.join(f"{t['task']}#{t['run']}" for t in snapshot['incomplete_trials']) or '-'
    print(f"{snapshot['time'][11:19]}Z {snapshot['event']:<14} done {snapshot['completed']}/{snapshot['total']} "
          f"active {len(snapshot['incomplete_trials'])} ({active}) nonempty {snapshot['nonempty_answers']} "
          f"gen_err {snapshot['generation_errors']} cost ${snapshot['cost_usd']:.2f}", flush=True)
    return snapshot


def wait_for_trials(base, futures, interval=30):
    """Report on completion and at each heartbeat even if every worker is busy."""
    if interval <= 0:
        raise ValueError('Progress interval must be positive')
    pending = set(futures)
    deadline = time.monotonic() + interval
    while pending:
        done, pending = cf.wait(pending, timeout=max(0, deadline - time.monotonic()),
                                return_when=cf.FIRST_COMPLETED)
        for future in done:
            future.result()
        due = time.monotonic() >= deadline
        if done or due:
            emit_progress(base, 'task_completed' if done else 'heartbeat', persist=True)
        if due:
            deadline = time.monotonic() + interval


def watch_progress(base, interval=30):
    """Read-only companion for a runner already in progress; never schedules work."""
    if interval <= 0:
        raise ValueError('Progress interval must be positive')
    try:
        while True:
            snapshot = emit_progress(base, 'status')
            if snapshot['status'] in ('generation_complete','complete','completed','failed','stopped','invalidated'):
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        return  # Stops only this observer; the evaluation is a separate process.


def table_status(base, interval=30, watch=False, queries=False, baseline=None):
    """Run local scoring in this observer process, never inside the generator."""
    path = Path(__file__).with_name('dab-solo-monitor.py')
    spec = importlib.util.spec_from_file_location('dab_status_monitor', path)
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    args = ['--base', str(base), '--interval', str(interval)]
    args += ['--clear'] if watch else ['--once']
    if queries:
        args.append('--queries')
    if baseline is not None:
        args += ['--baseline', str(baseline)]
    return monitor.main(args)


SANDBOX_IMAGE = 'alpha/ide-sandbox-qs:latest'


def preflight():
    # A pruned sandbox image makes every run end 'stuck' with an empty answer.
    r = subprocess.run(['docker', 'image', 'inspect', SANDBOX_IMAGE], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'preflight: docker image {SANDBOX_IMAGE} missing or docker down; rebuild with '
                 'IDE_SANDBOX_IMAGE=alpha/ide-sandbox-qs multica/.claude/worktrees/quorum-sizes/scripts/build-ide-sandbox.sh')


def run_suite(base, parallel, interval=30):
    preflight()
    m=verify_frozen(base)
    state={'status':'running','started_at':now(),'pid':os.getpid(),'parallel':parallel}
    write(base/'state.json',state)
    def launch(item):
        out=trial_dir(base,item['task'],item['run']);out.mkdir(parents=True,exist_ok=True)
        if (out/'submission-result.json').exists():return
        if (out/'started.json').exists():raise RuntimeError('Interrupted trial needs explicit disposition: '+str(out))
        while shutil.disk_usage(ROOT).free<4_000_000_000:
            print('Waiting for 4 GB free disk',flush=True);time.sleep(30)
        with (out/'worker.log').open('w') as f:
            driver = base / 'runtime/scripts/submission-driver.py'
            p=subprocess.run([sys.executable,str(driver),'worker','--base',str(base),
                              '--task',item['task'],'--run',str(item['run'])],stdout=f,stderr=subprocess.STDOUT,
                             env=dict(os.environ, SCRYBE_SUBMISSION_ROOT=str(ROOT), PYTHONDONTWRITEBYTECODE='1'))
        if p.returncode:raise RuntimeError('Worker failed before result: '+str(out))
    try:
        with cf.ThreadPoolExecutor(parallel) as pool:
            futures={pool.submit(launch,item):item for item in m['schedule']}
            emit_progress(base, 'started', persist=True)
            wait_for_trials(base, futures, interval)
        state.update(status='generation_complete',finished_at=now())
    except BaseException as e:
        state.update(status='failed',error=f'{type(e).__name__}: {e}',finished_at=now())
        raise
    finally:
        write(base/'state.json',state)
        emit_progress(base, state['status'], persist=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','run','worker','status'])
    p.add_argument('--base',type=Path,default=DEFAULT)
    p.add_argument('--task');p.add_argument('--run',type=int);p.add_argument('--parallel',type=int,default=4)
    p.add_argument('--harness-source',type=Path,default=SQ,
                   help='prepare only: checkout containing scripts/sq (default: sibling scrybe-sq)')
    p.add_argument('--trials',type=int,default=5,help='prepare only: trials per query (default: 5)')
    p.add_argument('--dry-run',action='store_true',help='prepare only: validate inputs and print paths; no writes or evaluation')
    p.add_argument('--interval',type=float,default=30,
                   help='seconds between run progress updates or watched status snapshots (default: 30)')
    p.add_argument('--watch',action='store_true',help='status only: print immediately and every --interval seconds; never starts work')
    p.add_argument('--format',choices=['table','json'],default='table',
                   help='status only: table includes isolated local correctness scoring; json is unscored progress')
    p.add_argument('--queries',action='store_true',help='table status only: include per-question accuracy')
    p.add_argument('--baseline',type=Path,help='table status only: historical reference run (default: runs/sq-solo-v5)')
    a=p.parse_args();base=a.base.resolve()
    if a.dry_run and a.command!='prepare':p.error('--dry-run is only valid with prepare')
    if a.parallel<1:p.error('--parallel must be at least 1')
    if a.interval<=0:p.error('--interval must be positive')
    if a.watch and a.command!='status':p.error('--watch is only valid with status')
    if a.queries and (a.command!='status' or a.format!='table'):p.error('--queries requires table status')
    if a.baseline is not None and (a.command!='status' or a.format!='table'):p.error('--baseline requires table status')
    if a.command=='prepare':prepare(base,a.harness_source,a.trials,a.dry_run)
    elif a.command=='run':run_suite(base,a.parallel,a.interval)
    elif a.command=='worker':worker(base,a.task,a.run)
    elif a.format=='table':raise SystemExit(table_status(base,a.interval,a.watch,a.queries,a.baseline))
    elif a.watch:watch_progress(base,a.interval)
    else:print(json.dumps(progress(base),indent=2))  # --format json


if __name__=='__main__':main()
