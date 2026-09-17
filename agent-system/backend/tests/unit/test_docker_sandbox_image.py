"""Docker sandbox image provisioning (Phase 13 / reconciliation follow-up).

The QA sandbox image (`agent-system/qa-sandbox:latest`) is a build artifact of
this repository — it exists on no registry. Historically nothing built it, so
the Docker-gated QA test could only ever fail on a fresh machine. These tests
pin the provisioning contract and the actionable failure modes without needing
a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_system.services.sandbox import (
    QA_SANDBOX_DOCKERFILE,
    DockerSandbox,
    SandboxError,
    SandboxUnavailableError,
)

QA_IMAGE = DockerSandbox.QA_IMAGE


class _Images:
    """Fake `client.images` recording builds and remembering the result."""

    def __init__(self, present: bool, build_error: Exception | None = None) -> None:
        self._present = present
        self._build_error = build_error
        self.built: list[dict[str, Any]] = []

    def get(self, name: str) -> Any:
        if not self._present:
            raise Exception(f"no such image: {name}")  # noqa: TRY002 — mirrors docker SDK
        return object()

    def build(self, **kwargs: Any) -> Any:
        self.built.append(kwargs)
        if self._build_error is not None:
            raise self._build_error
        self._present = True
        return object()


class _Containers:
    def __init__(self, run_error: Exception | None = None) -> None:
        self._run_error = run_error

    def run(self, *args: Any, **kwargs: Any) -> Any:
        if self._run_error is not None:
            raise self._run_error
        raise AssertionError("run() should not be reached in these tests")


class _Client:
    def __init__(self, images: _Images, containers: _Containers | None = None) -> None:
        self.images = images
        self.containers = containers or _Containers()


def _sandbox(images: _Images, containers: _Containers | None = None) -> DockerSandbox:
    return DockerSandbox(docker_client=_Client(images, containers))


def test_repo_ships_the_qa_sandbox_dockerfile() -> None:
    """The image is only obtainable by building, so the Dockerfile must exist."""
    assert QA_SANDBOX_DOCKERFILE.is_file(), (
        f"missing {QA_SANDBOX_DOCKERFILE} — the QA sandbox image cannot be built"
    )
    body = QA_SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    assert "pytest" in body, "QA sandbox image must ship pytest to run generated tests"
    from_line = next(line for line in body.splitlines() if line.startswith("FROM "))
    assert "@sha256:" in from_line, (
        "the base image must be pinned by digest — a mutable tag lets the "
        "sandbox base layer drift without any change to this repository"
    )
    assert "USER " in body, (
        "the image must declare a non-root USER fallback so a caller that "
        "forgets user= still does not run untrusted code as root"
    )


def test_image_present_true_when_daemon_has_it() -> None:
    assert _sandbox(_Images(present=True)).image_present(QA_IMAGE) is True


def test_image_present_false_when_lookup_fails() -> None:
    assert _sandbox(_Images(present=False)).image_present(QA_IMAGE) is False


def test_ensure_image_is_a_noop_when_already_present() -> None:
    images = _Images(present=True)
    assert _sandbox(images).ensure_image(QA_IMAGE) is True
    assert images.built == [], "an image already present must not be rebuilt"


def test_ensure_image_builds_from_the_in_repo_dockerfile() -> None:
    images = _Images(present=False)
    sandbox = _sandbox(images)
    assert sandbox.ensure_image(QA_IMAGE) is True
    assert len(images.built) == 1
    call = images.built[0]
    assert call["tag"] == QA_IMAGE
    assert call["dockerfile"] == QA_SANDBOX_DOCKERFILE.name
    assert call["path"] == str(QA_SANDBOX_DOCKERFILE.parent)


def test_ensure_image_raises_actionable_error_when_build_fails() -> None:
    images = _Images(present=False, build_error=Exception("network unreachable"))
    with pytest.raises(SandboxUnavailableError) as excinfo:
        _sandbox(images).ensure_image(QA_IMAGE)
    assert "could not be built" in str(excinfo.value)


def test_ensure_image_raises_when_no_dockerfile_is_available() -> None:
    sandbox = _sandbox(_Images(present=False))
    missing = Path("/nonexistent/qa-sandbox.Dockerfile")
    with pytest.raises(SandboxUnavailableError) as excinfo:
        sandbox.ensure_image(QA_IMAGE, dockerfile=missing)
    assert "no Dockerfile was found" in str(excinfo.value)


def test_run_reports_missing_image_as_unavailable_not_generic_error() -> None:
    """A missing local image is an environment gap, not a broken sandbox.

    `SandboxUnavailableError` is what callers catch to fall back, so a fresh
    checkout degrades honestly instead of raising an opaque 404.
    """
    images = _Images(present=False)
    containers = _Containers(run_error=Exception("404 Client Error: pull access denied"))
    sandbox = _sandbox(images, containers)
    with pytest.raises(SandboxUnavailableError) as excinfo:
        sandbox.run("/tmp", ["echo", "hi"], image=QA_IMAGE)
    message = str(excinfo.value)
    assert QA_IMAGE in message
    assert "qa-sandbox-image" in message, "the error must name the fix"


def test_run_keeps_generic_error_for_pullable_images() -> None:
    """`python:3.12-slim` is pullable, so a start failure stays a SandboxError."""
    images = _Images(present=False)
    containers = _Containers(run_error=Exception("boom"))

    def _always_absent(name: str) -> Any:
        raise Exception("no such image")  # noqa: TRY002

    images.get = _always_absent  # type: ignore[method-assign]
    sandbox = _sandbox(images, containers)
    with pytest.raises(SandboxError) as excinfo:
        sandbox.run("/tmp", ["echo", "hi"])
    assert not isinstance(excinfo.value, SandboxUnavailableError)
