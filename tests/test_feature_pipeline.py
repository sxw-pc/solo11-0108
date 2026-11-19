"""Independent feature training and online serving evidence checks."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from keybridge import feature_pipeline as f
from keybridge.feature_store import connect

FIX = Path(__file__).parent / 'fixtures'


def setup(tmp_path):
    db=tmp_path/'feature.sqlite'
    f.register_contract(db,FIX/'feature_contract.json')
    f.import_observations(db,FIX/'feature_observations.json')
    f.import_labels(db,FIX/'feature_labels.json')
    return db


def write(tmp_path,name,data):
    file=tmp_path/name
    file.write_text(json.dumps(data),encoding='utf-8')
    return file


def test_point_in_time_prevents_late_feature_leak_and_keeps_lineage(tmp_path):
    print(__import__('keybridge').__file__)
    db=setup(tmp_path)
    result=f.build_training(db,'risk-v1','2026-11-03T00:00:00Z')
    assert (result['status'],result['eligible'],result['excluded'])==('ready',1,1)
    report=f.report(db,result['build_id'])
    rows={r['label_id']:r for r in report['rows']}
    assert json.loads(rows['la']['values_json'])=={'balance':100,'orders':4}
    assert rows['lb']['status']=='excluded' and 'balance:missing' in rows['lb']['reason']
    assert json.loads(rows['la']['evidence_json'])['balance']=={'observation_id':'bal-A','revision':1}


def test_training_freeze_keeps_old_result_after_late_correction(tmp_path):
    db=setup(tmp_path)
    first=f.build_training(db,'risk-v1','2026-11-03T00:00:00Z')
    late={'observations':[{'observation_id':'bal-A','revision':2,'entity_id':'A','feature_name':'balance',
          'event_at':'2026-11-01T08:00:00Z','available_at':'2026-11-04T00:00:00Z','value':900}]}
    f.import_observations(db,write(tmp_path,'late.json',late))
    second=f.build_training(db,'risk-v1','2026-11-05T00:00:00Z')
    assert f.report(db,first['build_id'])['rows'][0]['values_json']==f.report(db,second['build_id'])['rows'][0]['values_json']
    assert json.loads(f.report(db,second['build_id'])['rows'][0]['values_json'])['balance']==100


def test_online_parity_controls_release(tmp_path):
    db=setup(tmp_path)
    build=f.build_training(db,'risk-v1','2026-11-03T00:00:00Z')['build_id']
    f.import_capture(db,FIX/'feature_capture_bad.json')
    bad=f.check_parity(db,build,'online-bad')
    assert bad['status']=='mismatch'
    with pytest.raises(ValueError,match='release_gate_blocked'):f.release(db,build,bad['parity_id'])
    f.import_capture(db,FIX/'feature_capture_ok.json')
    good=f.check_parity(db,build,'online-1')
    assert good['status']=='matched'
    released=f.release(db,build,good['parity_id'])
    assert released['manifest_sha']==f.report(db,build)['build']['manifest_sha']
    with pytest.raises(ValueError,match='already_released'):f.release(db,build,good['parity_id'])


def test_revision_conflict_rolls_back_whole_source_file(tmp_path):
    db=setup(tmp_path)
    bad={'observations':[{'observation_id':'new','revision':1,'entity_id':'A','feature_name':'orders',
        'event_at':'2026-11-01T09:00:00Z','available_at':'2026-11-01T09:00:00Z','value':5},
        {'observation_id':'bal-A','revision':1,'entity_id':'A','feature_name':'balance',
        'event_at':'2026-11-01T08:00:00Z','available_at':'2026-11-01T09:00:00Z','value':999}]}
    with pytest.raises(ValueError,match='observation_revision_conflict'):
        f.import_observations(db,write(tmp_path,'conflict.json',bad))
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM feature_observation WHERE observation_id='new'").fetchone()[0]==0


def test_unknown_or_early_labels_block_build(tmp_path):
    db=setup(tmp_path)
    result=f.build_training(db,'risk-v1','2026-11-02T10:30:00Z')
    assert result['status']=='blocked' and result['eligible']==0
    with pytest.raises(ValueError,match='label_time_order'):
        f.import_labels(db,write(tmp_path,'badlabel.json',{'labels':[{'label_id':'bad','entity_id':'A',
          'prediction_at':'2026-11-02T10:00:00Z','outcome_at':'2026-11-01T10:00:00Z',
          'known_at':'2026-11-02T11:00:00Z','outcome':1}]}))


def test_public_cli_chain(tmp_path):
    db=tmp_path/'cli.sqlite'
    def cli(*args):
        p=subprocess.run([sys.executable,'-m','keybridge',*args,'--db',str(db)],capture_output=True,text=True)
        assert p.returncode==0,p.stderr
        return json.loads(p.stdout)
    cli('feature-contract',str(FIX/'feature_contract.json'))
    cli('feature-import',str(FIX/'feature_observations.json'))
    cli('feature-labels',str(FIX/'feature_labels.json'))
    build=cli('feature-train','--contract','risk-v1','--freeze-at','2026-11-03T00:00:00Z')
    cli('feature-capture',str(FIX/'feature_capture_ok.json'))
    parity=cli('feature-parity','--build',str(build['build_id']),'--capture','online-1')
    cli('feature-release','--build',str(build['build_id']),'--parity',str(parity['parity_id']))
    assert cli('feature-report','--build',str(build['build_id']))['release']
