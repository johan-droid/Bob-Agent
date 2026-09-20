"""Unit tests for deterministic request classifier."""

from __future__ import annotations

from agent_system.services.classifier import classify_request_type


def test_classify_chat() -> None:
    assert classify_request_type("Hii") == "CHAT"
    assert classify_request_type("Who are you?") == "CHAT"
    assert classify_request_type("How are you?") == "CHAT"
    assert classify_request_type("Hello") == "CHAT"
    assert classify_request_type("what can you do?") == "CHAT"


def test_classify_tasks() -> None:
    assert classify_request_type("Write a python script to parse JSON") == "CODING_TASK"
    assert classify_request_type("Search the web for AI news") == "RESEARCH_TASK"
    assert classify_request_type("Run a long batch crawl") == "LONG_RUNNING_TASK"
    assert classify_request_type("Execute bash command ls -la") == "TOOL_TASK"
    assert classify_request_type("Audit my repository") == "CODING_TASK"
