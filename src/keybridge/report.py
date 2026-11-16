import json
from pathlib import Path
from .store import connect
from .rules import _batches
def write(path,plan_id,out):
    db=connect(path);p=dict(db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone())
    actions=json.loads(p['actions']);batches=_batches(actions)
    cursor=db.execute('SELECT * FROM plan_cursor WHERE plan_id=?',(plan_id,)).fetchone()
    committed={r['seq'] for r in db.execute('SELECT seq FROM apply_batch WHERE plan_id=?',(plan_id,))}
    if p['status']=='applied':available=[b[0]['table'] for b in batches if b];pending=[]
    elif cursor:
        available=[batches[i][0]['table'] for i in sorted(committed) if i<len(batches) and batches[i]]
        pending=[b[0]['table'] for i,b in enumerate(batches) if i not in committed and b]
    else:available=[];pending=[b[0]['table'] for b in batches if b]
    data={'plan':{k:v for k,v in p.items() if k!='actions'},'actions':actions,
          'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
          'collisions':[dict(x) for x in db.execute('SELECT * FROM collision_group')],
          'edges':[dict(x) for x in db.execute('SELECT * FROM reference_edge')],
          'sources':[dict(x) for x in db.execute('SELECT id,path,sha FROM source_file')],
          'steps':[dict(x) for x in db.execute('SELECT * FROM apply_step WHERE plan_id=?',(plan_id,))],
          'progress':{'mode':cursor['mode'] if cursor else ('atomic' if p['status']=='applied' else None),
                      'complete':p['status']=='applied',
                      'next_batch':cursor['next_batch'] if cursor else None,
                      'total_batches':len(batches),
                      'available_tables':available,
                      'pending_tables':pending,
                      'batches':[{'seq':i,'table':b[0]['table'] if b else None,'actions':len(b),
                                  'status':'committed' if i in committed else 'pending'} for i,b in enumerate(batches)]}}
    data['counts']={}
    for row in db.execute('SELECT name FROM table_contract'):
        name=row['name'];data['counts'][name]={
          'input':db.execute('SELECT COUNT(*) FROM source_row WHERE table_name=?',(name,)).fetchone()[0],
          'insert':sum(a['kind']=='insert' and a['table']==name for a in data['actions']),
          'reuse':sum(a['kind']=='reuse' and a['table']==name for a in data['actions']),
          'isolated':db.execute('SELECT COUNT(*) FROM collision_group WHERE table_name=?',(name,)).fetchone()[0]+db.execute('SELECT COUNT(*) FROM issue WHERE subject LIKE ?',(name+':%',)).fetchone()[0]}
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    out.with_suffix('.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    progress=data['progress']
    lines=['# 迁移计划','','状态：'+p['status'],'模式：'+str(progress['mode']),'完成：'+('是' if progress['complete'] else '否')]
    if available:lines.append('可用表：'+','.join(available))
    if pending:lines.append('待执行表：'+','.join(pending))
    lines+=['']+[f"- {a['table']} {a['old']} → {a['new']} {a['kind']}" for a in actions]
    out.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    db.close();return out.with_suffix('.json')
