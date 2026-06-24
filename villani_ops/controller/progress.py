from __future__ import annotations

class RunProgressReporter:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
    def info(self, message: str) -> None:
        if self.enabled:
            print(message, flush=True)
    def warning(self, message: str) -> None:
        self.info(f"Warning: {message}")
    def step(self, message: str) -> None:
        self.info(message)
