from pathlib import Path
import json
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.core.acceptance import is_attempt_acceptance_eligible
from villani_ops.core.backend import Backend
from villani_ops.core.task import TaskClassification
from villani_ops.policy_engine.engine import PolicyEngine
from villani_ops.llm.client import LLMCallResult

runner=CliRunner()

def test_acceptance_requires_successful_runner_and_passing_review(tmp_path):
    patch=tmp_path/"diff.patch"; patch.write_text("diff --git a/a b/a")
    base={'status':'validated','exit_code':0,'patch_path':str(patch),'changed_files':['hello.txt'],'review':{'passed':True,'decision':'pass','recommended_action':'accept'}}
    assert is_attempt_acceptance_eligible(base)[0]
    bad={**base,'exit_code':1,'patch_path':str(patch)}
    ok, blockers=is_attempt_acceptance_eligible(bad)
    assert not ok and any('exit code' in b for b in blockers)
    bad={**base,'review':{'passed':False,'decision':'fail','recommended_action':'fail'}}
    assert not is_attempt_acceptance_eligible(bad)[0]
    bad={**base,'review':{'passed':False,'decision':'uncertain','recommended_action':'ask_human'}}
    assert not is_attempt_acceptance_eligible(bad)[0]
    human={**bad,'status':'human_approved','patch_path':str(patch),'changed_files':['hello.txt'],'human_approval':{'decision':'accept'}}
    assert not is_attempt_acceptance_eligible(human)[0]
    valid={**human,'human_approval':{'decision':'accept','valid_override':True,'requested':True,'prompted':True,'skipped_reason':None,'request_reasons':['reviewer_recommended_ask_human'],'shown_evidence':{'patch_path':str(patch),'changed_files':['hello.txt'],'reviewer_decision':'uncertain','acceptance_blockers':['review decision is uncertain']}}}
    assert is_attempt_acceptance_eligible(valid)[0]

def _backend(name, roles=('coding',), cap=10, in_cost=1, out_cost=1, enabled=True):
    return Backend(name=name, provider='local', model=name, roles=list(roles), capability_score=cap, input_cost_per_million=in_cost, output_cost_per_million=out_cost, enabled=enabled)

class FakeClient:
    def __init__(self, attempts): self.attempts=attempts
    def complete_json(self, *args, **kwargs):
        return LLMCallResult(raw_text='{}', parsed_json={'profile':'cheap','attempts':self.attempts}, input_tokens=1, output_tokens=1, estimated_cost=0, backend_name='policy', model='m')

def test_policy_normalizes_cheap_and_filters_disabled_non_coding():
    backs={'cheap':_backend('cheap', cap=10, in_cost=0, out_cost=0), 'exp':_backend('exp', cap=90, in_cost=10, out_cost=10), 'off':_backend('off', enabled=False), 'review':_backend('review', roles=('policy',), cap=99)}
    cls=TaskClassification(difficulty='easy', category='bugfix', risk='low')
    strat,_=PolicyEngine(FakeClient([{'backend':'exp','max_attempts':1},{'backend':'off','max_attempts':1},{'backend':'review','max_attempts':1}])).generate(cls, backs, 'cheap')
    assert strat.attempts[0].backend=='cheap'
    assert all(a.backend in {'cheap','exp'} for a in strat.attempts)
    assert strat.warnings

def test_quality_hard_starts_highest_capability():
    backs={'cheap':_backend('cheap', cap=10, in_cost=0, out_cost=0), 'exp':_backend('exp', cap=90, in_cost=10, out_cost=10), 'policy':_backend('policy', roles=('policy',), cap=1)}
    cls=TaskClassification(difficulty='hard', category='bugfix', risk='high')
    strat,_=PolicyEngine(FakeClient([{'backend':'cheap','max_attempts':1}])).generate(cls, backs, 'quality')
    assert strat.attempts[0].backend=='exp'

def test_filestorage_workspace_is_absolute(tmp_path):
    from villani_ops.storage.files import FileStorage
    s=FileStorage(tmp_path/'rel')
    assert s.workspace.is_absolute()
