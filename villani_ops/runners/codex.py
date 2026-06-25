from __future__ import annotations
from villani_ops.runners.base import RunnerContext, RunnerResult
class CodexRunner:
    name='codex'
    def run(self, context: RunnerContext) -> RunnerResult:
        raise NotImplementedError('Runner codex is not supported yet')
