from __future__ import annotations
from villani_ops.runners.base import RunnerContext, RunnerResult
class PiRunner:
    name='pi'
    def run(self, context: RunnerContext) -> RunnerResult:
        raise NotImplementedError('Runner pi is not supported yet')
