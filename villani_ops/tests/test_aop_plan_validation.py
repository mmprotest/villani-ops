from villani_ops.orchestration.planner import DecompositionResult, Subtask, validate_decomposition_plan
from villani_ops.core.backend import Backend


def _backs():
    return {
        'reviewer': Backend(name='reviewer', provider='local', model='r', capability_score=99, roles=['review']),
        'coder': Backend(name='coder', provider='local', model='c', capability_score=10, roles=['coding'], max_parallel=2),
    }


def _good():
    return DecompositionResult(should_use_decomposition=True, reason='separable', subtasks=[
        Subtask(id='a', title='A', objective='implement alpha requirement', success_criteria='alpha requirement works', relevant_files=['a.py'], can_run_parallel=True),
        Subtask(id='b', title='B', objective='implement beta requirement', success_criteria='beta requirement works', relevant_files=['b.py'], can_run_parallel=True),
    ])


def test_plan_validation_accepts_good_decomposition():
    assert validate_decomposition_plan(_good(), task='alpha beta', success_criteria='alpha beta', backends=_backs()).accepted


def test_plan_validation_rejects_redundant_decomposition():
    d=_good(); d.subtasks[1].objective=d.subtasks[0].objective
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted and not v.non_redundancy.passed


def test_plan_validation_rejects_invalid_dependencies_and_parallel_layout():
    d=_good(); d.subtasks[1].dependencies=['missing']; d.subtasks[1].relevant_files=['a.py']
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted
    assert not v.dependency_validity.passed
    assert not v.parallel_safety.passed


def test_plan_validation_backend_fit_is_role_aware():
    d=_good(); d.subtasks[0].assigned_backend='reviewer'
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted and not v.backend_fit.passed
