"""Agent skills — Hermes-style pluggable capabilities.

A skill is a folder ``skills/<name>/SKILL.md``: YAML frontmatter (name,
version, description, enabled flag, agent allowlist, default config) plus a
Markdown body of instructions that get injected into an agent's prompt.

Users add skills by dropping in a folder, by ``agentctl skills add <path|url>``,
or via ``POST /api/v1/skills`` — and agents themselves can create new skills
through the same ``SkillManager.create_skill()`` / API path (``author`` tracks
who made it). Per-user tweaks live outside the skill file so updates never
clobber them:

- ``skills/.state.json``               — enable/disable overrides
- ``skills/<name>/config.local.yaml``  — config value overrides

``SkillManager.compose()`` renders the ``<skills>`` prompt block; the model
router calls it automatically when ``invoke()`` receives ``skills=`` /
``agent_type=``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_FILENAME = "SKILL.md"
STATE_FILENAME = ".state.json"
LOCAL_CONFIG_FILENAME = "config.local.yaml"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class SkillError(RuntimeError):
    """Base error for skill operations."""


class SkillNotFoundError(SkillError):
    """Raised when a named skill does not exist."""


class SkillExistsError(SkillError):
    """Raised when creating/importing would overwrite an existing skill."""


class SkillValidationError(SkillError):
    """Raised when a SKILL.md file is malformed."""


@dataclass(frozen=True)
class Skill:
    """One parsed skill with effective (merged) enabled/config state."""

    name: str
    version: str
    description: str
    enabled: bool
    agents: tuple[str, ...]
    config: dict[str, Any]
    instructions: str
    path: Path
    source: str  # "builtin" (shipped) or "custom" (user/agent added)
    author: str = "user"

    def summary(self) -> dict[str, Any]:
        """Short dict for list views (no instruction body)."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "enabled": self.enabled,
            "agents": list(self.agents),
            "source": self.source,
            "author": self.author,
        }

    def detail(self) -> dict[str, Any]:
        """Full dict including instructions + effective config."""
        data = self.summary()
        data["config"] = dict(self.config)
        data["instructions"] = self.instructions
        return data


