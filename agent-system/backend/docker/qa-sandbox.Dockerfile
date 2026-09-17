# QA sandbox image (v3.1 Phase 13): untrusted generated tests run here.
#
# python:3.12-slim + pytest only — no host mounts besides the run dir,
# network disabled, resource limits enforced by the sandbox manager.
#
# Production hardening (see docs/implementation/SECURITY.md):
# - The base image is pinned BY DIGEST, not by mutable tag. `python:3.12-slim`
#   is somebody else's variable; this digest is what this repository built and
#   ran its Docker-gated tests against. Rotating it is a deliberate, reviewed
#   change (update the digest, rebuild, re-run the sandbox tests), never a
#   silent drift caused by an upstream retag.
# - pip is version-pinned so a fresh base image cannot change resolver
#   behaviour under a build.
# - `USER sandbox` is a defensive fallback: if a caller forgets `user=`,
#   work still does not run as root. `DockerSandbox` always overrides this
#   with the invoking uid:gid so bind-mounted files stay host-owned.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

RUN pip install --no-cache-dir "pip==25.2" \
    && pip install --no-cache-dir pytest==8.* coverage==7.* \
    && useradd --create-home --shell /usr/sbin/nologin sandbox

# DockerSandbox.run() overrides this with the invoking uid:gid; this only
# matters if the image is ever run without that override.
USER sandbox

