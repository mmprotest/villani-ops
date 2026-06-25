from __future__ import annotations
from villani_ops.runners.base import RunnerContext, RunnerResult
class AiderRunner:
    name='aider'
    def run(self, context: RunnerContext) -> RunnerResult:
        raise NotImplementedError('Runner aider is not supported yet')
