"""Unit tests — SkillManager (discover/compose/config/authoring)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.services.skills import (
    ComposedPrompt,
    SkillExistsError,
    SkillManager,
    SkillNotFoundError,
    SkillValidationError,
)

SEEDS = Path(__file__).resolve().parents[2] / "skills"


def _write(skill_dir: Path, body: str) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


_GOOD = """---
name: demo
version: 1.2.0
description: Demo skill.
enabled: true
agents: [research]
config:
  depth: 2
---
# Demo
Do the thing.
"""


class TestDiscover:
    def test_seed_skills_load(self) -> None:
        mgr = SkillManager(SEEDS, builtin_dir=SEEDS)
        skills = mgr.discover()
        names = {s.name for s in skills}
        assert {"web-research", "document-craft", "qa-assist"} <= names
        assert mgr.last_errors == []
        web = next(s for s in skills if s.name == "web-research")
        assert web.source == "builtin"
        assert web.config["max_results"] == 5
        assert "primary sources" in web.instructions

    def test_missing_dir_discovers_nothing(self, tmp_path: Path) -> None:
        mgr = SkillManager(tmp_path / "nope")
        assert mgr.discover() == []

    def test_bad_skill_skipped_with_error(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        _write(tmp_path / "bad", "no frontmatter here\n")
        mgr = SkillManager(tmp_path)
        skills = mgr.discover()
        assert [s.name for s in skills] == ["demo"]
        assert len(mgr.last_errors) == 1
        assert "bad" in mgr.last_errors[0]

    def test_get_unknown_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            SkillManager(tmp_path).get("ghost")


class TestEnabled:
    def test_agent_filter(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)  # agents: [research]
        mgr = SkillManager(tmp_path)
        assert [s.name for s in mgr.enabled("research")] == ["demo"]
        assert mgr.enabled("documents") == []

    def test_state_override_disables(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        mgr = SkillManager(tmp_path)
        mgr.set_enabled("demo", False)
        assert mgr.get("demo").enabled is False
        assert mgr.enabled() == []
        assert mgr.enabled("research") == []


class TestCompose:
    def test_compose_all_enabled(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        composed = SkillManager(tmp_path).compose("hello")
        assert isinstance(composed, ComposedPrompt)
        assert composed.text.startswith("hello")
        assert "<skills>" in composed.text
        assert 'name="demo"' in composed.text
        assert composed.skills_used == ["demo"]

    def test_compose_explicit_skips_disabled(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        mgr = SkillManager(tmp_path)
        mgr.set_enabled("demo", False)
        composed = mgr.compose("hi", skills=["demo", "ghost"])
        assert composed.text == "hi"
        assert composed.skipped == ["demo", "ghost"]

    def test_system_with_skills_prepends(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        composed = SkillManager(tmp_path).system_with_skills("You are Bob.")
        assert composed.text.startswith("You are Bob.")
        assert "<skills>" in composed.text


class TestConfig:
    def test_set_config_persists_override(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        mgr = SkillManager(tmp_path)
        updated = mgr.set_config("demo", "depth", 9)
        assert updated.config["depth"] == 9
        # Fresh manager sees the override too.
        assert SkillManager(tmp_path).get("demo").config["depth"] == 9

    def test_set_config_bad_key(self, tmp_path: Path) -> None:
        _write(tmp_path / "demo", _GOOD)
        with pytest.raises(SkillValidationError):
            SkillManager(tmp_path).set_config("demo", "not a key!", 1)

    def test_set_config_unknown_skill(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            SkillManager(tmp_path).set_config("ghost", "k", 1)


class TestAuthoring:
    def test_create_skill(self, tmp_path: Path) -> None:
        mgr = SkillManager(tmp_path)
        skill = mgr.create_skill(
            "my-skill",
            "Does things.",
            "Always do things well.",
            agents=["research"],
            config={"level": 1},
            author="agent:research",
        )
        assert skill.name == "my-skill"
        assert skill.author == "agent:research"
        assert (tmp_path / "my-skill" / "SKILL.md").is_file()

    def test_create_validates_name(self, tmp_path: Path) -> None:
        with pytest.raises(SkillValidationError):
            SkillManager(tmp_path).create_skill("Bad Name!", "d", "i")

    def test_create_duplicate_raises(self, tmp_path: Path) -> None:
        mgr = SkillManager(tmp_path)
        mgr.create_skill("dup", "d", "i")
        with pytest.raises(SkillExistsError):
            mgr.create_skill("dup", "d", "i")

    def test_add_from_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "cool"
        _write(src, _GOOD.replace("name: demo", "name: cool"))
        mgr = SkillManager(tmp_path / "skills")
        added = mgr.add_skill(str(tmp_path / "src"))
        assert [s.name for s in added] == ["cool"]

    def test_add_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            SkillManager(tmp_path).add_skill(str(tmp_path / "nope"))

    def test_delete(self, tmp_path: Path) -> None:
        mgr = SkillManager(tmp_path)
        mgr.create_skill("bye", "d", "i")
        mgr.delete_skill("bye")
        with pytest.raises(SkillNotFoundError):
            mgr.get("bye")
