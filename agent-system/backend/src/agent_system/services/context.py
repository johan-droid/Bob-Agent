"""Context management (v3.1 §11).

The ReAct loop accumulates tool results until the transcript approaches the
model's context window. The old behaviour dropped the oldest results and kept a
fixed number of newest ones, which is exactly the wrong policy: the oldest
result is often the *reason* the agent is in this loop (an error, a failing
test, a constraint), while the newest is often a bulky but low-value listing.

:class:`ContextManager` replaces that with explicit prioritisation:

    token estimation · context budget · raw-result truncation ·
    recent-result retention · important-result retention ·
    tool-result prioritisation · system-message preservation

Retention rules:

1. **System messages and the task are never part of compaction.** They are
   passed in separately and never candidates for dropping.
2. **Important results are pinned** for the life of the run — errors, failures,
   file changes, test/lint results, approval outcomes and decisions that the
   agent must keep acting on.
3. **The most recent N results are retained** regardless of importance: the
   agent is actively reasoning about them.
4. **Only low-value raw output is compacted**, oldest first, and every
   compaction states in the summary which results were preserved and why.

The chars/4 heuristic remains the token estimator (a fallback, documented as an
approximation) — swapping in a real tokenizer is a one-function change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TRUNCATION_MARKER = "\n…[truncated]"

#: Names/payload keys that make a result important enough to pin.
_IMPORTANT_KEYS = (
    "error",
    "errors",
    "detail",
    "failure",
    "failed",
    "tests_failed",
    "exit_code",
    "approval_id",
    "denied",
    "permission_denied",
    "diff",
    "changed",
)
#: Capability groups whose results are decisions/state the agent must not lose.
_IMPORTANT_TOOLS = frozenset(
    {
        "git_status",
        "git_diff",
        "git_log",
        "run_tests",
        "run_linter",
        "run_typecheck",
        "run_formatter",
        "file_edit",
        "file_patch",
        "edit_source",
        "apply_patch",
        "document_persist",
        "task_status",
        "tasks_inspect",
    }
)

IMPORTANCE_LOW = 0
IMPORTANCE_NORMAL = 1
IMPORTANCE_HIGH = 2
IMPORTANCE_CRITICAL = 3


def estimate_tokens(text: str) -> int:
    """chars/4 heuristic — documented approximation, never used for billing."""
    return max(1, len(text) // 4)


def classify_importance(name: str, result: dict[str, Any]) -> int:
    """Score one tool result's retention value."""
    if not isinstance(result, dict):
        return IMPORTANCE_NORMAL
    if result.get("error") or result.get("denied"):
        return IMPORTANCE_CRITICAL
    if result.get("permission_denied") or result.get("approval_id"):
        return IMPORTANCE_CRITICAL
    if result.get("changed") is True or result.get("diff"):
        return IMPORTANCE_HIGH
    if result.get("exit_code") not in (None, 0) or result.get("ok") is False:
        return IMPORTANCE_HIGH
    if name in _IMPORTANT_TOOLS:
        return IMPORTANCE_HIGH
    if any(key in result for key in _IMPORTANT_KEYS):
        return IMPORTANCE_HIGH
    return IMPORTANCE_LOW


@dataclass
class ContextBlock:
    """One rendered ``<tool_result>`` block plus its retention metadata."""

    name: str
    text: str
    tokens: int
    importance: int
    sequence: int

    @property
    def pinned(self) -> bool:
        return self.importance >= IMPORTANCE_HIGH


@dataclass
class CompactionResult:
    """What a compaction pass did (fed to the ``context.compacted`` event)."""

    transcript: str
    dropped_count: int
    kept_count: int
    tokens_saved: int
    retained_important: list[str] = field(default_factory=list)
    dropped_names: list[str] = field(default_factory=list)


