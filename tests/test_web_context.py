import io

from pypdf import PdfWriter

import writer.web_context as web_context_mod
from writer.web_context import _extract_pdf_text, _is_excluded, fetch_page, scrape_source


class FakeResponse:
    def __init__(self, text="", content=b"", headers=None, status_code=200):
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        pass


class TestIsExcluded:
    def test_wikipedia_domain_excluded(self):
        assert _is_excluded("https://en.wikipedia.org/wiki/Rocket_Lab") is True

    def test_normal_domain_not_excluded(self):
        assert _is_excluded("https://techcrunch.com/rocket-lab") is False

    def test_empty_url_not_excluded(self):
        assert _is_excluded("") is False


class TestFetchPage:
    def test_no_url_returns_error_dict(self):
        result = fetch_page("")
        assert isinstance(result, dict)
        assert "error" in result

    def test_excluded_domain_returns_error_dict(self):
        result = fetch_page("https://en.wikipedia.org/wiki/Rocket_Lab")
        assert isinstance(result, dict)
        assert result["error"] == "excluded domain"

    def test_filters_boilerplate_paragraphs(self, monkeypatch):
        html = (
            "<html><body>"
            "<p>Please accept all cookies to continue</p>"
            "<p>This is the real article content that matters.</p>"
            "</body></html>"
        )
        monkeypatch.setattr(
            web_context_mod.requests, "get", lambda *a, **k: FakeResponse(text=html)
        )
        result = fetch_page("https://example.com/article")
        assert isinstance(result, str)
        assert "cookie" not in result.lower()
        assert "real article content" in result

    def test_request_failure_returns_error_dict_not_empty_string(self, monkeypatch):
        def raise_err(*a, **k):
            raise web_context_mod.requests.RequestException("connection timed out")

        monkeypatch.setattr(web_context_mod.requests, "get", raise_err)
        result = fetch_page("https://example.com/broken")
        assert isinstance(result, dict)
        assert "connection timed out" in result["error"]

    def test_no_readable_body_returns_error_dict(self, monkeypatch):
        monkeypatch.setattr(
            web_context_mod.requests, "get", lambda *a, **k: FakeResponse(text="<html><body></body></html>")
        )
        result = fetch_page("https://example.com/empty")
        assert isinstance(result, dict)
        assert "error" in result

    def test_pdf_content_type_routes_to_pdf_extraction(self, monkeypatch):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        monkeypatch.setattr(
            web_context_mod.requests,
            "get",
            lambda *a, **k: FakeResponse(
                content=buf.getvalue(), headers={"Content-Type": "application/pdf"}
            ),
        )
        result = fetch_page("https://example.com/whitepaper.pdf")
        # A blank page has no extractable text — confirms the PDF branch ran
        # (not the HTML branch) and reported that honestly instead of
        # silently returning "".
        assert isinstance(result, dict)
        assert "PDF" in result["error"]


class TestExtractPdfText:
    def test_blank_pdf_returns_empty_string(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        assert _extract_pdf_text(buf.getvalue(), max_chars=1000) == ""


class TestScrapeSource:
    def test_returns_string_on_success(self, monkeypatch):
        monkeypatch.setattr(
            web_context_mod,
            "fetch_page",
            lambda url, max_chars: "some article text",
        )
        assert scrape_source("https://example.com/article") == "some article text"

    def test_returns_empty_string_on_error_dict(self, monkeypatch):
        monkeypatch.setattr(
            web_context_mod,
            "fetch_page",
            lambda url, max_chars: {"error": "boom", "url": url},
        )
        assert scrape_source("https://example.com/broken") == ""
