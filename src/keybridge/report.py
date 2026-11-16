import json
from pathlib import Path
from .store import connect
def write(path,plan_id,out):
    db=connect(path);p=dict(db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone())
    data={'plan':{k:v for k,v in p.items() if k!='actions'},'actions':json.loads(p['actions']),
          'groups':json.loads(p['groups']) if p.get('groups') else [],
          'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
          'collisions':[dict(x) for x in db.execute('SELECT * FROM collision_group')],
          'edges':[dict(x) for x in db.execute('SELECT * FROM reference_edge')],
          'sources':[dict(x) for x in db.execute('SELECT id,path,sha FROM source_file')],
          'steps':[dict(x) for x in db.execute('SELECT * FROM apply_step WHERE plan_id=?',(plan_id,))]}
    data['counts']={}
    for row in db.execute('SELECT name FROM table_contract'):
        name=row['name'];data['counts'][name]={
          'input':db.execute('SELECT COUNT(*) FROM source_row WHERE table_name=?',(name,)).fetchone()[0],
          'insert':sum(a['kind']=='insert' and a['table']==name for a in data['actions']),
          'reuse':sum(a['kind']=='reuse' and a['table']==name for a in data['actions']),
          'isolated':db.execute('SELECT COUNT(*) FROM collision_group WHERE table_name=?',(name,)).fetchone()[0]+db.execute('SELECT COUNT(*) FROM issue WHERE subject LIKE ?',(name+':%',)).fetchone()[0]}
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    out.with_suffix('.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    out.with_suffix('.md').write_text('# 迁移计划\n\n状态：'+p['status']+'\n\n'+'\n'.join(f"- {a['table']} {a['old']} → {a['new']} {a['kind']}" for a in data['actions']),encoding='utf-8')
    db.close();return out.with_suffix('.json')
