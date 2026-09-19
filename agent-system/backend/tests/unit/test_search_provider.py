"""Unit tests for SearchProvider and DuckDuckGoSearchProvider."""

from unittest.mock import MagicMock, patch

import pytest

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ServiceHealthStatus,
    ServiceUnavailableError,
)
from agent_system.services.external_services.search import DuckDuckGoSearchProvider

SAMPLE_DDG_HTML = """
<html>
<body>
  <div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F">Python</a>
    <a class="result__snippet">Python is a programming language that lets you work quickly.</a>
  </div>
  <div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F">Python Dup</a>
    <a class="result__snippet">Duplicate link test.</a>
  </div>
  <div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F">Docs</a>
    <a class="result__snippet">Official documentation for Python.</a>
  </div>
</body>
</html>
"""


def test_search_provider_html_parsing_and_deduplication():
    settings = Settings()
    provider = DuckDuckGoSearchProvider(settings)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = SAMPLE_DDG_HTML
        mock_client.get.return_value = mock_resp

        results = provider.search("python programming", limit=10)

        assert len(results) == 2  # duplicate https://python.org/ filtered out
        assert results[0].source_url == "https://python.org/"
        assert results[0].title == "Python"
        assert "programming language" in results[0].snippet
        assert results[1].source_url == "https://docs.python.org/"


def test_search_provider_network_error():
    settings = Settings()
    provider = DuckDuckGoSearchProvider(settings)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = Exception("Connection timed out")

        with pytest.raises(ServiceUnavailableError):
            provider.search("fail query")

        health = provider.check_health()
        assert health.status == ServiceHealthStatus.UNAVAILABLE
        assert "timed out" in health.last_error
