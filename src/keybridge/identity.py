import hashlib
def new_key(table,natural):return table[:3].upper()+'-'+hashlib.sha256(natural.encode()).hexdigest()[:12]
def allocate(db):
    seen={};issues=[]
    rows=list(db.execute('SELECT * FROM source_row ORDER BY table_name,natural,old_key'))
    for r in rows:
        key=(r['table_name'],r['natural']);new=new_key(*key)
        if key in seen and seen[key]!=r['old_key']:
            issues.append(('natural_collision',r['table_name']+':'+r['old_key']));continue
        seen[key]=r['old_key']
        db.execute('INSERT OR REPLACE INTO identity_map VALUES (?,?,?,?)',(r['table_name'],r['old_key'],new,'v1'))
    for code,subject in issues:db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',(code,subject))
    return issues
