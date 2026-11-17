import argparse,csv,json,tempfile
from pathlib import Path
from .contract import compile_contract
from .ingest import import_rows,import_target
from .rules import plan,apply,rollback
from .report import write
from .cutover import audit_target,configure_clients,observe_lookup,retirement_decision
from .corrections import import_corrections,prepare_corrections,apply_corrections,correction_report
from .authorization import import_grants,check_access
from .credentials import issue as issue_credential,verify as verify_credential
from .auth_report import report as authorization_report
from .store import connect
from .cutover import _hash
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='keybridge-'));root.mkdir(parents=True,exist_ok=True)
    contract=root/'contract.json';contract.write_text(json.dumps({'tables':[{'name':'parent','pk':'id','natural':'code'}, {'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}),encoding='utf-8')
    for name,cols,values in [('parent',['id','code'],[['p1','A']]),('child',['id','code','parent_id'],[['c1','C','p1']])]:
        with (root/f'{name}.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.writer(f);w.writerow(cols);w.writerows(values)
    db=root/'data.db';compile_contract(contract,db)
    for name in ['parent','child']:import_rows(db,name,root/f'{name}.csv')
    pid,status,_=plan(db)
    if status=='ready':apply(db,pid)
    return write(db,pid,root/'report')
def decision_demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='keybridge-decision-'));root.mkdir(parents=True,exist_ok=True)
    demo(root);db=root/'data.db';con=connect(db)
    pid=con.execute('SELECT MAX(id) FROM migration_plan').fetchone()[0]
    aid,status,_=audit_target(db,pid)
    clients=root/'clients.json';clients.write_text(json.dumps([{'id':'catalog','critical':True,'table':'parent'}]),encoding='utf-8')
    configure_clients(db,clients)
    mapping=con.execute("SELECT * FROM identity_map WHERE table_name='parent' ORDER BY old_key LIMIT 1").fetchone()
    payload=json.loads(con.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',('parent',mapping['new_key'])).fetchone()[0])
    observe_lookup(db,'catalog',pid,1,mapping['old_key'],mapping['new_key'],_hash(payload))
    gate,retirement,_=retirement_decision(db,aid)
    return {'target_audit':aid,'target_status':status,'retirement_gate':gate,'retirement_status':retirement,'db':str(db)}
def correction_demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='keybridge-correction-'));root.mkdir(parents=True,exist_ok=True)
    demo(root);db=root/'data.db';con=connect(db)
    pid=con.execute('SELECT MAX(id) FROM migration_plan').fetchone()[0]
    file=root/'correction.json';file.write_text(json.dumps([
        {'table':'parent','old_key':'p1','expected_revision':0,'after':{'id':'p1','code':'A','label':'reviewed'}}]),encoding='utf-8')
    batch=import_corrections(db,pid,file);prepare_corrections(db,batch);status=apply_corrections(db,batch)
    audit,quality,_=audit_target(db,pid)
    return {'correction_batch':batch,'correction_status':status,'target_audit':audit,'target_status':quality,'db':str(db)}
def authorization_demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='keybridge-auth-'));root.mkdir(parents=True,exist_ok=True)
    demo(root);db=root/'data.db';con=connect(db)
    pid=con.execute('SELECT MAX(id) FROM migration_plan').fetchone()[0]
    audit_target(db,pid)
    fixtures=Path(__file__).resolve().parent.parent.parent/'tests'/'fixtures'
    import_grants(db,fixtures/'auth_normal.json')
    mapping=con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    request={'request_id':'access-1','table':'parent','new_key':mapping,'principal':'analyst',
             'purpose':'analytics','fields':['code'],'at':'2026-06-01T00:00:00Z'}
    decision=check_access(db,request)
    issued=issue_credential(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')
    before=verify_credential(db,'cred-1',issued['token'],'2026-06-02T00:00:00Z')
    import_grants(db,fixtures/'auth_revoke.json')
    after=verify_credential(db,'cred-1',issued['token'],'2026-08-01T00:00:00Z')
    authorization_report(db,root/'authorization.json')
    return {'db':str(db),'access':decision['decision'],'before':before['status'],
            'after':after['status'],'report':str(root/'authorization.json')}
def main():
    p=argparse.ArgumentParser(description='离线多表键映射迁移');s=p.add_subparsers(dest='cmd',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    dd=s.add_parser('demo-decisions');dd.add_argument('--workdir')
    dc=s.add_parser('demo-corrections');dc.add_argument('--workdir')
    da=s.add_parser('demo-auth');da.add_argument('--workdir')
    c=s.add_parser('contract');c.add_argument('file');c.add_argument('--db',required=True)
    i=s.add_parser('import');i.add_argument('table');i.add_argument('file');i.add_argument('--db',required=True)
    t=s.add_parser('target');t.add_argument('table');t.add_argument('file');t.add_argument('--db',required=True)
    n=s.add_parser('plan');n.add_argument('--db',required=True)
    a=s.add_parser('apply');a.add_argument('--db',required=True);a.add_argument('--plan',type=int,required=True)
    b=s.add_parser('rollback');b.add_argument('--db',required=True);b.add_argument('--plan',type=int,required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--plan',type=int,required=True);r.add_argument('--out',required=True)
    au=s.add_parser('target-audit');au.add_argument('--db',required=True);au.add_argument('--plan',type=int,required=True)
    cl=s.add_parser('client-contract');cl.add_argument('file');cl.add_argument('--db',required=True)
    ob=s.add_parser('lookup-observe');ob.add_argument('--db',required=True);ob.add_argument('--client',required=True);ob.add_argument('--plan',type=int,required=True);ob.add_argument('--sequence',type=int,required=True);ob.add_argument('--old-key',required=True);ob.add_argument('--new-key',required=True);ob.add_argument('--payload-sha',required=True)
    rg=s.add_parser('retirement-gate');rg.add_argument('--db',required=True);rg.add_argument('--audit',type=int,required=True)
    ci=s.add_parser('correction-import');ci.add_argument('file');ci.add_argument('--db',required=True);ci.add_argument('--plan',type=int,required=True)
    cp=s.add_parser('correction-prepare');cp.add_argument('--db',required=True);cp.add_argument('--batch',type=int,required=True)
    ca=s.add_parser('correction-apply');ca.add_argument('--db',required=True);ca.add_argument('--batch',type=int,required=True)
    cr=s.add_parser('correction-report');cr.add_argument('--db',required=True);cr.add_argument('--batch',type=int,required=True)
    gi=s.add_parser('auth-import');gi.add_argument('file');gi.add_argument('--db',required=True)
    ac=s.add_parser('access-check');ac.add_argument('file');ac.add_argument('--db',required=True)
    ce=s.add_parser('credential-issue');ce.add_argument('--db',required=True);ce.add_argument('--id',required=True);ce.add_argument('--request',required=True);ce.add_argument('--issued',required=True);ce.add_argument('--expires',required=True)
    cv=s.add_parser('credential-verify');cv.add_argument('--db',required=True);cv.add_argument('--id',required=True);cv.add_argument('--token',required=True);cv.add_argument('--at',required=True)
    ar=s.add_parser('auth-report');ar.add_argument('--db',required=True);ar.add_argument('--out')
    x=p.parse_args()
    if x.cmd=='demo':print(demo(x.workdir))
    elif x.cmd=='demo-decisions':print(decision_demo(x.workdir))
    elif x.cmd=='demo-corrections':print(correction_demo(x.workdir))
    elif x.cmd=='demo-auth':print(authorization_demo(x.workdir))
    elif x.cmd=='contract':print(compile_contract(x.file,x.db))
    elif x.cmd=='import':print(import_rows(x.db,x.table,x.file))
    elif x.cmd=='target':print(import_target(x.db,x.table,x.file))
    elif x.cmd=='plan':print(plan(x.db))
    elif x.cmd=='apply':apply(x.db,x.plan)
    elif x.cmd=='rollback':rollback(x.db,x.plan)
    elif x.cmd=='target-audit':print(audit_target(x.db,x.plan))
    elif x.cmd=='client-contract':print(configure_clients(x.db,x.file))
    elif x.cmd=='lookup-observe':print(observe_lookup(x.db,x.client,x.plan,x.sequence,x.old_key,x.new_key,x.payload_sha))
    elif x.cmd=='retirement-gate':print(retirement_decision(x.db,x.audit))
    elif x.cmd=='correction-import':print(import_corrections(x.db,x.plan,x.file))
    elif x.cmd=='correction-prepare':print(prepare_corrections(x.db,x.batch))
    elif x.cmd=='correction-apply':print(apply_corrections(x.db,x.batch))
    elif x.cmd=='correction-report':print(json.dumps(correction_report(x.db,x.batch),ensure_ascii=False,indent=2))
    elif x.cmd=='auth-import':print(import_grants(x.db,x.file))
    elif x.cmd=='access-check':print(check_access(x.db,json.loads(Path(x.file).read_text(encoding='utf-8'))))
    elif x.cmd=='credential-issue':print(issue_credential(x.db,x.id,x.request,x.issued,x.expires))
    elif x.cmd=='credential-verify':print(verify_credential(x.db,x.id,x.token,x.at))
    elif x.cmd=='auth-report':print(json.dumps(authorization_report(x.db,x.out),ensure_ascii=False,indent=2))
    else:print(write(x.db,x.plan,x.out))
if __name__=='__main__':main()
