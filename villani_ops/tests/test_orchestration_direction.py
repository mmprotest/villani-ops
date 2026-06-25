from pathlib import Path
from typer.testing import CliRunner
import pytest
from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.execution_policies import policy_for_mode
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from villani_ops.orchestration.planner import build_fixed_graph
from villani_ops.orchestration.scheduler import GraphScheduler

runner=CliRunner()

def backs():
    return {
        'a-low': Backend(name='a-low', provider='local', model='s', capability_score=1, input_cost_per_million=1000, output_cost_per_million=1000),
        'b-mid': Backend(name='b-mid', provider='local', model='m', capability_score=5),
        'c-high': Backend(name='c-high', provider='local', model='l', capability_score=10, roles=['review']),
    }

def test_cli_modes_and_legacy_rejections(tmp_path):
    for mode in ['performance','cheap','balanced','quality']:
        r=runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--mode',mode,'--runner','claude-code'])
        assert mode in r.output or 'registered but not implemented yet' in r.output
    assert runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--mode','bad']).exit_code != 0
    assert '--policy has been replaced by --mode' in runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--policy','cheap']).output
    assert 'Backend assignment is controlled by the execution policy' in runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--backend','x']).output
    assert 'Human approval is not supported in the primary orchestration path' in runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--human-approval']).output
    assert 'Unknown option: --bogus' in runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--bogus']).output

def test_policies_route_from_real_signals():
    b=backs(); easy=TaskContext(objective='x', classification={'difficulty':'easy','risk':'low','confidence':.95}, investigation={'confidence':.95}, plan={'expected_difficulty':'easy','confidence':.95})
    hard=TaskContext(objective='x', classification={'difficulty':'hard','risk':'high','confidence':.4}, plan={'expected_difficulty':'hard','confidence':.4})
    n=OrchestrationNode(id='code_attempt_001', kind='code', objective='code')
    assert policy_for_mode('performance').select_backend(node=n, backends=b, task_context=easy).backend_name == 'c-high'
    assert policy_for_mode('cheap').select_backend(node=n, backends=b, task_context=easy).backend_name == 'a-low'
    assert policy_for_mode('cheap').select_backend(node=OrchestrationNode(id='c', kind='code', objective=''), backends=b, task_context=hard).backend_name == 'c-high'
    assert policy_for_mode('balanced').select_backend(node=OrchestrationNode(id='c', kind='code', objective=''), backends=b, task_context=TaskContext(objective='x', classification={'difficulty':'medium','risk':'medium','confidence':.7})).backend_name == 'c-high'
    assert policy_for_mode('quality').select_backend(node=OrchestrationNode(id='r', kind='review', objective=''), backends=b, task_context=easy).backend_name == 'c-high'
    assert policy_for_mode('cheap').select_backend(node=OrchestrationNode(id='p', kind='plan', objective=''), backends=b, task_context=easy, prior_results=[NodeResult(status='failed')]).backend_name == 'c-high'
    b['c-high'].enabled=False; assert policy_for_mode('performance').select_backend(node=n, backends=b, task_context=easy).backend_name == 'b-mid'
    b['a-low'].enabled=False; b['b-mid'].enabled=False
    with pytest.raises(ValueError, match='No enabled backends'):
        policy_for_mode('performance').select_backend(node=n, backends=b, task_context=easy)

def test_graph_scheduler_and_shape(tmp_path):
    g=build_fixed_graph(2, run_id='r1', mode='performance', classify=True)
    assert [n.kind for n in g.nodes][:3] == ['classify','investigate','plan']
    assert g.get('code_attempt_001').parallel_group == 'candidate_code'
    assert g.get('review_attempt_001').dependencies == ['code_attempt_001']
    assert set(g.get('select').dependencies) == {'review_attempt_001','review_attempt_002'}
    s=GraphScheduler(); ready=s.next_ready_nodes(g); assert [n.id for n in ready] == ['classify']
    g.mark_running('classify'); g.mark_succeeded('classify', summary='ok')
    assert [n.id for n in s.next_ready_nodes(g)] == ['investigate']
    g.mark_failed('investigate','boom'); s.next_ready_nodes(g); assert g.get('plan').status == 'skipped'
    out=tmp_path/'orchestration_graph.json'; g.write(out); assert out.exists() and 'investigate' in out.read_text()

def test_readme_new_direction():
    r=Path('README.md').read_text()
    assert 'villani-ops run --mode performance' in r
    assert 'legacy compatibility command' in r
    assert 'previous cost-policy runner' in r
