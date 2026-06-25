from villani_ops.orchestration.progress import ProgressReporter, ConsoleProgressReporter, NullProgressReporter

class RunProgressReporter(ConsoleProgressReporter):
    def __init__(self, enabled: bool = True, verbose: bool = False):
        self.enabled = enabled; self.verbose = verbose
    def _print(self, msg: str = "") -> None:
        if self.enabled: print(msg, flush=True)
    def info(self, message: str) -> None: self._print(message)
    def warning(self, message: str) -> None: self._print(f"Warning: {message}")
    def step(self, message: str) -> None: self._print(message)
