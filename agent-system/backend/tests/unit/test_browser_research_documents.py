"""Unit tests: research agent + DocumentAgent (Phases 6–7).

Browser Playwright paths are unavailable in CI; the contract tested is the
honest-unavailable one (raises, never fakes). Research fetch is network-bound,
so a local HTTP server exercises the full extract+citation path.
"""

from __future__ import annotations

import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_system.agents.browser_research import ResearchAgent
from agent_system.agents.documents import DocumentAgent, DocumentError


@pytest.fixture(scope="module")
def http_server() -> dict[str, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = (
                b"<html><head><title>Test Page</title></head>"
                b"<body><h1>Hello</h1><script>evil()</script>"
                b"<p>Citation content here.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://127.0.0.1:{server.server_port}"}
    server.shutdown()


class TestResearch:
    def test_fetch_extracts_title_text(self, http_server: dict[str, str]) -> None:
        agent = ResearchAgent()
        citation = agent.fetch(f"{http_server['base']}/page")
        assert citation.title == "Test Page"
        assert "Citation content here." in citation.snippet
        assert "evil()" not in citation.snippet  # script content skipped
        assert citation.url.startswith("http://127.0.0.1")

    def test_research_aggregates_and_reports_errors(self, http_server: dict[str, str]) -> None:
        agent = ResearchAgent()
        out = agent.research([f"{http_server['base']}/ok", "http://127.0.0.1:1/unreachable"])
        assert len(out["citations"]) == 1
        assert "unreachable" in out["errors"] or len(out["errors"]) == 1
        assert "[Test Page]" in out["extract"]

    def test_blocks_non_http_scheme(self) -> None:
        agent = ResearchAgent()
        with pytest.raises(ValueError, match="blocked"):
            agent.fetch("file:///etc/passwd")


class TestDocuments:
    def _agent(self) -> DocumentAgent:
        return DocumentAgent(Path(tempfile.mkdtemp()))

    def test_docx_roundtrip(self) -> None:
        out = self._agent().generate(
            "docx", "report-doc", {"title": "T", "paragraphs": ["para one", "para two"]}
        )
        assert out.exists() and out.stat().st_size > 1000
        from docx import Document

        doc = Document(str(out))
        assert doc.paragraphs[0].text == "T"

    def test_xlsx_roundtrip(self) -> None:
        out = self._agent().generate(
            "xlsx",
            "book",
            {"sheets": {"data": [[1, 2], [3, 4]]}},
        )
        assert out.exists()
        import openpyxl

        wb = openpyxl.load_workbook(str(out))
        assert wb["data"]["A2"].value == 3

    def test_pdf_roundtrip(self) -> None:
        out = self._agent().generate(
            "pdf", "report-pdf", {"title": "PDF Title", "paragraphs": ["line one"]}
        )
        assert out.exists() and out.read_bytes()[:4] == b"%PDF"

    def test_pptx_roundtrip(self) -> None:
        out = self._agent().generate(
            "pptx",
            "deck",
            {
                "title": "Deck",
                "subtitle": "sub",
                "slides": [{"heading": "S1", "bullets": ["a", "b"]}],
            },
        )
        assert out.exists() and out.stat().st_size > 1000

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(DocumentError):
            self._agent().generate("exe", "bad", {})
