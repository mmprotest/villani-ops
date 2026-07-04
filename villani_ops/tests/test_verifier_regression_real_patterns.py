import json
import httpx
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.deterministic import build_packet, deterministic_result
from villani_ops.verifier.llm import llm_result, calibrate
from villani_ops.verifier.extract import extract_deliverables


def fx(tmp_path, objective, commands=None, tools=None, final=None, patches=None):
    d=tmp_path/'debug'; d.mkdir()
    (d/'session_meta.json').write_text(json.dumps({'objective':objective,'run_id':'r'}))
    (d/'summary.json').write_text(json.dumps({'status':'completed'}))
    (d/'final_summary.json').write_text(json.dumps(final or {'status':'completed'}))
    (d/'commands.jsonl').write_text('\n'.join(json.dumps(x) for x in (commands or [])))
    (d/'tool_calls.jsonl').write_text('\n'.join(json.dumps(x) for x in (tools or [])))
    (d/'patches.jsonl').write_text('\n'.join(json.dumps(x) for x in (patches or [])))
    (d/'model_responses.jsonl').write_text('')
    return d

def mock_success(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':json.dumps({'type':'final_verdict','result':1,'verdict':'success','confidence':0.82,'recommendedAction':'accept','reason':'Deliverable-linked evidence validates the task.','deliverableAssessment':{'requiredDeliverables':[],'validatedDeliverables':[],'missingDeliverables':[],'weakValidationReasons':[]},'constraintAssessment':{'constraints':[],'satisfiedConstraints':[],'violatedConstraints':[],'uncheckedConstraints':[]}})}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp())

def ws(tmp_path):
    s=FileStorage(tmp_path/'ws'); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)}); return str(tmp_path/'ws')

def test_path_extraction_rejects_fraction():
    spec=extract_deliverables('Use (alpha + beta)^(-5/2) and write analysis.R plus posterior_alpha_mean.txt')
    assert '/2' not in spec.required_files
    assert 'analysis.R' in spec.required_files

def test_fixture_a_ics_write_only_not_flipped(monkeypatch,tmp_path):
    d=fx(tmp_path,'Create meeting_scheduled.ics',tools=[{'tool_call_id':'w','tool_name':'Write','status':'completed','args':{'path':'meeting_scheduled.ics','content':'BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:S\nDTSTART:20260101T120000Z\nDTEND:20260101T130000Z\nATTENDEE:a\nATTENDEE:b\nATTENDEE:c\nEND:VEVENT\nEND:VCALENDAR'}}],final={'status':'completed','changed_files':['meeting_scheduled.ics']})
    run=load_debug_run(d); pkt=build_packet(run); assert pkt['evidence']['deliverableEvidence']; assert 'ics_deliverable_structure' in pkt['evidence']['deliverableEvidence'][0]['text']
    mock_success(monkeypatch); res=llm_result(run,deterministic_result(run,mode='llm_tool_loop'),workspace=ws(tmp_path)); assert res['result']==1

def test_fixture_b_eval_py_strong(tmp_path):
    d=fx(tmp_path,'Optimize /app/eigen.py',commands=[{'command':'python -c "import numpy as np; print(np.show_config())"','exit_code':0,'stdout':'blas'}, {'command':"python - <<'PY'\ndef largest_eigenvalue(x): return 1\nprint('All correctness tests passed!')\nPY",'exit_code':0,'stdout':'All correctness tests passed!'}, {'command':'cd /app && python eval.py','exit_code':0,'stdout':'All correctness tests passed! performance OK'}],tools=[{'tool_call_id':'w','tool_name':'Write','status':'completed','args':{'path':'/app/eigen.py','content':'def largest_eigenvalue(x): return 1'}}],final={'status':'completed','changed_files':['/app/eigen.py']})
    det=deterministic_result(load_debug_run(d)); vals=det['evidenceByCategory']['testValidation']; assert any('eval.py' in v['text'] and v['validationStrength']=='strong' for v in vals); assert not any('show_config' in e['text'] for e in det['evidenceByCategory']['finalEndToEndValidation'])

