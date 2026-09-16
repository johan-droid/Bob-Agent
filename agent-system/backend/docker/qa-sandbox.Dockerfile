# QA sandbox image (v3.1 Phase 13): untrusted generated tests run here.
# python:3.12-slim + pytest only — no host mounts besides the run dir,
# network disabled, resource limits enforced by the sandbox manager.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.* coverage==7.*
