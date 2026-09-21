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
    "hello bro",
    "hey bro",
    "hi bro",
    "hi bob",
    "hello bob",
    "hey bob",
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
    "ok",
    "okay",
    "nice",
    "cool",
    "awesome",
    "got it",
    "makes sense",
}

_CHAT_PATTERNS = [
    r"^who\s+are\s+you",
    r"^what\s+are\s+you",
    r"^what\s+is\s+your\s+name",
    r"^how\s+are\s+you",
    r"^tell\s+me\s+about\s+yourself",
    r"^are\s+you\s+there",
    r"^are\s+you\s+a\s+bot",
    r"^what\s+can\s+you\s+do",
    r"^what\s+did\s+we\s+discuss",
    r"^what\s+did\s+i\s+(say|ask)",
    r"^can\s+you\s+(plan|access|explain|tell|help|show|find|list|check|summarize|think|write)",
    r"^can\s+you\b",
    r"^how\s+(do|can|would|to)",
    r"^what\s+(is|are|does|do|can|should)",
    r"^plan\s+(out\s+)?a\s+project",
    r"^project\s+plan",
    r"\bplan\s+a\s+project\b",
    r"^remember\s+this",
    r"^explain\s+",
    r"^tell\s+me\s+",
    r"^why\s+is\s+",
    r"^i\s+don't\s+understand",
    r"^that's\s+",
    r"^that\s+is\s+",
]

_CODING_PATTERNS = [
    r"\bpython\b",
    r"\bjavascript\b",
    r"\btypescript\b",
    r"\bpytest\b",
    r"\bunittest\b",
    r"write\s+(a\s+)?(python|code|script|program|function|class)",
    r"\brefactor\b",
    r"\bfix\b",
    r"git\s+(commit|push|pull|merge|repo)",
    r"audit\s+(my\s+)?(repository|code|repo)",
]

_RESEARCH_PATTERNS = [
    r"search\s+(the\s+)?web",
    r"search\s+google",
    r"\bgoogle\b",
    r"look\s+up\s+",
    r"find\s+info",
    r"browse\s+(the\s+)?web",
    r"\bsummarize\b",
    r"\bsummary\b",
    r"\bresearch\b",
]

_LONG_RUNNING_PATTERNS = [
    r"\bbatch\b",
    r"\bcrawl\b",
    r"\bbenchmark\b",
    r"long\s+running",
]

_TOOL_PATTERNS = [
    r"\btask\b",
    r"\banalyze\b",
    r"\breport\b",
    r"\bprocess\b",
    r"\bexecute\b",
    r"run\s+(command|terminal|a\s+task|task|script|test|job|app)",
    r"\bssh\b",
    r"\bcreate\b",
    r"\bbuild\b",
    r"\bgenerate\b",
    r"\bdeploy\b",
    r"\binstall\b",
    r"remind\s+me",
    r"openconnector",
    r"\bmcp\b",
    r"delete\s+file",
    r"download\s+file",
]


def classify_request_type(text: str) -> str:
    """Classify text into CHAT, TOOL_TASK, CODING_TASK, RESEARCH_TASK, or LONG_RUNNING_TASK."""
    raw = (text or "").strip()
    if not raw:
        return "CHAT"
    cleaned = raw.lower().strip()
    cleaned_no_punct = cleaned.rstrip(".!?")

    # 1. Exact or simple greeting/conversational match
    if cleaned in _CHAT_GREETINGS or cleaned_no_punct in _CHAT_GREETINGS:
        return "CHAT"

    # 2. Known conversational patterns
    for pattern in _CHAT_PATTERNS:
        if re.search(pattern, cleaned):
            return "CHAT"

    # 3. Strong coding triggers
    for pattern in _CODING_PATTERNS:
        if re.search(pattern, cleaned):
            return "CODING_TASK"

    # 4. Strong research triggers
    for pattern in _RESEARCH_PATTERNS:
        if re.search(pattern, cleaned):
            return "RESEARCH_TASK"

    # 5. Long running triggers
    for pattern in _LONG_RUNNING_PATTERNS:
        if re.search(pattern, cleaned):
            return "LONG_RUNNING_TASK"

    # 6. Tool execution triggers
    for pattern in _TOOL_PATTERNS:
        if re.search(pattern, cleaned):
            return "TOOL_TASK"

    return "CHAT"


__all__ = ["classify_request_type"]
