"""Cheap deterministic classification of inbound messages (no LLM call required)."""

from __future__ import annotations

import re

_CHAT_GREETINGS = {
    "hi",
    "hii",
    "hiii",
    "hello",
    "hey",
    "heyy",
    "good morning",
    "good afternoon",
    "good evening",
    "howdy",
    "sup",
    "yo",
    "ping",
    "test",
    "thanks",
    "thank you",
    "bye",
    "goodbye",
}

_CHAT_PHRASES = [
    r"who\s+are\s+you",
    r"what\s+are\s+you",
    r"what\s+is\s+your\s+name",
    r"how\s+are\s+you",
    r"tell\s+me\s+about\s+yourself",
    r"are\s+you\s+there",
    r"are\s+you\s+a\s+bot",
    r"what\s+can\s+you\s+do",
]

_CODING_KEYWORDS = [
    "code",
    "script",
    "python",
    "javascript",
    "typescript",
    "html",
    "css",
    "refactor",
    "bug",
    "fix",
    "function",
    "class",
    "git",
    "commit",
    "repo",
    "repository",
    "pytest",
    "unittest",
    "write a program",
]

_RESEARCH_KEYWORDS = [
    "search",
    "google",
    "web",
    "find info",
    "look up",
    "research",
    "browse",
    "news",
    "summarize article",
]

_LONG_RUNNING_KEYWORDS = [
    "batch",
    "crawl",
    "benchmark",
    "deploy",
    "train",
    "backup",
    "long running",
]

_TOOL_KEYWORDS = [
    "file",
    "folder",
    "directory",
    "shell",
    "bash",
    "ssh",
    "execute",
    "run",
    "terminal",
    "create",
    "delete",
    "download",
    "upload",
    "read",
    "write",
    "openconnector",
    "mcp",
    "audit",
    "analyze",
    "task",
    "data",
]


def classify_request_type(text: str) -> str:
    """Classify text into CHAT, TOOL_TASK, CODING_TASK, RESEARCH_TASK, or LONG_RUNNING_TASK."""
    raw = (text or "").strip()
    if not raw:
        return "CHAT"
    cleaned = raw.lower().strip()

    # 1. Exact or simple greeting match
    if cleaned in _CHAT_GREETINGS or cleaned.rstrip(".!?") in _CHAT_GREETINGS:
        return "CHAT"

    # 2. Known conversational phrases
    for pattern in _CHAT_PHRASES:
        if re.search(pattern, cleaned):
            return "CHAT"

    # 3. Task category keywords
    for kw in _CODING_KEYWORDS:
        if kw in cleaned:
            return "CODING_TASK"

    for kw in _RESEARCH_KEYWORDS:
        if kw in cleaned:
            return "RESEARCH_TASK"

    for kw in _LONG_RUNNING_KEYWORDS:
        if kw in cleaned:
            return "LONG_RUNNING_TASK"

    for kw in _TOOL_KEYWORDS:
        if kw in cleaned:
            return "TOOL_TASK"

    task_verbs = ("task", "audit", "analyze", "report", "process", "build", "create")
    if any(w in cleaned for w in task_verbs):
        return "TOOL_TASK"

    # Short casual messages without task/action verbs
    words = cleaned.split()
    if len(words) <= 3:
        return "CHAT"

    return "TOOL_TASK"


__all__ = ["classify_request_type"]
