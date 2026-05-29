"""Hermes Agent bridge — inference, memory sync, and skills (GitHub issue #22).

Phase 0 ships the thin HTTP client only. The runtime bridge (Phase 1), memory
bridge (Phase 2), and skill import + gating (Phase 3) build on top of it.
"""

from palmtop.hermes.client import HermesAPIError, HermesClient

__all__ = ["HermesAPIError", "HermesClient"]
