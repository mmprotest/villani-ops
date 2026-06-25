import pytest
from villani_ops.core.backend import Backend
from villani_ops.execution_policies.base import required_role_for_node, select_backend_for_role
from villani_ops.orchestration.nodes import OrchestrationNode


def b(name, roles, score=1):
    return Backend(name=name, provider='local', model=name, roles=roles, capability_score=score)


def test_selection_role_preferred_over_higher_review():
    name,_,reason=select_backend_for_role('selection','policy', {'a':b('a',['selection'],1), 'r':b('r',['review'],99)})
    assert name == 'a' and 'Filtered by required role selection' in reason


def test_selection_fallback_to_review_logged():
    name,_,reason=select_backend_for_role('selection','policy', {'r':b('r',['review'],2)})
    assert name == 'r'
    assert 'requested_role=selection' in reason and 'fallback_role=review' in reason and 'selected_backend=r' in reason


def test_selection_fallback_to_policy_logged():
    name,_,reason=select_backend_for_role('selection','policy', {'p':b('p',['policy'],2)})
    assert name == 'p'
    assert 'fallback_role=policy' in reason and 'selected_backend=p' in reason


def test_no_valid_fallback_fails_clearly():
    with pytest.raises(ValueError, match='selection'):
        select_backend_for_role('selection','policy', {'c':b('c',['coding'],99)})


def test_plan_decompose_use_policy_not_planning():
    assert required_role_for_node(OrchestrationNode(id='plan', kind='plan', objective='')) == 'policy'
    assert required_role_for_node(OrchestrationNode(id='decompose', kind='decompose', objective='')) == 'policy'


def test_coding_does_not_fallback_to_review_or_policy():
    with pytest.raises(ValueError, match='coding'):
        select_backend_for_role('coding','policy', {'r':b('r',['review','policy'],99)})
