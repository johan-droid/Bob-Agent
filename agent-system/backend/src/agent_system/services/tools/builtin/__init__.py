"""First-party capability groups.

Registration order is part of the contract: it determines the prompt catalog
and the ``capabilities_list`` output, so it stays stable across runs.

| group | capabilities |
| --- | --- |
| filesystem | read/write/list/search/edit/patch/diff/tree/metadata |
| coding | detect/search/read/edit/patch + test/lint/typecheck/format/inspect |
| git | read, write and destructive (default-deny) operations |
| shell | one gated arbitrary-command capability |
| browser | interactive Playwright sessions (isolated from research) |
| research | search/fetch/extract/citations/metadata/compare |
| documents | validate/create/inspect/extract/convert/persist |
| memory | recall/remember (memory is data, never executable) |
| tasks | read-only task inspection |
| system | capability inventory + system status |
"""

from __future__ import annotations

from typing import Any

from agent_system.services.tools.registry import ToolRegistry

from . import (
    browser,
    coding,
    documents,
    filesystem,
    git,
    memory,
    research,
    shell,
    system,
    tasks,
)

#: Deterministic registration order (see module docstring).
GROUPS = (
    filesystem,
    coding,
    git,
    shell,
    browser,
    research,
    documents,
    memory,
    tasks,
    system,
)


def register_first_party(registry: ToolRegistry, settings: Any) -> None:
    """Register every first-party group into ``registry``."""
    for module in GROUPS:
        module.register(registry, settings)


__all__ = ["GROUPS", "register_first_party"]
