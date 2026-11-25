import argparse,csv,json,tempfile
from pathlib import Path
from .contract import compile_contract
from .ingest import import_rows,import_target
from .rules import plan,apply,rollback
from .report import write
from .cutover import audit_target,configure_clients,observe_lookup,retirement_decision
from .corrections import import_corrections,prepare_corrections,apply_corrections,correction_report
from .authorization import import_grants,check_access
from .access_tokens import issue as issue_credential,verify as verify_credential
from .auth_report import report as authorization_report
from .archive import register_contract,import_candidates,plan_archive,import_replica,audit_archive,archive_report
from .shards import create_manifest,ingest_shard,restore,shard_report
from .quality import register_contract as quality_contract,import_batch as quality_import,evaluate as quality_evaluate,propose_repair as quality_propose,review_repair as quality_review,release as quality_release,quality_report
from .credit import register_contract as credit_contract,issue_note,review_note,revoke_note
from .credit_effect import decide_credit,apply_credit,fulfill_refund,audit_credit,credit_report
from .settlement import import_file as settlement_import,evaluate as settlement_evaluate,report as settlement_report
from .feature_pipeline import register_contract as feature_contract,import_observations as feature_import,import_labels as feature_labels,build_training as feature_train,import_capture as feature_capture,check_parity as feature_parity,release as feature_release,report as feature_report
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
    for name in ('archive-contract','archive-candidates','archive-replica'):
        command=s.add_parser(name);command.add_argument('file');command.add_argument('--db',required=True)
    ap=s.add_parser('archive-plan');ap.add_argument('--db',required=True);ap.add_argument('--contract',required=True)
    aa=s.add_parser('archive-audit');aa.add_argument('--db',required=True);aa.add_argument('--plan',type=int,required=True)
    arpt=s.add_parser('archive-report');arpt.add_argument('--db',required=True)
    sm=s.add_parser('shard-manifest');sm.add_argument('--db',required=True);sm.add_argument('--plan',type=int,required=True);sm.add_argument('--candidate',required=True);sm.add_argument('--revision',type=int,required=True);sm.add_argument('--input',required=True);sm.add_argument('--out',required=True)
    si=s.add_parser('shard-ingest');si.add_argument('--db',required=True);si.add_argument('--manifest',type=int,required=True);si.add_argument('--proof',required=True);si.add_argument('--index',type=int,required=True);si.add_argument('--domain',required=True);si.add_argument('file')
    sr=s.add_parser('shard-restore');sr.add_argument('--db',required=True);sr.add_argument('--manifest',type=int,required=True);sr.add_argument('--lost',action='append',default=[]);sr.add_argument('--out')
    ss=s.add_parser('shard-report');ss.add_argument('--db',required=True)
    qc=s.add_parser('quality-contract');qc.add_argument('file');qc.add_argument('--db',required=True)
    qi=s.add_parser('quality-import');qi.add_argument('file');qi.add_argument('--db',required=True);qi.add_argument('--contract',required=True)
    qe=s.add_parser('quality-evaluate');qe.add_argument('--db',required=True);qe.add_argument('--batch',type=int,required=True)
    qp=s.add_parser('quality-propose');qp.add_argument('file');qp.add_argument('--db',required=True)
    qr=s.add_parser('quality-review');qr.add_argument('--db',required=True);qr.add_argument('--proposal',required=True);qr.add_argument('--reviewer',required=True);qr.add_argument('--decision',choices=['approve','reject'],required=True);qr.add_argument('--reason',required=True)
    ql=s.add_parser('quality-release');ql.add_argument('--db',required=True);ql.add_argument('--batch',type=int,required=True);ql.add_argument('--out')
    qrp=s.add_parser('quality-report');qrp.add_argument('--db',required=True)
    for name in ('credit-contract','credit-note','credit-review','credit-apply','credit-refund','credit-revoke'):
        command=s.add_parser(name);command.add_argument('file');command.add_argument('--db',required=True)
    cd=s.add_parser('credit-decide');cd.add_argument('--db',required=True);cd.add_argument('--note',required=True);cd.add_argument('--as-of',required=True)
    cau=s.add_parser('credit-audit');cau.add_argument('--db',required=True);cau.add_argument('--note',required=True);cau.add_argument('--as-of',required=True)
    crep=s.add_parser('credit-report');crep.add_argument('--db',required=True);crep.add_argument('--note',required=True)
    se=s.add_parser('settlement-import');se.add_argument('file');se.add_argument('--db',required=True)
    sv=s.add_parser('settlement-evaluate');sv.add_argument('--db',required=True)
    srp=s.add_parser('settlement-report');srp.add_argument('--db',required=True)
    sd=s.add_parser('demo-settlement');sd.add_argument('--workdir')
    for name in ('feature-contract','feature-import','feature-labels','feature-capture'):
        command=s.add_parser(name);command.add_argument('file');command.add_argument('--db',required=True)
    ft=s.add_parser('feature-train');ft.add_argument('--db',required=True);ft.add_argument('--contract',required=True);ft.add_argument('--freeze-at',required=True)
    fp=s.add_parser('feature-parity');fp.add_argument('--db',required=True);fp.add_argument('--build',type=int,required=True);fp.add_argument('--capture',required=True)
    fr=s.add_parser('feature-release');fr.add_argument('--db',required=True);fr.add_argument('--build',type=int,required=True);fr.add_argument('--parity',type=int,required=True)
    frep=s.add_parser('feature-report');frep.add_argument('--db',required=True);frep.add_argument('--build',type=int,required=True)
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
    elif x.cmd=='archive-contract':print(register_contract(x.db,x.file))
    elif x.cmd=='archive-candidates':print(import_candidates(x.db,x.file))
    elif x.cmd=='archive-plan':print(plan_archive(x.db,x.contract))
    elif x.cmd=='archive-replica':print(import_replica(x.db,x.file))
    elif x.cmd=='archive-audit':print(audit_archive(x.db,x.plan))
    elif x.cmd=='archive-report':print(json.dumps(archive_report(x.db),ensure_ascii=False,indent=2))
    elif x.cmd=='shard-manifest':print(json.dumps(create_manifest(x.db,x.plan,x.candidate,x.revision,x.input,x.out),ensure_ascii=False))
    elif x.cmd=='shard-ingest':print(json.dumps(ingest_shard(x.db,x.manifest,x.proof,x.index,x.domain,x.file),ensure_ascii=False))
    elif x.cmd=='shard-restore':print(json.dumps(restore(x.db,x.manifest,x.lost,x.out),ensure_ascii=False))
    elif x.cmd=='shard-report':print(json.dumps(shard_report(x.db),ensure_ascii=False,indent=2))
    elif x.cmd=='quality-contract':print(quality_contract(x.db,x.file))
    elif x.cmd=='quality-import':print(quality_import(x.db,x.contract,x.file))
    elif x.cmd=='quality-evaluate':print(json.dumps(quality_evaluate(x.db,x.batch),ensure_ascii=False,indent=2))
    elif x.cmd=='quality-propose':print(quality_propose(x.db,x.file))
    elif x.cmd=='quality-review':print(json.dumps(quality_review(x.db,x.proposal,x.reviewer,x.decision=='approve',x.reason),ensure_ascii=False,indent=2))
    elif x.cmd=='quality-release':print(json.dumps(quality_release(x.db,x.batch,x.out),ensure_ascii=False,indent=2))
    elif x.cmd=='quality-report':print(json.dumps(quality_report(x.db),ensure_ascii=False,indent=2))
    elif x.cmd=='credit-contract':print(json.dumps(credit_contract(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-note':print(json.dumps(issue_note(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-review':print(json.dumps(review_note(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-decide':print(json.dumps(decide_credit(x.db,x.note,x.as_of),ensure_ascii=False))
    elif x.cmd=='credit-apply':print(json.dumps(apply_credit(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-refund':print(json.dumps(fulfill_refund(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-revoke':print(json.dumps(revoke_note(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='credit-audit':print(json.dumps(audit_credit(x.db,x.note,x.as_of),ensure_ascii=False))
    elif x.cmd=='credit-report':print(json.dumps(credit_report(x.db,x.note),ensure_ascii=False,indent=2))
    elif x.cmd=='settlement-import':print(json.dumps(settlement_import(x.db,x.file),ensure_ascii=False,indent=2))
    elif x.cmd=='settlement-evaluate':print(json.dumps(settlement_evaluate(x.db),ensure_ascii=False,indent=2))
    elif x.cmd=='settlement-report':print(json.dumps(settlement_report(x.db),ensure_ascii=False,indent=2))
    elif x.cmd=='demo-settlement':
        root=Path(x.workdir or tempfile.mkdtemp(prefix='keybridge-settlement-'));root.mkdir(parents=True,exist_ok=True)
        db=root/'settlement.sqlite';fixture=Path(__file__).resolve().parent.parent.parent/'tests'/'fixtures'/'settlement_ok.json'
        settlement_import(db,fixture)
        print(json.dumps({'db':str(db),'report':settlement_report(db)},ensure_ascii=False,indent=2))
    elif x.cmd=='feature-contract':print(json.dumps(feature_contract(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='feature-import':print(json.dumps(feature_import(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='feature-labels':print(json.dumps(feature_labels(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='feature-capture':print(json.dumps(feature_capture(x.db,x.file),ensure_ascii=False))
    elif x.cmd=='feature-train':print(json.dumps(feature_train(x.db,x.contract,x.freeze_at),ensure_ascii=False))
    elif x.cmd=='feature-parity':print(json.dumps(feature_parity(x.db,x.build,x.capture),ensure_ascii=False))
    elif x.cmd=='feature-release':print(json.dumps(feature_release(x.db,x.build,x.parity),ensure_ascii=False))
    elif x.cmd=='feature-report':print(json.dumps(feature_report(x.db,x.build),ensure_ascii=False,indent=2))
    else:print(write(x.db,x.plan,x.out))
if __name__=='__main__':main()
