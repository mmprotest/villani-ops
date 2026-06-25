from __future__ import annotations
from villani_ops.runners.base import RunnerContext, RunnerResult
class ClaudeCodeRunner:
    name='claude-code'
    def run(self, context: RunnerContext) -> RunnerResult:
        raise NotImplementedError('Runner claude-code is not supported yet')