@dataclass(frozen=True)
class ComposedPrompt:
    """Prompt text plus which skills were injected (for event payloads)."""

    text: str
    skills_used: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _parse_skill_file(path: Path) -> tuple[dict[str, Any], str]:
    """Split SKILL.md into (frontmatter dict, instruction body)."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillValidationError(f"{path}: missing YAML frontmatter (--- block)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillValidationError(f"{path}: frontmatter must be a mapping")
    return dict(meta), raw[match.end() :].strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


class SkillManager:
    """Discover, render, configure, and author skills in a directory."""

    def __init__(self, skills_dir: Path | str, builtin_dir: Path | str | None = None) -> None:
        self.skills_dir = Path(skills_dir)
        self.builtin_dir = Path(builtin_dir) if builtin_dir is not None else None
        self.last_errors: list[str] = []

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _state_overrides(self) -> dict[str, Any]:
        path = self.skills_dir / STATE_FILENAME
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _local_config(self, name: str) -> dict[str, Any]:
        path = self.skills_dir / name / LOCAL_CONFIG_FILENAME
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        path = self.skills_dir / STATE_FILENAME
        import json

        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _load_skill(self, skill_dir: Path, state: dict[str, Any]) -> Skill:
        meta, instructions = _parse_skill_file(skill_dir / SKILL_FILENAME)
        dirname = skill_dir.name
        if not _NAME_RE.match(dirname):
            raise SkillValidationError(f"{skill_dir}: invalid skill folder name '{dirname}'")
        # Folder name is canonical (Hermes convention); frontmatter may repeat it.
        raw_name = str(meta.get("name") or dirname)
        if raw_name != dirname:
            raise SkillValidationError(
                f"{skill_dir}: frontmatter name '{raw_name}' must match folder '{dirname}'"
            )
        name = dirname
        if not instructions:
            raise SkillValidationError(f"{skill_dir}: empty instruction body")
        agents = tuple(_as_str_list(meta.get("agents")))
        base_config = meta.get("config") or {}
        if not isinstance(base_config, dict):
            raise SkillValidationError(f"{skill_dir}: 'config' must be a mapping")
        config: dict[str, Any] = {**dict(base_config), **self._local_config(name)}
        override = state.get(name)
        enabled = bool(meta.get("enabled", True))
        if isinstance(override, dict) and "enabled" in override:
            enabled = bool(override["enabled"])
        source = "custom"
        if self.builtin_dir is not None:
            try:
                skill_dir.resolve().relative_to(self.builtin_dir.resolve())
                source = "builtin"
            except ValueError:
                source = "custom"
        return Skill(
            name=name,
            version=str(meta.get("version") or "0.1.0"),
            description=str(meta.get("description") or ""),
            enabled=enabled,
            agents=agents,
            config=config,
            instructions=instructions,
            path=skill_dir,
            source=source,
            author=str(meta.get("author") or "user"),
        )

    def discover(self) -> list[Skill]:
        """Load every valid skill; invalid ones land in ``last_errors``."""
        self.last_errors = []
        skills: list[Skill] = []
        if not self.skills_dir.is_dir():
            return skills
        state = self._state_overrides()
        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / SKILL_FILENAME).is_file():
                continue
            try:
                skills.append(self._load_skill(child, state))
            except SkillError as exc:
                self.last_errors.append(str(exc))
            except Exception as exc:  # never let one bad skill break discovery
                self.last_errors.append(f"{child.name}: {type(exc).__name__}: {exc}")
        skills.sort(key=lambda s: s.name)
        return skills

    def _by_name(self) -> dict[str, Skill]:
        return {s.name: s for s in self.discover()}

    def get(self, name: str) -> Skill:
        """Return one skill or raise SkillNotFoundError."""
        skill = self._by_name().get(name)
        if skill is None:
            raise SkillNotFoundError(f"skill '{name}' not found in {self.skills_dir}")
        return skill

    def enabled(self, agent_type: str | None = None) -> list[Skill]:
        """Enabled skills, optionally filtered to an agent type allowlist."""
        out = []
        for skill in self.discover():
            if not skill.enabled:
                continue
            if agent_type and skill.agents and agent_type not in skill.agents:
                continue
            out.append(skill)
        return out

    # ------------------------------------------------------------------
    # Prompt composition
    # ------------------------------------------------------------------

    def compose(
        self,
        prompt: str,
        *,
        agent_type: str | None = None,
        skills: list[str] | None = None,
    ) -> ComposedPrompt:
        """Append a ``<skills>`` block with the selected skills' instructions.

        - ``skills=[...]``: inject exactly those (enabled ones; disabled or
          unknown names are reported in ``skipped``).
        - otherwise: inject every enabled skill matching ``agent_type``
          (or all enabled skills when no agent type is given).
        """
        if skills is not None:
            by_name = self._by_name()
            chosen: list[Skill] = []
            skipped: list[str] = []
            for name in skills:
                skill = by_name.get(name)
                if skill is None or not skill.enabled:
                    skipped.append(name)
                else:
                    chosen.append(skill)
        else:
            chosen = self.enabled(agent_type)
            skipped = []
        if not chosen:
            return ComposedPrompt(text=prompt, skills_used=[], skipped=skipped)
        blocks = "\n".join(
            f'<skill name="{s.name}" version="{s.version}">\n{s.instructions}\n</skill>'
            for s in chosen
        )
        text = f"{prompt.rstrip()}\n\n<skills>\n{blocks}\n</skills>"
        return ComposedPrompt(text=text, skills_used=[s.name for s in chosen], skipped=skipped)

    def system_with_skills(
        self,
        base_system: str,
        *,
        agent_type: str | None = None,
        skills: list[str] | None = None,
    ) -> ComposedPrompt:
        """Prepend skills to a system prompt (variant of compose)."""
        composed = self.compose("", agent_type=agent_type, skills=skills)
        if not composed.skills_used:
            return ComposedPrompt(text=base_system, skipped=composed.skipped)
        block = composed.text.split("<skills>", 1)[1]
        text = f"{base_system.rstrip()}\n\n<skills>{block}"
        return ComposedPrompt(text=text, skills_used=composed.skills_used, skipped=composed.skipped)

    # ------------------------------------------------------------------
    # Configuration (user-facing; never edits SKILL.md itself)
    # ------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> Skill:
        """Enable/disable a skill (persisted in .state.json)."""
        self.get(name)  # raises if unknown
        state = self._state_overrides()
        entry = dict(state.get(name) or {})
        entry["enabled"] = bool(enabled)
        state[name] = entry
        self._save_state(state)
        return self.get(name)

    def set_config(self, name: str, key: str, value: Any) -> Skill:
        """Set one config value (persisted in config.local.yaml)."""
        skill = self.get(name)  # raises if unknown
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", key):
            raise SkillValidationError(f"invalid config key '{key}'")
        merged = {**skill.config, key: value}
        path = self.skills_dir / name / LOCAL_CONFIG_FILENAME
        path.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
        return self.get(name)

    # ------------------------------------------------------------------
    # Authoring (users AND agents create skills through here)
    # ------------------------------------------------------------------

    @staticmethod
    def _render_skill_file(
        name: str,
        description: str,
        instructions: str,
        agents: list[str],
        config: dict[str, Any],
        author: str,
    ) -> str:
        header = {
            "name": name,
            "version": "0.1.0",
            "description": description,
            "enabled": True,
            "agents": agents,
            "config": config,
            "author": author,
        }
        frontmatter = yaml.safe_dump(header, sort_keys=False)
        return f"---\n{frontmatter}---\n\n{instructions.rstrip()}\n"

    def create_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        agents: list[str] | None = None,
        config: dict[str, Any] | None = None,
        author: str = "user",
    ) -> Skill:
        """Scaffold ``skills/<name>/SKILL.md``. Used by CLI, API, and agents."""
        if not _NAME_RE.match(name):
            raise SkillValidationError(
                f"invalid skill name '{name}' (lowercase letters, digits, dashes)"
            )
        if not description.strip():
            raise SkillValidationError("description must not be empty")
        if not instructions.strip():
            raise SkillValidationError("instructions must not be empty")
        target = self.skills_dir / name
        if target.exists():
            raise SkillExistsError(f"skill '{name}' already exists")
        target.mkdir(parents=True)
        body = self._render_skill_file(
            name,
            description.strip(),
            instructions.strip(),
            list(agents or []),
            dict(config or {}),
            author,
        )
        (target / SKILL_FILENAME).write_text(body, encoding="utf-8")
        return self.get(name)

    def add_skill(self, source: str, overwrite: bool = False) -> list[Skill]:
        """Import skill(s) from a local path, an .md URL, or a git URL."""
        added: list[Skill] = []
        if source.lower().endswith(".md") and source.lower().startswith(("http://", "https://")):
            added = [self._import_from_url(source, overwrite)]
        elif self._looks_like_git_url(source):
            added = self._import_from_git(source, overwrite)
        else:
            path = Path(source).expanduser()
            if not path.exists():
                raise SkillNotFoundError(f"import source not found: {source}")
            if path.is_file():
                added = [self._import_single_file(path, overwrite)]
            else:
                added = self._import_directory(path, overwrite)
        if not added:
            raise SkillNotFoundError(f"no skills found in {source}")
        return added

    @staticmethod
    def _looks_like_git_url(source: str) -> bool:
        lowered = source.lower()
        if lowered.startswith(("git+", "git@", "ssh://", "git://")) or lowered.endswith(".git"):
            return True
        if lowered.startswith(("http://", "https://")) and not lowered.endswith(".md"):
            return "github.com" in lowered or "gitlab.com" in lowered
        return False

    def _import_from_url(self, url: str, overwrite: bool) -> Skill:
        import urllib.request

        tmp = Path(tempfile.mkdtemp(prefix="bob-skill-")) / "SKILL.md"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                tmp.write_bytes(resp.read())
            return self._import_single_file(tmp, overwrite)
        except SkillError:
            raise
        except Exception as exc:
            raise SkillError(f"download failed for {url}: {exc}") from exc
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def _import_single_file(self, path: Path, overwrite: bool) -> Skill:
        meta, instructions = _parse_skill_file(path)
        name = str(meta.get("name") or path.stem)
        if not _NAME_RE.match(name):
            raise SkillValidationError(f"cannot derive a valid skill name from {path}")
        if overwrite and (self.skills_dir / name).exists():
            return self._overwrite_skill(name, path.read_text(encoding="utf-8"))
        return self.create_skill(
            name,
            description=str(meta.get("description") or ""),
            instructions=instructions,
            agents=_as_str_list(meta.get("agents")),
            config=dict(meta.get("config") or {}),
            author=str(meta.get("author") or "user"),
        )

    def _overwrite_skill(self, name: str, skill_md: str) -> Skill:
        (self.skills_dir / name / SKILL_FILENAME).write_text(skill_md, encoding="utf-8")
        return self.get(name)

    def _import_directory(self, path: Path, overwrite: bool) -> list[Skill]:
        # Single skill dir (contains SKILL.md) or a collection of them.
        candidates = (
            [path]
            if (path / SKILL_FILENAME).is_file()
            else [
                c for c in sorted(path.iterdir()) if c.is_dir() and (c / SKILL_FILENAME).is_file()
            ]
        )
        added: list[Skill] = []
        for candidate in candidates:
            meta, _ = _parse_skill_file(candidate / SKILL_FILENAME)
            name = str(meta.get("name") or candidate.name)
            if not _NAME_RE.match(name):
                self.last_errors.append(f"{candidate}: invalid skill name '{name}'")
                continue
            target = self.skills_dir / name
            if target.exists():
                if not overwrite:
                    raise SkillExistsError(f"skill '{name}' already exists (use overwrite)")
                shutil.rmtree(target)
            shutil.copytree(candidate, target)
            added.append(self.get(name))
        return added

    def _import_from_git(self, url: str, overwrite: bool) -> list[Skill]:
        git = shutil.which("git")
        if git is None:
            raise SkillError("git is required to import skills from a URL")
        tmp = Path(tempfile.mkdtemp(prefix="bob-skill-"))
        try:
            proc = subprocess.run(
                [git, "clone", "--depth", "1", url, str(tmp / "repo")],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise SkillError(f"git clone failed: {proc.stderr.strip()[-500:]}")
            return self._import_directory(tmp / "repo", overwrite)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def delete_skill(self, name: str) -> None:
        """Remove a skill directory entirely."""
        skill = self.get(name)  # raises if unknown
        shutil.rmtree(skill.path)
