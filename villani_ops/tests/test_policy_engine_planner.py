import pytest
from typer.testing import CliRunner
from villani_ops.core.backend import Backend
from villani_ops.core.task import TaskClassification
from villani_ops.policy_engine.planner import (
    estimate_required_capability,
    estimate_backend_solve_probability,
    plan_execution_strategy,
)
from villani_ops.cli.main import app


def b(name, cap, cost=1.0, roles=("coding",), enabled=True):
    return Backend(name=name, provider="openai-compatible", base_url="http://x/v1", model=name, api_key="dummy", roles=list(roles), enabled=enabled, capability_score=cap, input_cost_per_million=cost, output_cost_per_million=cost)


def test_max_attempts_is_separate_from_policy():
    strat=plan_execution_strategy({"small":b("small",35,.1), "large":b("large",70,2)}, TaskClassification(difficulty="medium", risk="low", category="bug_fix"), "cheap", max_attempts=3)
    assert strat.max_attempts == 3
    assert 1 <= len(strat.attempts) <= 3


def test_cheap_starts_with_cheapest_viable_backend():
    backs={"small":b("small",20,.01), "medium":b("medium",45,.1), "large":b("large",80,1)}
    strat=plan_execution_strategy(backs, TaskClassification(difficulty="easy", risk="low"), "cheap", max_attempts=2)
    assert strat.attempts[0].backend == "small"


def test_cheap_skips_hopeless_backend():
    backs={"tiny":b("tiny",5,.001), "medium":b("medium",45,.1), "large":b("large",80,1)}
    strat=plan_execution_strategy(backs, TaskClassification(difficulty="medium", risk="medium"), "cheap", max_attempts=2)
    assert strat.attempts[0].backend != "tiny"
    assert strat.attempts[0].backend == "medium"


def test_cheap_retry_threshold():
    cls=TaskClassification(difficulty="medium", risk="low", category="bug_fix")
    strat=plan_execution_strategy({"small":b("small",35,.01), "large":b("large",70,1)}, cls, "cheap", max_attempts=2)
    # required=40, small gap=-5, p=.40, below .45 and gap<0, so escalate.
    assert [a.backend for a in strat.attempts] == ["small", "large"]


def test_balanced_starts_cheap_but_escalates_sooner():
    backs={"qwen9b":b("qwen9b",24,.01), "qwen35b":b("qwen35b",51,.1)}
    cls=TaskClassification(difficulty="medium", risk="low", category="bug_fix")
    strat=plan_execution_strategy(backs, cls, "balanced", max_attempts=2)
    assert [a.backend for a in strat.attempts] == ["qwen9b", "qwen35b"]


def test_quality_uses_strongest_backend_repeatedly():
    backs={"qwen9b":b("qwen9b",24,.01), "qwen35b":b("qwen35b",51,.1)}
    strat=plan_execution_strategy(backs, TaskClassification(), "quality", max_attempts=3)
    assert [a.backend for a in strat.attempts] == ["qwen35b", "qwen35b", "qwen35b"]


def test_required_capability_estimate():
    assert estimate_required_capability(TaskClassification(difficulty="easy", risk="low", category="bug_fix")) == 20
    assert estimate_required_capability(TaskClassification(difficulty="medium", risk="low", category="bug_fix")) == 40
    assert estimate_required_capability(TaskClassification(difficulty="hard", risk="high", category="security")) == 80


def test_solve_probability_increases_with_capability():
    cls=TaskClassification(difficulty="medium", risk="medium", category="bug_fix")
    req=estimate_required_capability(cls)
    ps=[estimate_backend_solve_probability(b(str(c), c), cls, req) for c in (20,50,80)]
    assert ps[0] < ps[1] < ps[2]


def test_policy_plan_includes_estimates():
    strat=plan_execution_strategy({"small":b("small",35,.01)}, TaskClassification(), "balanced", max_attempts=1)
    a=strat.attempts[0]
    assert a.estimated_solve_probability is not None
    assert a.estimated_attempt_cost is not None
    assert a.required_capability is not None
    assert a.capability_gap is not None


def test_no_enabled_coding_backend():
    with pytest.raises(ValueError, match="No enabled coding backends available"):
        plan_execution_strategy({"review":b("review",90,1,roles=("review",))}, TaskClassification(), "balanced")


def test_cli_accepts_max_attempts_with_mocked_run(monkeypatch, tmp_path):
    class D:
        accepted=False; final_state='failed'; final_action='fail'; classification={'difficulty':'medium','category':'bug_fix','risk':'low'}; execution_strategy={'max_attempts':4,'attempts':[{},{}]}; attempts_used=0; retries_used=0; escalations_used=0; human_reviews_requested=0; human_reviews_skipped=0; winning_attempt_id=None; reviewer_decision=None; reviewer_score=None; human_override_used=False; reason='x'; total_cost=0; reviewer_evidence=[]; run_id='r'; failure_reason='x'; attempts=[]
    def fake_run(self, **kwargs):
        assert kwargs['max_attempts'] == 4
        return type('R', (), {'decision':D(), 'report_path':'/tmp/report.md'})()
    monkeypatch.setattr('villani_ops.controller.executor.VillaniOps.run', fake_run)
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'.git').mkdir()
    res=CliRunner().invoke(app, ['run','--repo',str(repo),'--task','x','--policy','balanced','--max-attempts','4','--non-interactive'])
    assert res.exit_code == 0
    assert 'Max attempts: 4' in res.output
