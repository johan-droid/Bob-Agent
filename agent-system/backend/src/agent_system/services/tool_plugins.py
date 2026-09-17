"""Pluggable tools — folder-drop tools for the ReAct loop.

Same discovery pattern as ``SkillManager`` (hermes-style): each plugin is a
folder ``<tools_plugin_dir>/<name>/`` containing ``tool.json``::

    {
      "description": "Count words/chars/lines in a text snippet.",
      "parameters": {"type": "object", "properties": {...}, "required": [...]},
      "risk": "read",
      "handler": "handler.py:handle"
    }

plus the handler module, which must define ``handle(args: dict) -> dict``.

Safety model — identical gates as built-in tools, no exemptions:

- ``risk`` must be ``read`` | ``write`` | ``execute``; anything else is a
  validation error and the plugin never loads.
- ``execute``-risk plugins are **always** approval-gated: the built tool
  declares ``risk="execute"`` with the forced scope ``plugin:<name>``, so the
  one execution engine asks the one permission gate *before* the handler runs
  (same DB approval flow + ``NeedsApprovalError`` as ``shell`` when
  ``tools_require_approval``). No plugin-local gate exists to drift. They also
  **always** run isolated from the host: ``DockerSandbox`` locally,
  ``SubprocessJail`` when ``HEROKU_JAIL=true`` (cloud containment) — a plugin
  cannot declare itself exempt, and ``tools_shell_mode=local`` does not
  apply to plugins.
  The handler runs in the container via a staged runner (args JSON in,
  result JSON on stdout); sandbox-unavailable is an honest ``ToolError``,
  never a silent host fallback.
- ``read``/``write`` plugins run in-process like ``file_read``/``file_write``.
- Plugins can never shadow built-in tools (``build_registry`` refuses).
- Enable toggles persist in ``<tools_plugin_dir>/.state.json``; discovery
  re-reads the dir on every ``build_registry`` call, so toggling takes
  effect on the next task with no restart.
"""

from __future__ import annotations

import importlib.util
import json as _json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_FILENAME = "tool.json"
STATE_FILENAME = ".state.json"

_VALID_RISKS = ("read", "write", "execute")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Staged into the execution workspace as _runner.py alongside args.json and
# handler.py: reads args.json from its own directory (Docker: /ws, jail: the
# staging dir used as cwd), calls handle(), prints the result JSON.
# Only the last non-empty stdout line is parsed, so handler prints are safe.
_RUNNER_SOURCE = """\
import importlib.util
import json
import sys
import traceback
from pathlib import Path


def main() -> None:
    try:
        here = Path(__file__).resolve().parent
        with open(here / "args.json", encoding="utf-8") as f:
            args = json.load(f)
        handler_path = here / "handler.py"
        spec = importlib.util.spec_from_file_location("plugin_handler", handler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {handler_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.handle(args)
        if not isinstance(result, dict):
            result = {"_value": result}
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)


main()
"""


class ToolPluginError(ValueError):
    """A plugin is invalid, unknown, or refused (never silent, never fatal)."""


@dataclass
class ToolPlugin:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    handler_file: Path
    handler_func: str
    enabled: bool
    path: Path

    def load_callable(self) -> Any:
        """Import the handler module and return the ``handle`` callable."""
        spec = importlib.util.spec_from_file_location(f"tool_plugin_{self.name}", self.handler_file)
        if spec is None or spec.loader is None:
            raise ToolPluginError(f"{self.name}: cannot load {self.handler_file}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ToolPluginError(
                f"{self.name}: handler import failed: {type(exc).__name__}: {exc}"
            ) from exc
        func = getattr(module, self.handler_func, None)
        if not callable(func):
            raise ToolPluginError(
                f"{self.name}: handler '{self.handler_func}' not found/callable "
                f"in {self.handler_file.name}"
            )
        return func


