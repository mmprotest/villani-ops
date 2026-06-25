from __future__ import annotations

from collections.abc import Mapping

from villani_ops.core.backend import Backend


def select_performance_backend(backends: Mapping[str, Backend]) -> tuple[str, Backend]:
    """Select the single backend used by performance orchestration.

    Considers only enabled backends and ranks solely by capability score, with a
    deterministic backend-name tie break. Pricing, roles, and policy profiles are
    intentionally ignored.
    """
    enabled = [(name, backend) for name, backend in backends.items() if backend.enabled]
    if not enabled:
        raise ValueError("No enabled backend configured for performance orchestration")
    return sorted(enabled, key=lambda item: (-item[1].capability_score, item[0]))[0]
