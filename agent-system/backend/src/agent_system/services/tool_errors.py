"""Tool error taxonomy (import-safe).

Kept in its own module so the permission layer and the capability layer can
both raise the same exception types without an import cycle:

    services/tool_errors.py  <-  services/tools/*  (raises)
                             <-  services/permissions.py (raises)
"""

from __future__ import annotations


class ToolError(RuntimeError):
    """A tool ran and failed (reported back to the model)."""


class ToolValidationError(ToolError):
    """Arguments failed schema validation and never reached the handler.

    Carries a structured, model-readable report so the model can correct
    itself instead of guessing why the call was rejected.
    """

    def __init__(self, tool_name: str, errors: list[str]) -> None:
        self.tool_name = tool_name
        self.errors = errors
        super().__init__(f"invalid arguments for '{tool_name}': " + "; ".join(errors))

    def payload(self) -> dict[str, object]:
        return {
            "error": "invalid_arguments",
            "tool": self.tool_name,
            "detail": self.errors,
            "hint": "Fix the arguments to match the tool schema and call it again.",
        }


class ToolPermissionError(ToolError):
    """A capability was refused by the permission layer (default-deny)."""


class NeedsApprovalError(ToolError):
    """Raised when a capability needs an approval first.

    Carries the persisted approval id so the runner surfaces it and the user
    can approve + retry. ``denied=True`` means the request was refused outright
    (dangerous/default-deny scope) rather than merely waiting on a human.
    """

    def __init__(
        self,
        approval_id: str,
        action: str,
        *,
        denied: bool = False,
        reason: str | None = None,
    ) -> None:
        self.approval_id = approval_id
        self.action = action
        self.denied = denied
        self.reason = reason
        verb = "denied by permission gate" if denied else "awaiting approval"
        suffix = f" ({reason})" if reason else ""
        super().__init__(f"{verb} {approval_id} for: {action}{suffix}")


__all__ = [
    "NeedsApprovalError",
    "ToolError",
    "ToolPermissionError",
    "ToolValidationError",
]
