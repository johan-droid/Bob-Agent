"""Connection Capability Registry & Provider Adapters (Chat-Native Architecture §9 & §15).

Maps: Credential -> Provider -> Connection -> Capabilities -> Authorized Tools.

The agent receives capability metadata rather than credentials:
  connection: "github:personal"
  capabilities: ["repos", "issues", "pull_requests"]
  authorized_tools: ["git_clone", "github_issue_create"]

Raw credentials (API keys, private keys, tokens) NEVER pass to the agent or LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple

from agent_system.services.credentials import CredentialStore

logger = logging.getLogger(__name__)

# Generic Credential Types
CREDENTIAL_TYPES = (
    "api_key",
    "oauth",
    "bearer_token",
    "basic_auth",
    "ssh_key",
    "certificate",
    "database",
    "cloud_credentials",
)


class CapabilityInfo(NamedTuple):
    name: str
    description: str


@dataclass
class ConnectionCapability:
    connection_ref: str  # e.g., "github:personal", "ssh:home-server"
    provider: str  # e.g., "github", "ssh", "groq"
    name: str  # e.g., "personal", "home-server"
    credential_type: str  # e.g., "oauth", "ssh_key", "api_key"
    status: str  # "healthy", "degraded", "revoked"
    capabilities: list[CapabilityInfo] = field(default_factory=list)
    authorized_tools: list[str] = field(default_factory=list)
    last_validated_at: str | None = None


# Provider Capability Mapping Definitions
PROVIDER_CAPABILITIES_MAP: dict[str, tuple[str, list[CapabilityInfo], list[str]]] = {
    "github": (
        "oauth",
        [
            CapabilityInfo("repos", "Repository read/write access"),
            CapabilityInfo("issues", "Issue tracking and creation"),
            CapabilityInfo("pull_requests", "Pull request reviews and management"),
            CapabilityInfo("actions", "CI/CD workflow triggers"),
        ],
        ["git_clone", "git_commit", "github_pr_create", "github_issue_list"],
    ),
    "ssh": (
        "ssh_key",
        [
            CapabilityInfo("command_execution", "Remote shell command execution"),
            CapabilityInfo("file_transfer", "SFTP/SCP file transfer"),
        ],
        ["ssh_execute"],
    ),
    "groq": (
        "api_key",
        [
            CapabilityInfo("chat_completion", "Fast LLM inference"),
            CapabilityInfo("tool_calling", "Structured tool calling"),
        ],
        ["llm_router"],
    ),
    "openrouter": (
        "api_key",
        [
            CapabilityInfo("chat_completion", "Multi-model LLM router"),
            CapabilityInfo("tool_calling", "Structured tool calling"),
        ],
        ["llm_router"],
    ),
    "gemini": (
        "api_key",
        [
            CapabilityInfo("chat_completion", "Google Gemini multimodal inference"),
            CapabilityInfo("tool_calling", "Tool execution"),
        ],
        ["llm_router"],
    ),
    "mcp": (
        "bearer_token",
        [
            CapabilityInfo("mcp_tools", "Model Context Protocol tool execution"),
        ],
        ["mcp_call_tool"],
    ),
    "openconnector": (
        "bearer_token",
        [
            CapabilityInfo("saas_actions", "OpenConnector 1000+ SaaS API actions"),
        ],
        ["openconnector_act"],
    ),
}


class CapabilityRegistry:
    """Discovers and resolves capabilities from stored user credentials."""

    def __init__(self, credential_store: CredentialStore) -> None:
        self._vault = credential_store

    def get_user_capabilities(
        self, user_id: str, provider_filter: str | None = None
    ) -> list[ConnectionCapability]:
        """List all capabilities available to a user from validated credentials."""
        metas = self._vault.list_metadata(user_id, provider_filter)
        results: list[ConnectionCapability] = []

        for m in metas:
            if m.status == "revoked":
                continue

            provider = m.provider.lower()
            default_type, caps, tools = PROVIDER_CAPABILITIES_MAP.get(
                provider,
                (
                    "api_key",
                    [CapabilityInfo("generic_access", f"Generic API access for {provider}")],
                    ["generic_tool"],
                ),
            )

            conn_ref = f"{provider}:{m.name}"
            last_val = m.last_validated_at.isoformat() if m.last_validated_at else None

            results.append(
                ConnectionCapability(
                    connection_ref=conn_ref,
                    provider=provider,
                    name=m.name,
                    credential_type=default_type,
                    status=m.status,
                    capabilities=caps,
                    authorized_tools=tools,
                    last_validated_at=last_val,
                )
            )

        return results

    def is_tool_authorized(self, user_id: str, connection_ref: str, tool_name: str) -> bool:
        """Check if a tool execution is authorized for a specific connection reference."""
        if ":" not in connection_ref:
            return False
        provider, name = connection_ref.split(":", 1)
        cred = self._vault.get(user_id, provider, name)
        if not cred:
            return False

        _, _, authorized_tools = PROVIDER_CAPABILITIES_MAP.get(
            provider.lower(), ("api_key", [], [])
        )
        return tool_name in authorized_tools or "generic_tool" in authorized_tools
