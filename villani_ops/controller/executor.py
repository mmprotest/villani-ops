from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.controller.progress import RunProgressReporter
from villani_ops.execution_policies import policy_for_mode
from villani_ops.runners import runner_for_name
from villani_ops.orchestration.engine import OrchestrationEngine, RunResult

class VillaniOps:
    def __init__(self, storage: FileStorage, progress_reporter=None):
        self.storage=storage; self.progress_reporter=progress_reporter or RunProgressReporter(False)
    @classmethod
    def from_workspace(cls, path: str | Path = '.villani-ops') -> 'VillaniOps': return cls(FileStorage(Path(path).expanduser().resolve()))
    def run(self, repo: str|Path, task: Task, candidate_attempts: int=3, timeout_seconds: int|None=None, classify: bool=True, non_interactive: bool=False, isolation: str='worktree', mode: str='performance', runner: str='villani-code') -> RunResult:
        if candidate_attempts < 1 or candidate_attempts > 8: raise ValueError('candidate_attempts must be between 1 and 8')
        if mode not in {'performance','cheap','balanced','quality'}: raise ValueError('mode must be one of: performance, cheap, balanced, quality')
        adapter=runner_for_name(runner)
        if runner != 'villani-code': raise ValueError(f"Runner '{runner}' is registered but not implemented yet.")
        self.storage.init_workspace(); backends=self.storage.load_backends()
        engine=OrchestrationEngine(backends=backends, execution_policy=policy_for_mode(mode), runner_adapter=adapter, workspace=self.storage.workspace, non_interactive=non_interactive, progress_reporter=self.progress_reporter, storage=self.storage)
        return engine.run(repo=repo, task=task, candidate_attempts=candidate_attempts, timeout_seconds=timeout_seconds, classify=classify, isolation=isolation)
