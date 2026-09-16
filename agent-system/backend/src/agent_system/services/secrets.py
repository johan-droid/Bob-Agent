"""Secret redaction utilities (v3.1 §14).

Never expose .env values, private keys, API keys, tokens, credentials,
database URLs, browser profiles, or secret env vars through any output path.
"""

from __future__ import annotations

import re
from pathlib import Path

_REDACTED = "[REDACTED]"

# Key-name markers (case-insensitive substring match)
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "cookie",
    "private_key",
    "credential",
    "session_key",
    "signing_key",
    "database_url",
    "connection_string",
)

# Value patterns (matched regardless of key name)
_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWTs
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
)

# File basenames that must never be copied into templates/snapshots
SECRET_FILENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials.json",
        "secrets.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".htpasswd",
    }
)

# Directory names never copied
SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config/gcloud"})


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def redact_value(value: str) -> str:
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def redact_dict(obj: dict[str, object]) -> dict[str, object]:
    """Redact a payload dict by key markers and value patterns (deep).

    Recurses into nested dicts, lists, and tuples so secrets inside
    arrays (e.g. event payload lists) are scrubbed too.
    """
    cleaned: dict[str, object] = {}
    for key, value in obj.items():
        if is_secret_key(key):
            cleaned[key] = _REDACTED
        else:
            cleaned[key] = _redact_any(value)
    return cleaned


def _redact_any(value: object) -> object:
    if isinstance(value, str):
        return redact_value(value)
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [_redact_any(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_any(item) for item in value)
    return value


def is_secret_path(rel_path: str) -> bool:
    """True if a relative path points at a secret-bearing file/dir."""
    parts = Path(rel_path).parts
    if not parts:
        return True
    if any(part in SECRET_DIRS for part in parts):
        return True
    return parts[-1] in SECRET_FILENAMES or parts[-1].startswith(".env")
