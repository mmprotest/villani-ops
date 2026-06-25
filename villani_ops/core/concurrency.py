from __future__ import annotations

import threading
from typing import Callable, Mapping, TypeVar

from villani_ops.core.backend import Backend

T = TypeVar("T")


class BackendConcurrencyLimiter:
    """Per-backend concurrency limiter for runner/LLM backend use."""

    def __init__(self, backends: Mapping[str, Backend]):
        self._locks = {
            name: threading.BoundedSemaphore(value=max(1, backend.max_parallel))
            for name, backend in backends.items()
        }

    def run(self, backend_name: str, fn: Callable[[], T]) -> T:
        sem = self._locks[backend_name]
        with sem:
            return fn()