def test_fixture_c_inline_false_positive_remains_failure(tmp_path):
    d=fx(tmp_path,'Implement /app/eigen.py',commands=[{'command':"python - <<'PY'\ndef largest_eigenvalue(x): return 1\nprint('All correctness tests passed!')\nPY",'exit_code':0,'stdout':'All correctness tests passed!'}])
    det=deterministic_result(load_debug_run(d),mode='llm_tool_loop'); assert det['evidenceByCategory']['testValidation'][0]['validationStrength']=='weak'; out=calibrate(det,{'result':0,'verdict':'failure','confidence':.7,'recommendedAction':'reject','reason':'inline only','riskFlags':[]}); assert out['result']==0

def test_fixture_d_rscript_later_success(tmp_path):
    d=fx(tmp_path,'Run analysis.R with hierarchical_model.stan and create posterior_alpha_mean.txt posterior_beta_mean.txt',commands=[{'command':'Rscript -e "install.packages(\'rstan\', repos=\"https://cloud.r-project.org\")"','exit_code':0,'stdout':'installed'}, {'command':'Rscript analysis.R','exit_code':1,'stderr':'Stan error'}, {'command':'Rscript analysis.R','exit_code':0,'stdout':'wrote posterior files'}, {'command':'cat posterior_alpha_mean.txt posterior_beta_mean.txt','exit_code':0,'stdout':'1.23\n4.56'}],final={'status':'completed','changed_files':['analysis.R','hierarchical_model.stan','posterior_alpha_mean.txt','posterior_beta_mean.txt']})
    det=deterministic_result(load_debug_run(d)); assert any('install.packages' in e['text'] for e in det['evidenceByCategory']['setupEvidence']); assert not det['evidenceByCategory']['serviceValidation']; assert any('Rscript analysis.R' in e['text'] and e['validationStrength']=='strong' for e in det['evidenceByCategory']['testValidation']); assert det['evidenceByCategory']['recoveredFailures']

def test_fixture_e_sqlite_gcov(tmp_path):
    cmds=[{'command':'./configure --enable-gcov && make && make install','exit_code':0,'stdout':'ok'}, {'command':'file sqlite3','exit_code':127,'stderr':'file not found'}, {'command':'which sqlite3','exit_code':0,'stdout':'/usr/local/bin/sqlite3'}, {'command':'sqlite3 --version','exit_code':0,'stdout':'3.0'}, {'command':'sqlite3 :memory: "select 1"','exit_code':0,'stdout':'1'}, {'command':'nm /usr/local/bin/sqlite3 | grep gcov','exit_code':0,'stdout':'__gcov_init'}, {'command':'find . -name "*.gcno"','exit_code':0,'stdout':'./x.gcno'}, {'command':'find . -name "*.gcda"','exit_code':0,'stdout':'./x.gcda'}]
    det=deterministic_result(load_debug_run(fx(tmp_path,'Build and install sqlite3 with gcov coverage artifacts .gcno .gcda',commands=cmds)))
    assert det['evidenceByCategory']['finalEndToEndValidation']; assert det['evidenceByCategory']['recoveredFailures']

def test_fixture_f_overfull_constraint_violation(tmp_path):
    d=fx(tmp_path,'Only edit input.tex by replacing words using synonyms.txt. main.tex and synonyms.txt must not change.',commands=[{'command':'pdflatex main.tex','exit_code':0,'stdout':'no Overfull \\hbox'}],final={'status':'completed','changed_files':['input.tex']},patches=[{'file_path':'input.tex','ok':True,'diff':'not allowed non-synonym replacement'}])
    res=deterministic_result(load_debug_run(d)); assert res['result']==0; assert res['evidenceByCategory']['constraintEvidence']

def test_fixture_g_overfull_valid_synonym(tmp_path):
    d=fx(tmp_path,'Only edit input.tex by replacing words using synonyms.txt. main.tex and synonyms.txt must not change.',commands=[{'command':'pdflatex main.tex','exit_code':0,'stdout':'no warnings'}],final={'status':'completed','changed_files':['input.tex']},patches=[{'file_path':'input.tex','ok':True,'diff':'allowed replacement synonym'}])
    res=deterministic_result(load_debug_run(d)); assert not res['constraintAssessment']['violatedConstraints']; assert res['evidenceByCategory']['constraintEvidence']
