"""Unit tests — system environment detection."""

from __future__ import annotations

import json

from agent_system.cli import sysdetect


class TestDetect:
    def test_report_has_expected_shape(self) -> None:
        report = sysdetect.detect_environment()
        assert report.os
        assert report.arch
        assert report.cpu_count >= 1
        assert report.python_version
        assert set(report.tools) == {"uv", "git", "docker", "docker-compose"}
        assert set(report.ports_in_use) == {8000}
        assert isinstance(report.warnings, list)

    def test_python_ok_on_supported_interpreter(self) -> None:
        report = sysdetect.detect_environment()
        assert report.python_ok is True

    def test_to_dict_is_json_serializable(self) -> None:
        report = sysdetect.detect_environment()
        payload = json.dumps(report.to_dict())
        assert "python_version" in payload

    def test_uv_detected_in_dev_env(self) -> None:
        report = sysdetect.detect_environment()
        assert report.tools["uv"].found is True


class TestProbes:
    def test_missing_tool_carries_hint(self) -> None:
        status = sysdetect._probe_tool("definitely-not-a-real-tool-xyz", "hint text")
        assert status.found is False
        assert status.hint == "hint text"

    def test_render_does_not_raise(self, capsys) -> None:
        report = sysdetect.detect_environment()
        sysdetect.render_report(report)
        out = capsys.readouterr().out
        assert "Python" in out
