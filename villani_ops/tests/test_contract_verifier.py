import json
from pathlib import Path
from villani_ops.verifier.types import DebugRun, CommandRecord, ModelResponseRecord
from villani_ops.verifier.contract import build_task_contract, adjudicate_contract, EvidenceStrength
from villani_ops.verifier.evidence import build_contract_evidence
from villani_ops.verifier.tools import compare_file, run_readonly_command
from villani_ops.verifier.llm import _parse


def run(prompt, cmds=None, tmp_path=None, files=None):
    root=tmp_path or Path('.')
    if files:
        for k,v in files.items():
            p=root/k; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(v)
    dr=DebugRun(debugDir=str(root), objective=prompt, commands=[CommandRecord(command=c[0], exitCode=c[1], stdout=c[2] if len(c)>2 else '', stderr='', index=i) for i,c in enumerate(cmds or [])])
    c=build_task_contract(prompt, root, root, dr)
    e=build_contract_evidence(c, root, root, dr)
    return adjudicate_contract(c,e,{'result':1,'reason':'LLM says success'}), c, e

def test_contract_build_kinds(tmp_path):
    prompts=[
        ('optimize query latency benchmark', 'performance_requirement'),
        ('make analysis.R run', 'final_deliverable_executes'),
        ('install sqlite3 command on PATH', 'clean_environment_availability'),
        ('handle SIGINT in external CLI subprocess', 'external_runtime_behavior'),
        ('only replace words using synonyms.txt', 'constrained_edit'),
        ('fix bug so tests pass', 'test_suite_passes'),
        ('generate report.csv output', 'output_file_created'),
        ('server HTTP endpoint returns JSON', 'api_or_service_behavior'),
        ('produce correct rows', 'data_correctness'),
        ('do the thing', 'unknown'),
    ]
    (tmp_path/'tests').mkdir()
    for prompt, kind in prompts:
        c=build_task_contract(prompt,tmp_path,tmp_path,DebugRun(debugDir=str(tmp_path),objective=prompt))
        assert any(r.kind==kind for r in c.requirements)

def test_file_comparison(tmp_path):
    o=tmp_path/'o'; f=tmp_path/'f'; o.mkdir(); f.mkdir(); (o/'a.txt').write_text('x\n'); (f/'a.txt').write_text('x\ny\n')
    cmp=compare_file(o,f,'a.txt')
    assert cmp.exists_in_original and cmp.exists_in_final and cmp.added_lines==['y'] and not cmp.deleted

def test_readonly_blocks_destructive(tmp_path):
    out=run_readonly_command('rm -rf x', tmp_path)
    assert out['blocked'] and out['verifierGenerated']

def test_query_optimize_explain_without_benchmark_is_not_success(tmp_path):
    d,_,_=run('Optimize this SQL query for latency', [('sqlite3 db "EXPLAIN QUERY PLAN select * from t"',0,'SCAN t')], tmp_path)
    assert d.result==0

def test_query_optimize_correct_rows_without_performance_is_not_success(tmp_path):
    d,_,_=run('Make query faster and return correct rows', [('sqlite3 db < solution.sql',0,'correct rows')], tmp_path)
    assert d.result==0

def test_sqlite_export_path_does_not_satisfy_clean_environment(tmp_path):
    d,_,_=run('Install sqlite3 command so it is available on PATH', [('export PATH="/app/sqlite:$PATH" && which sqlite3',0,'/app/sqlite/sqlite3')], tmp_path)
    assert d.result==0

def test_sqlite_clean_subprocess_required(tmp_path):
    d,_,_=run('Install sqlite3 command so it is available on PATH', [('env -i PATH=/usr/bin:/bin sqlite3 --version',0,'3.1')], tmp_path)
    assert d.result==1

def test_mcmc_inline_rscript_does_not_satisfy_analysis_script(tmp_path):
    d,_,_=run('Create analysis.R and make analysis.R run', [('Rscript -e "print(1)"',0,'1')], tmp_path, {'analysis.R':'print(1)'})
    assert d.result==0

