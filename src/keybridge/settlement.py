"""Multi-source settlement with conservative ambiguity and reversal handling."""
import hashlib,itertools,json
from contextlib import closing
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .settlement_store import connect

def cents(value):
    try:
        amount=Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError('invalid monetary amount') from e
    scaled=amount*100
    if not amount.is_finite() or scaled!=scaled.to_integral_value() or amount<=0:
        raise ValueError('amount must be positive whole cents')
    return int(scaled)

def _event(db,kind,item):
    raw=json.dumps(item,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    sha=hashlib.sha256(raw).hexdigest();event_id=item['event_id']
    old=db.execute('SELECT sha,kind FROM settlement_event WHERE event_id=?',(event_id,)).fetchone()
    if old:
        if old['sha']!=sha or old['kind']!=kind:raise ValueError(f'event id or type conflict: {event_id}')
        return False
    db.execute('INSERT INTO settlement_event(event_id,kind,sha,raw,business_time) VALUES(?,?,?,?,?)',
               (event_id,kind,sha,raw,item.get('at')))
    return True

def import_file(db_path,file):
    raw=Path(file).read_bytes();data=json.loads(raw);sha=hashlib.sha256(raw).hexdigest()
    with closing(connect(db_path)) as db,db:
        before=db.execute('SELECT count(*) FROM settlement_event').fetchone()[0]
        for item in data.get('invoices',[]):
            if _event(db,'invoice',item):
                db.execute('INSERT INTO settlement_invoice(id,event_id,party,reference,currency,cents,issued_at) VALUES(?,?,?,?,?,?,?)',
                           (item['id'],item['event_id'],item['party'],item['reference'],item['currency'],cents(item['amount']),item['at']))
        for item in data.get('payments',[]):
            if _event(db,'payment',item):
                db.execute('INSERT INTO settlement_payment(id,event_id,party,reference,currency,cents,paid_at) VALUES(?,?,?,?,?,?,?)',
                           (item['id'],item['event_id'],item['party'],item['reference'],item['currency'],cents(item['amount']),item['at']))
        for item in data.get('allocations',[]):
            if _event(db,'allocation',item):
                db.execute('INSERT INTO settlement_claim(event_id,payment_id,invoice_id,cents) VALUES(?,?,?,?)',
                           (item['event_id'],item['payment_id'],item['invoice_id'],cents(item['amount'])))
        for item in data.get('reversals',[]):
            if _event(db,'reversal',item):
                db.execute('INSERT INTO settlement_reversal(event_id,payment_id,invoice_id,cents,reversed_at) VALUES(?,?,?,?,?)',
                           (item['event_id'],item['payment_id'],item.get('invoice_id'),cents(item['amount']),item['at']))
        db.execute('INSERT OR IGNORE INTO settlement_file(sha,raw) VALUES(?,?)',(sha,raw))
        after=db.execute('SELECT count(*) FROM settlement_event').fetchone()[0]
        if after==before:
            row=db.execute('SELECT run_id FROM settlement_head WHERE id=1').fetchone()
            return {'run_id':row['run_id'] if row else None,'idempotent':True}
        return evaluate_in_connection(db)

def _issue(issues,subject,code,detail):
    issues[(subject,code)]=detail

def _unique_subset(candidates,target):
    if len(candidates)>16:return None,'too_many_candidates'
    solutions=[]
    for size in range(1,len(candidates)+1):
        for chosen in itertools.combinations(candidates,size):
            if sum(amount for _,amount in chosen)==target:
                solutions.append(chosen)
                if len(solutions)>1:return None,'ambiguous_subset'
    if len(solutions)==1:return solutions[0],None
    return None,'no_unique_subset'

def evaluate_in_connection(db):
    invoice={r['id']:dict(r) for r in db.execute('SELECT * FROM settlement_invoice ORDER BY id')}
    payments={r['id']:dict(r) for r in db.execute('SELECT * FROM settlement_payment ORDER BY paid_at,id')}
    claims={pid:[] for pid in payments}
    for row in db.execute('SELECT * FROM settlement_claim ORDER BY event_id'):
        claims[row['payment_id']].append(dict(row))
    reversals={pid:[] for pid in payments}
    for row in db.execute('SELECT * FROM settlement_reversal ORDER BY reversed_at,event_id'):
        reversals[row['payment_id']].append(dict(row))
    issues={};disputed_invoices=set();disputed_payments=set();edges=[]
    net={}
    for pid,p in payments.items():
        total=sum(r['cents'] for r in reversals[pid])
        net[pid]=max(0,p['cents']-total)
        if total>p['cents']:
            disputed_payments.add(pid)
            _issue(issues,pid,'over_reversal','reversals exceed original bank payment')
    for pid,p in payments.items():
        group=claims[pid]
        if not group:continue
        invalid=False
        for claim in group:
            i=invoice[claim['invoice_id']]
            if (p['party'],p['reference'],p['currency'])!=(i['party'],i['reference'],i['currency']):
                invalid=True;disputed_invoices.add(i['id'])
                _issue(issues,pid,'identity_mismatch',f'claim to {i["id"]} crosses party, reference or currency')
        if invalid:
            disputed_payments.add(pid);continue
        unassigned=sum(r['cents'] for r in reversals[pid] if not r['invoice_id'])
        attributed={}
        for r in reversals[pid]:
            if r['invoice_id']:
                attributed[r['invoice_id']]=attributed.get(r['invoice_id'],0)+r['cents']
        if unassigned and unassigned<p['cents'] and len({x['invoice_id'] for x in group})>1:
            disputed_payments.add(pid)
            disputed_invoices.update(x['invoice_id'] for x in group)
            _issue(issues,pid,'ambiguous_reversal','partial reversal of split payment lacks invoice attribution')
            continue
        proposed={}
        for claim in group:proposed[claim['invoice_id']]=proposed.get(claim['invoice_id'],0)+claim['cents']
        if unassigned>=p['cents']:
            proposed={key:0 for key in proposed}
        elif unassigned and len(proposed)==1:
            key=next(iter(proposed));proposed[key]-=unassigned
        for key,amount in attributed.items():
            proposed[key]=proposed.get(key,0)-amount
        if any(amount<0 for amount in proposed.values()) or sum(proposed.values())>net[pid]:
            disputed_payments.add(pid);disputed_invoices.update(proposed)
            _issue(issues,pid,'allocation_exceeds_net','allocation or attributed reversal exceeds payment net')
            continue
        edges.extend((pid,key,amount,'explicit') for key,amount in sorted(proposed.items()) if amount>0)
    paid={iid:0 for iid in invoice}
    for _,iid,amount,_ in edges:paid[iid]+=amount
    for pid,p in payments.items():
        if claims[pid] or pid in disputed_payments or net[pid]==0:continue
        candidates=[(iid,max(0,i['cents']-paid[iid])) for iid,i in invoice.items()
                    if (p['party'],p['reference'],p['currency'])==(i['party'],i['reference'],i['currency'])
                    and i['cents']>paid[iid]]
        if len(candidates)==1:
            iid,remaining=candidates[0];amount=min(net[pid],remaining)
            edges.append((pid,iid,amount,'inferred'));paid[iid]+=amount
        elif len(candidates)>1:
            subset,error=_unique_subset(candidates,net[pid])
            if error:
                disputed_payments.add(pid);disputed_invoices.update(iid for iid,_ in candidates)
                _issue(issues,pid,'ambiguous_match',f'{error}: multiple same identity invoices remain')
            else:
                for iid,amount in subset:edges.append((pid,iid,amount,'inferred'));paid[iid]+=amount
        else:
            _issue(issues,pid,'unmatched_payment','no invoice with matching party, reference and currency')
            disputed_payments.add(pid)
    for _,iid,_,_ in edges:
        if paid[iid]>invoice[iid]['cents']:
            disputed_invoices.add(iid)
            _issue(issues,iid,'invoice_overpaid','combined allocation exceeds invoice amount')
    digest=hashlib.sha256(''.join(r['sha'] for r in db.execute('SELECT sha FROM settlement_event ORDER BY event_id')).encode()).hexdigest()
    old=db.execute('SELECT r.id,r.source_sha FROM settlement_head h JOIN settlement_run r ON r.id=h.run_id WHERE h.id=1').fetchone()
    if old and old['source_sha']==digest:return {'run_id':old['id'],'idempotent':True}
    residual=any(net[pid]>sum(e[2] for e in edges if e[0]==pid) for pid in payments)
    status='disputed' if disputed_payments or disputed_invoices else ('review' if issues else ('open' if residual or any(paid[i]<row['cents'] for i,row in invoice.items()) else 'settled'))
    run=db.execute('INSERT INTO settlement_run(source_sha,status) VALUES(?,?)',(digest,status)).lastrowid
    for pid,iid,amount,source in edges:
        db.execute('INSERT INTO settlement_edge(run_id,payment_id,invoice_id,cents,source) VALUES(?,?,?,?,?)',(run,pid,iid,amount,source))
    for iid,row in invoice.items():
        amount=paid[iid];outstanding=max(0,row['cents']-amount)
        state='disputed' if iid in disputed_invoices else ('settled' if outstanding==0 else ('partial' if amount else 'unpaid'))
        reason='ambiguous or invalid allocation evidence' if iid in disputed_invoices else f'{amount} of {row["cents"]} cents allocated'
        db.execute('INSERT INTO settlement_invoice_decision(run_id,invoice_id,state,paid_cents,outstanding_cents,reason) VALUES(?,?,?,?,?,?)',
                   (run,iid,state,amount,outstanding,reason))
    for pid,row in payments.items():
        allocated=sum(e[2] for e in edges if e[0]==pid)
        state='disputed' if pid in disputed_payments else ('reversed' if net[pid]==0 else ('allocated' if allocated==net[pid] else ('partial' if allocated else 'unmatched')))
        db.execute('INSERT INTO settlement_payment_decision(run_id,payment_id,state,net_cents,allocated_cents,reason) VALUES(?,?,?,?,?,?)',
                   (run,pid,state,net[pid],allocated,f'{allocated} of {net[pid]} net cents assigned'))
    for (subject,code),detail in issues.items():
        db.execute('INSERT INTO settlement_issue(run_id,subject,code,detail) VALUES(?,?,?,?)',(run,subject,code,detail))
    db.execute('INSERT INTO settlement_head(id,run_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id',(run,))
    return {'run_id':run,'status':status,'issues':len(issues),'edges':len(edges),'idempotent':False}

def evaluate(db_path):
    with closing(connect(db_path)) as db,db:return evaluate_in_connection(db)

def report(db_path):
    with closing(connect(db_path)) as db:
        head=db.execute('SELECT run_id FROM settlement_head WHERE id=1').fetchone()
        if not head:return {'run_id':None,'invoices':[],'payments':[],'edges':[],'issues':[]}
        run=head['run_id']
        result={'run_id':run,'status':db.execute('SELECT status FROM settlement_run WHERE id=?',(run,)).fetchone()[0]}
        for name,table in [('invoices','settlement_invoice_decision'),('payments','settlement_payment_decision'),('edges','settlement_edge'),('issues','settlement_issue')]:
            result[name]=[dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE run_id=? ORDER BY 2,3',(run,))]
        result['history']=[dict(r) for r in db.execute('SELECT id,source_sha,status FROM settlement_run ORDER BY id')]
        result['source_files']=[{'id':r['id'],'sha':r['sha']} for r in db.execute('SELECT id,sha FROM settlement_file ORDER BY id')]
        return result