class ToolPluginManager:
    """Discover + toggle + build plugin tools (mirrors SkillManager)."""

    def __init__(self, plugin_dir: Path | str) -> None:
        self.plugin_dir = Path(plugin_dir)
        self.last_errors: list[str] = []

    # -- state --------------------------------------------------------

    def _state_overrides(self) -> dict[str, Any]:
        path = self.plugin_dir / STATE_FILENAME
        try:
            data = _json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        (self.plugin_dir / STATE_FILENAME).write_text(
            _json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # -- discovery ----------------------------------------------------

    def _load_plugin(self, plugin_path: Path, state: dict[str, Any]) -> ToolPlugin:
        dirname = plugin_path.name
        if not _NAME_RE.match(dirname):
            raise ToolPluginError(f"{plugin_path}: invalid plugin folder name '{dirname}'")
        try:
            meta = _json.loads((plugin_path / TOOL_FILENAME).read_text(encoding="utf-8"))
        except Exception as exc:
            raise ToolPluginError(f"{dirname}: unreadable {TOOL_FILENAME}: {exc}") from exc
        if not isinstance(meta, dict):
            raise ToolPluginError(f"{dirname}: {TOOL_FILENAME} must be a JSON object")
        name = str(meta.get("name") or dirname)
        if name != dirname:
            raise ToolPluginError(
                f"{dirname}: tool.json name '{name}' must match folder '{dirname}'"
            )
        if not _NAME_RE.match(name):
            raise ToolPluginError(f"{dirname}: invalid tool name '{name}'")
        description = str(meta.get("description") or "").strip()
        if not description:
            raise ToolPluginError(f"{dirname}: 'description' is required")
        parameters = meta.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise ToolPluginError(f"{dirname}: 'parameters' must be a JSON schema object")
        risk = str(meta.get("risk") or "read")
        if risk not in _VALID_RISKS:
            raise ToolPluginError(f"{dirname}: invalid risk '{risk}' (expected read|write|execute)")
        handler_ref = str(meta.get("handler") or "")
        if ":" not in handler_ref:
            raise ToolPluginError(
                f"{dirname}: 'handler' must be 'file.py:function' (got '{handler_ref}')"
            )
        filename, _, func = handler_ref.partition(":")
        handler_file = (plugin_path / filename).resolve()
        try:
            inside = handler_file.is_relative_to(plugin_path.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside or not handler_file.is_file():
            raise ToolPluginError(f"{dirname}: handler file '{filename}' not found in plugin dir")
        if not func.isidentifier():
            raise ToolPluginError(f"{dirname}: invalid handler function '{func}'")
        override = state.get(name)
        enabled = True
        if isinstance(override, dict) and "enabled" in override:
            enabled = bool(override["enabled"])
        return ToolPlugin(
            name=name,
            description=description,
            parameters=parameters,
            risk=risk,
            handler_file=handler_file,
            handler_func=func,
            enabled=enabled,
            path=plugin_path,
        )

    def discover(self) -> list[ToolPlugin]:
        """Load every valid plugin; invalid ones land in ``last_errors``."""
        self.last_errors = []
        plugins: list[ToolPlugin] = []
        if not self.plugin_dir.is_dir():
            return plugins
        state = self._state_overrides()
        for child in sorted(self.plugin_dir.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if not (child / TOOL_FILENAME).is_file():
                continue
            try:
                plugins.append(self._load_plugin(child, state))
            except ToolPluginError as exc:
                self.last_errors.append(str(exc))
            except Exception as exc:  # never let one bad plugin break discovery
                self.last_errors.append(f"{child.name}: {type(exc).__name__}: {exc}")
        plugins.sort(key=lambda p: p.name)
        return plugins

    def _by_name(self) -> dict[str, ToolPlugin]:
        return {p.name: p for p in self.discover()}

    def get(self, name: str) -> ToolPlugin:
        plugin = self._by_name().get(name)
        if plugin is None:
            raise ToolPluginError(f"plugin '{name}' not found in {self.plugin_dir}")
        return plugin

    def enabled_plugins(self) -> list[ToolPlugin]:
        return [p for p in self.discover() if p.enabled]

    def set_enabled(self, name: str, enabled: bool) -> ToolPlugin:
        plugin = self.get(name)  # raises when unknown
        state = self._state_overrides()
        state[name] = {"enabled": bool(enabled)}
        self._save_state(state)
        plugin.enabled = bool(enabled)
        return plugin

    # -- build --------------------------------------------------------

    def build_tools(self) -> list[Any]:
        """Build safety-wrapped ``Tool`` objects for every enabled plugin.

        The declared contract carries the safety model — the plugin author never
        does. ``risk`` comes from ``tool.json`` (validated to read|write|execute),
        the scope is forced to ``plugin:<name>``, and the origin is ``plugin``,
        so the one execution engine gates the call before the handler is reached.
        """
        from agent_system.services.tools import Tool

        tools: list[Any] = []
        for plugin in self.enabled_plugins():
            try:
                func = plugin.load_callable()
            except ToolPluginError as exc:
                self.last_errors.append(str(exc))
                continue
            tools.append(
                Tool(
                    name=plugin.name,
                    description=plugin.description,
                    parameters=plugin.parameters,
                    risk=plugin.risk,
                    handler=_wrap_plugin_handler(plugin, func),
                    scope=f"plugin:{plugin.name}",
                    kind="plugin",
                )
            )
        return tools


def _wrap_plugin_handler(plugin: ToolPlugin, func: Any) -> Any:
    """Wrap a plugin handler: sandboxed when execute, in-process otherwise.

    Approval is *not* handled here — the declared ``risk``/``scope`` on the
    ``Tool`` route the call through the one permission gate before this handler
    runs, so there is no second, plugin-local gate to drift.
    """
    from agent_system.services.tools import RISK_EXECUTE, ToolError

    if plugin.risk == RISK_EXECUTE:

        def execute_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return _run_in_sandbox(plugin, args, ctx)

        return execute_handler

    def passthrough_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            result = func(args)
        except Exception as exc:
            raise ToolError(f"{plugin.name} failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(result, dict):
            return {"_value": result}
        return result

    return passthrough_handler


def _run_in_sandbox(plugin: ToolPlugin, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run an execute-risk plugin handler sandboxed (Docker) or jailed (cloud).

    Local: ``DockerSandbox`` (container isolation). Cloud (``HEROKU_JAIL``):
    ``SubprocessJail`` (containment — cwd-confined, secret env stripped,
    rlimits enforced; approval gate still applies). Sandbox-unavailable is
    an honest error — plugins never fall back to bare host execution.
    """
    from agent_system.services.tools import ToolError

    settings = getattr(ctx, "settings", None)
    timeout = int(getattr(settings, "max_execution_time_seconds", 300) or 300)
    use_jail = bool(getattr(settings, "heroku_jail", False))
    runner: Any = None
    if use_jail:
        from agent_system.services.sandbox import SubprocessJail

        runner = SubprocessJail()
    else:
        try:
            from agent_system.services.sandbox import DockerSandbox

            runner = DockerSandbox()
        except Exception as exc:
            raise ToolError(f"{plugin.name}: sandbox unavailable: {exc}") from exc
    staging = Path(tempfile.mkdtemp(prefix="tool-plugin-"))
    try:
        shutil.copy(plugin.handler_file, staging / "handler.py")
        (staging / "args.json").write_text(_json.dumps(args), encoding="utf-8")
        (staging / "_runner.py").write_text(_RUNNER_SOURCE, encoding="utf-8")
        # Docker mounts the staging dir at /ws; the jail uses it as cwd.
        remote_cmd = "_runner.py" if use_jail else "/ws/_runner.py"
        try:
            outcome = runner.run(str(staging), f"python3 {remote_cmd}", timeout_seconds=timeout)
        except Exception as exc:
            raise ToolError(f"{plugin.name}: sandbox exec failed: {exc}") from exc
        if int(outcome.get("exit_code", -1)) != 0:
            raise ToolError(
                f"{plugin.name}: sandbox exit {outcome.get('exit_code')}: "
                f"{str(outcome.get('stdout', ''))[-500:]}"
            )
        lines = [ln for ln in str(outcome.get("stdout", "")).splitlines() if ln.strip()]
        if not lines:
            raise ToolError(f"{plugin.name}: empty sandbox output")
        try:
            result = _json.loads(lines[-1])
        except ValueError as exc:
            raise ToolError(
                f"{plugin.name}: sandbox did not return JSON: {lines[-1][:200]}"
            ) from exc
        return result if isinstance(result, dict) else {"_value": result}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "ToolPlugin",
    "ToolPluginError",
    "ToolPluginManager",
]
