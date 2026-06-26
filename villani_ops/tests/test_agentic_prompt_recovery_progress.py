from io import StringIO
from rich.console import Console
from villani_ops.agentic.prompts import SYSTEM_PROMPT
from villani_ops.agentic.recovery import handle_no_tool_call
from villani_ops.agentic.progress import AgenticProgressReporter
from villani_ops.agentic.event_recorder import OpsEvent
from villani_ops.agentic.state import SubtaskState
from villani_ops.tests.test_agentic_tools import state


def test_prompts_discourage_xml_pseudo_tool_calls(tmp_path):
    assert '<tool_call>' in SYSTEM_PROMPT
    assert '<function=...>' in SYSTEM_PROMPT
    s=state(tmp_path)
    rr=handle_no_tool_call(s, max_recovery_attempts=3)
    assert 'Do not write XML-style tool calls' in rr.message['content']
    assert '<tool_call>' in rr.message['content']


def test_recovery_prompt_names_likely_select_path_tool(tmp_path):
    s=state(tmp_path)
    s.investigation={'summary':'i'}; s.plan={'summary':'p','should_decompose':True}
    s.decomposition_validated=True; s.decomposition_accepted=True; s.execution_path='unknown'
    s.subtasks=[SubtaskState(subtask_id='s0',title='s0',objective='o'),SubtaskState(subtask_id='s1',title='s1',objective='o')]
    rr=handle_no_tool_call(s, max_recovery_attempts=0)
    assert rr.should_fail is False
    assert 'Call ops_select_execution_path with path="decomposed_subtasks"' in rr.message['content']


def test_progress_reports_deterministic_recovery_action():
    buf=StringIO(); console=Console(file=buf, force_terminal=False, color_system=None)
    reporter=AgenticProgressReporter(enabled=True, console=console)
    reporter.on_event(OpsEvent(event_id='e', run_id='r', timestamp='2026-06-26T00:00:00+00:00', type='recovery_deterministic_action_executed', payload={'tool_name':'ops_select_execution_path','tool_input':{'path':'decomposed_subtasks'},'reason':'Accepted decomposition has no selected execution path.'}))
    out=buf.getvalue()
    assert 'Recovery selected execution path: decomposed_subtasks' in out
