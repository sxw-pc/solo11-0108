import argparse,csv,json,tempfile
from pathlib import Path
from .contract import compile_contract
from .ingest import import_rows,import_target
from .rules import plan,apply,rollback
from .report import write
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
def main():
    p=argparse.ArgumentParser(description='离线多表键映射迁移');s=p.add_subparsers(dest='cmd',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    c=s.add_parser('contract');c.add_argument('file');c.add_argument('--db',required=True)
    i=s.add_parser('import');i.add_argument('table');i.add_argument('file');i.add_argument('--db',required=True)
    t=s.add_parser('target');t.add_argument('table');t.add_argument('file');t.add_argument('--db',required=True)
    n=s.add_parser('plan');n.add_argument('--db',required=True)
    a=s.add_parser('apply');a.add_argument('--db',required=True);a.add_argument('--plan',type=int,required=True)
    b=s.add_parser('rollback');b.add_argument('--db',required=True);b.add_argument('--plan',type=int,required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--plan',type=int,required=True);r.add_argument('--out',required=True)
    x=p.parse_args()
    if x.cmd=='demo':print(demo(x.workdir))
    elif x.cmd=='contract':print(compile_contract(x.file,x.db))
    elif x.cmd=='import':print(import_rows(x.db,x.table,x.file))
    elif x.cmd=='target':print(import_target(x.db,x.table,x.file))
    elif x.cmd=='plan':print(plan(x.db))
    elif x.cmd=='apply':apply(x.db,x.plan)
    elif x.cmd=='rollback':rollback(x.db,x.plan)
    else:print(write(x.db,x.plan,x.out))
if __name__=='__main__':main()