class ContextManager:
    """Track tool-result blocks and compact low-value ones under budget."""

    def __init__(
        self,
        *,
        max_context_tokens: int = 100_000,
        compaction_threshold_pct: float = 75.0,
        keep_recent: int = 3,
        max_result_chars: int = 4000,
    ) -> None:
        self.max_context_tokens = max(1, int(max_context_tokens))
        self.threshold = self.max_context_tokens * (float(compaction_threshold_pct) / 100.0)
        self.keep_recent = max(1, int(keep_recent))
        self.max_result_chars = max(200, int(max_result_chars))
        self._blocks: list[ContextBlock] = []
        self._sequence = 0
        self.compactions = 0

    # -- accounting ---------------------------------------------------------

    @property
    def estimated_tokens(self) -> int:
        return sum(block.tokens for block in self._blocks)

    @property
    def blocks(self) -> list[ContextBlock]:
        return list(self._blocks)

    def observe(
        self, block: str, *, name: str, result: dict[str, Any] | None = None
    ) -> ContextBlock:
        """Record one rendered result block; oversized results are truncated."""
        text = block
        if len(text) > self.max_result_chars:
            text = text[: self.max_result_chars] + TRUNCATION_MARKER
        self._sequence += 1
        entry = ContextBlock(
            name=name,
            text=text,
            tokens=estimate_tokens(text),
            importance=classify_importance(name, result or {}),
            sequence=self._sequence,
        )
        self._blocks.append(entry)
        return entry

    # -- compaction ---------------------------------------------------------

    def should_compact(self) -> bool:
        return self.estimated_tokens > self.threshold and len(self._blocks) > 1

    def compact(self, transcript: str) -> CompactionResult | None:
        """Drop low-value results until the budget is met; None if not needed."""
        if not self.should_compact():
            return None
        recent = {block.sequence for block in self._blocks[-self.keep_recent :]}
        victims: list[ContextBlock] = []
        kept: list[ContextBlock] = []
        running = self.estimated_tokens
        # Oldest first, never a pinned or recent block.
        for block in self._blocks:
            if running <= self.threshold:
                kept.append(block)
                continue
            if block.pinned or block.sequence in recent:
                kept.append(block)
                continue
            victims.append(block)
            running -= block.tokens
        if not victims:
            # Everything left is pinned; compact the least important pinned-but
            # non-recent block so the run can still make progress.
            candidates = [
                block
                for block in self._blocks
                if block.sequence not in recent and block not in kept
            ]
            if not candidates:
                return None
            victims = [min(candidates, key=lambda b: (b.importance, b.sequence))]
            kept = [block for block in self._blocks if block not in victims]
            running = sum(block.tokens for block in kept)
        for block in victims:
            transcript = transcript.replace(block.text, "", 1)
        retained = [block.name for block in self._blocks if block.pinned and block not in victims]
        summary = self._summary(victims, retained)
        transcript += summary
        self._blocks = kept
        self.compactions += 1
        return CompactionResult(
            transcript=transcript,
            dropped_count=len(victims),
            kept_count=len(kept),
            tokens_saved=sum(block.tokens for block in victims) - estimate_tokens(summary),
            retained_important=sorted(set(retained)),
            dropped_names=[block.name for block in victims],
        )

    @staticmethod
    def _summary(victims: list[ContextBlock], retained: list[str]) -> str:
        dropped = ", ".join(sorted({block.name for block in victims}))
        kept_note = (
            f" Important results were kept: {', '.join(sorted(set(retained)))}." if retained else ""
        )
        return (
            '\n\n<tool_result name="context-summary">\n'
            f"Compacted {len(victims)} low-value raw result(s) to stay within the context "
            f"budget: {dropped}. Re-run a capability if you need that raw output again."
            f"{kept_note}\n"
            "</tool_result>"
        )


__all__ = [
    "sanitize_context",
    "ContextBlock",
    "ContextManager",
    "CompactionResult",
    "IMPORTANCE_CRITICAL",
    "IMPORTANCE_HIGH",
    "IMPORTANCE_LOW",
    "IMPORTANCE_NORMAL",
    "TRUNCATION_MARKER",
    "classify_importance",
    "estimate_tokens",
]


def sanitize_context(obj: Any) -> Any:
    """Recursively sanitize raw credentials out of LLM context objects/dicts."""
    from agent_system.services.secrets import redact_dict, redact_value

    if isinstance(obj, dict):
        return redact_dict(obj)
    if isinstance(obj, str):
        return redact_value(obj)
    if isinstance(obj, list):
        return [sanitize_context(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_context(item) for item in obj)
    return obj
