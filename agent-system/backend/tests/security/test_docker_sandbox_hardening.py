"""Docker boundary hardening (production hardening of the sandbox itself).

The QA sandbox *architecture* (in-repo image, fail-closed, no in-process
execution of untrusted tests) is covered by ``test_docker_sandbox_image.py``.
This module pins the *runtime hardening of the Docker boundary*: the daemon
itself is a trust boundary (``THREAT_MODEL.md`` assets), so every container
this repository starts must run with no capabilities, no privilege
reacquisition, private IPC, and a non-executable /tmp — regardless of
configuration, and never relaxed by a networked run.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_system.services.sandbox import DockerSandbox


class _FakeContainer:
    def wait(self, timeout: int | None = None) -> dict[str, Any]:
        return {"StatusCode": 0}

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        return b""

    def remove(self, force: bool = True) -> None:
        pass

    def kill(self) -> None:
        pass


class _RecordingContainers:
    def __init__(self, result: Any = None) -> None:
        self._result = result if result is not None else _FakeContainer()
        self.calls: list[dict[str, Any]] = []

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self._result


class _Client:
    def __init__(self, containers: _RecordingContainers) -> None:
        self.images = type("I", (), {"get": staticmethod(lambda n: object())})()
        self.containers = containers


def _capture_run(network: bool = False) -> dict[str, Any]:
    containers = _RecordingContainers()
    sandbox = DockerSandbox(docker_client=_Client(containers))
    sandbox.run("/tmp/fake-ws", ["echo", "hi"], network=network)
    assert len(containers.calls) == 1
    return containers.calls[0]["kwargs"]


def test_every_container_drops_all_capabilities() -> None:
    assert _capture_run()["cap_drop"] == ["ALL"]


def test_every_container_forbids_privilege_reacquisition() -> None:
    assert "no-new-privileges" in _capture_run()["security_opt"]


def test_every_container_gets_private_ipc() -> None:
    assert _capture_run()["ipc_mode"] == "private"


def test_tmpfs_is_noexec_and_nosuid() -> None:
    tmpfs = _capture_run()["tmpfs"]
    assert "/tmp" in tmpfs
    for flag in ("noexec", "nosuid"):
        assert flag in tmpfs["/tmp"], f"/tmp must be mounted {flag}"


def test_network_is_disabled_by_default() -> None:
    kwargs = _capture_run(network=False)
    assert kwargs["network_disabled"] is True
    assert "network_mode" not in kwargs


def test_networked_run_relaxes_only_the_network_posture() -> None:
    """An explicitly networked run keeps every other hardening control."""
    kwargs = _capture_run(network=True)
    assert "network_disabled" not in kwargs
    assert kwargs["network_mode"] == "bridge"
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in kwargs["security_opt"]
    assert kwargs["ipc_mode"] == "private"
    assert "noexec" in kwargs["tmpfs"]["/tmp"]


def test_containers_run_as_the_invoking_uid() -> None:
    """Bind-mounted files must stay host-owned (not root-owned)."""
    kwargs = _capture_run()
    import os

    assert kwargs["user"] == f"{os.getuid()}:{os.getgid()}"


def test_workspace_mount_is_the_only_host_bind_mount() -> None:
    volumes = _capture_run()["volumes"]
    assert set(volumes) == {"/tmp/fake-ws"}
    assert volumes["/tmp/fake-ws"]["bind"] == "/ws"


# ---------------------------------------------------------------------------
# Live proofs (docker-gated; skip cleanly without a daemon or image)
# ---------------------------------------------------------------------------


def _live_sandbox() -> DockerSandbox | None:
    try:
        sandbox = DockerSandbox()
    except Exception:  # docker SDK missing or daemon down
        return None
    try:
        sandbox.ensure_image(sandbox.QA_IMAGE)
    except Exception:
        return None
    return sandbox


def test_live_container_has_no_capabilities(tmp_path: Any) -> None:
    """A real container must report an empty effective capability set."""
    sandbox = _live_sandbox()
    if sandbox is None:
        pytest.skip("docker daemon or qa-sandbox image unavailable")
    outcome = sandbox.run(
        str(tmp_path),
        "python -c \"print(open('/proc/self/status').read().split('CapEff:')[1].split()[0])\"",
        timeout_seconds=60,
    )
    assert outcome["exit_code"] == 0, outcome
    cap_eff = int(outcome["stdout"].strip() or "0", 16)
    assert cap_eff == 0, f"container retained capabilities: {cap_eff:#x}"


def test_live_tmp_is_mounted_noexec(tmp_path: Any) -> None:
    sandbox = _live_sandbox()
    if sandbox is None:
        pytest.skip("docker daemon or qa-sandbox image unavailable")
    # Compiling a trivial program in /tmp must fail: /tmp is noexec+nosuid.
    (tmp_path / "boom.sh").write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    outcome = sandbox.run(
        str(tmp_path),
        "cp /ws/boom.sh /tmp/boom.sh && chmod +x /tmp/boom.sh && /tmp/boom.sh",
        timeout_seconds=60,
    )
    assert outcome["exit_code"] != 0, "/tmp must not be executable inside the sandbox"
    assert "pwned" not in outcome["stdout"]


def test_live_network_is_blocked(tmp_path: Any) -> None:
    sandbox = _live_sandbox()
    if sandbox is None:
        pytest.skip("docker daemon or qa-sandbox image unavailable")
    outcome = sandbox.run(
        str(tmp_path),
        'python -c "import socket; socket.setdefaulttimeout(5); '
        "socket.create_connection(('93.184.216.34', 80))\"",
        timeout_seconds=30,
    )
    assert outcome["exit_code"] != 0, "sandbox egress must be blocked by default"