def test_mcmc_analysis_r_must_run(tmp_path):
    d,_,_=run('Create analysis.R and make analysis.R run', [('Rscript analysis.R',0,'ok')], tmp_path, {'analysis.R':'print("ok")'})
    assert d.result==1

def test_cancel_async_source_code_not_enough_for_sigint(tmp_path):
    d,_,_=run('Handle SIGINT cancellation in the CLI subprocess', [('grep -R SIGINT .',0,'signal handler')], tmp_path)
    assert d.result==0

def test_cancel_async_internal_selftest_not_enough(tmp_path):
    d,_,_=run('Handle SIGINT cancellation in the CLI subprocess', [('pytest tests/test_cancel_helper.py',0,'1 passed internal helper')], tmp_path)
    assert d.result==0

def test_external_runtime_behavior_positive(tmp_path):
    d,_,_=run('Handle SIGINT cancellation in the CLI subprocess', [('python test_cli.py && kill -INT 123',0,'subprocess observed SIGINT cleanup')], tmp_path)
    assert d.result==1

def test_overfull_hbox_latex_success_not_enough_without_synonym_check(tmp_path):
    d,_,_=run('Only replace words using synonyms.txt to fix overfull hbox in paper.tex', [('pdflatex paper.tex',0,'Output written')], tmp_path, {'paper.tex':'hello'})
    assert d.result==0

def test_overfull_hbox_invalid_word_change_blocks_success(tmp_path):
    o=tmp_path/'o'; f=tmp_path/'f'; o.mkdir(); f.mkdir(); (o/'synonyms.txt').write_text('big large\n'); (f/'synonyms.txt').write_text('big large\n'); (o/'paper.tex').write_text('big\n'); (f/'paper.tex').write_text('enormous\n')
    dr=DebugRun(debugDir=str(f), objective='Only replace words using synonyms.txt in paper.tex', commands=[CommandRecord(command='pdflatex paper.tex', exitCode=0, stdout='ok')])
    c=build_task_contract(dr.objective,o,f,dr); e=build_contract_evidence(c,o,f,dr); d=adjudicate_contract(c,e,{'result':1})
    assert d.result==0 and any(b['bestAvailableEvidence']=='contradictory' for b in d.blockedRequirements)

def test_constrained_edit_positive(tmp_path):
    o=tmp_path/'o2'; f=tmp_path/'f2'; o.mkdir(); f.mkdir(); (o/'synonyms.txt').write_text('large\n'); (f/'synonyms.txt').write_text('large\n'); (o/'paper.tex').write_text('big\n'); (f/'paper.tex').write_text('large\n')
    dr=DebugRun(debugDir=str(f), objective='Only replace words using synonyms.txt in paper.tex')
    c=build_task_contract(dr.objective,o,f,dr); e=build_contract_evidence(c,o,f,dr); d=adjudicate_contract(c,e,{'result':1})
    assert d.result==1

def test_performance_benchmark_evidence_is_equivalent(tmp_path):
    d,_,_=run('Optimize query benchmark latency', [('make benchmark',0,'baseline 100ms median 50ms speedup')], tmp_path)
    assert d.result==1

def test_llm_success_downgraded_when_contract_evidence_missing(tmp_path):
    d,_,_=run('Optimize query latency benchmark', [], tmp_path)
    assert d.result==0 and d.downgradedFromLlmSuccess

def test_malformed_requirement_results_does_not_crash():
    obj=_parse(json.dumps({'result':1,'verdict':'success','confidence':.8,'recommendedAction':'accept','reason':'ok','requirementResults':['looks good']}))
    assert obj['result']==0 and obj['recommendedAction']=='inspect_manually'
    assert 'Malformed LLM requirement output' in obj['reason']

def test_inspect_manually_with_missing_material_evidence_is_result_zero(tmp_path):
    d,_,_=run('Make analysis.R run', [], tmp_path, {'analysis.R':'print(1)'})
    assert d.result==0 and d.recommendedAction=='inspect_manually'
