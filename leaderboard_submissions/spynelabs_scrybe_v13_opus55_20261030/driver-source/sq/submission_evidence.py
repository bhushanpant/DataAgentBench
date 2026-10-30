
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
    def capture(rid, final=False, view=None):
        rec=records[rid];out=rec['out']
        try:
            # A caller fetching this view already paid for it. Reuse the exact
            # response; keep full message capture and the final pre-delete fetch.
            v=view if view is not None else raw('GET',f'/api/ship/runs/{rid}/quorum',timeout=30)
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
                view=d if m.group(2) else None
                final=bool(view is not None and ((view.get('report') or {}).get('status') or not view.get('live',True)))
                rec['last']=time.time();capture(rid,final,view=view)
        return d
    q.api=wrapped
    return records
