"""
DRPL LangChain Tool - Web Fetch

Fetches a single HTTP(S) URL and extracts the readable main content as
Markdown (or plain text) using `trafilatura`. Designed for agents that need
to pull a specific article, tender notice page, or portal listing and
reason over its text rather than do broad search.

- HTTP via `httpx.Client` (sync) with a sane timeout and a browser UA.
- Content extraction via `trafilatura.extract(..., output_format='markdown')`,
  falling back to a plain-text strip if trafilatura bails.
- Rejects non-http(s) schemes and very large bodies up front.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Type
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 DRPL/1.0"
)
_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB hard cap on response body
_MAX_CONTENT_CHARS = 80_000   # cap returned text so we don't blow the agent context


class WebFetchInput(BaseModel):
    url: str = Field(..., description="Absolute http(s) URL to fetch.")
    include_links: bool = Field(
        True,
        description="Keep inline Markdown links in the extracted content.",
    )
    include_tables: bool = Field(
        True,
        description="Preserve table structure in the extracted content.",
    )
    max_chars: Optional[int] = Field(
        None,
        description=(
            "Optional override for the maximum characters returned. Defaults "
            "to 80000. The agent can lower this to save tokens."
        ),
    )


def _clean_plain_text(html: str) -> str:
    """Very small plain-text fallback when trafilatura returns nothing."""
    # Strip scripts/styles and tags; collapse whitespace. Good enough as a
    # last resort — agents will usually prefer the markdown path.
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


def fetch_url_content(
    url: str,
    *,
    include_links: bool = True,
    include_tables: bool = True,
    max_chars: int = _MAX_CONTENT_CHARS,
    timeout_s: float = 20.0,
) -> dict:
    """Fetch + extract. Returns a result dict — never raises for expected errors."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"status": "error", "message": f"Unsupported or malformed URL: {url}"}

    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }

    from app.core.url_safety import BlockedURLError, public_only_hook

    try:
        with httpx.Client(
            timeout=timeout_s,
            follow_redirects=True,
            max_redirects=5,
            headers=headers,
            # Every hop, redirects included, must be a public address: the URL
            # is model-written, and a tender PDF can tell the model what to write.
            event_hooks={"request": [public_only_hook]},
        ) as client:
            resp = client.get(url)
    except BlockedURLError as e:
        return {"status": "error", "message": str(e)}
    except httpx.TimeoutException:
        return {"status": "error", "message": f"Timeout fetching {url}"}
    except httpx.HTTPError as e:
        return {"status": "error", "message": f"HTTP error: {e}"}
    except Exception as e:
        logger.warning(f"web_fetch: unexpected error fetching {url}: {e}")
        return {"status": "error", "message": f"Unexpected error: {e}"}

    if resp.status_code >= 400:
        return {
            "status": "error",
            "message": f"HTTP {resp.status_code} for {url}",
            "url": str(resp.url),
        }

    # Hard cap on body size so a huge page can't crash the worker.
    body = resp.content[:_MAX_BYTES]
    try:
        html = body.decode(resp.encoding or "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    content_type = (resp.headers.get("content-type") or "").lower()
    title = _extract_title(html) if "html" in content_type or "<html" in html[:500].lower() else None

    content: Optional[str] = None
    try:
        import trafilatura
        content = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=include_links,
            include_tables=include_tables,
            include_comments=False,
            favor_recall=True,
        )
    except ImportError:
        logger.warning("web_fetch: trafilatura not installed, using plain-text fallback.")
    except Exception as e:
        logger.debug(f"web_fetch: trafilatura extract failed for {url}: {e}")

    if not content:
        content = _clean_plain_text(html)

    if max_chars and len(content) > max_chars:
        content = content[:max_chars] + "\n\n[... truncated ...]"

    return {
        "status": "success",
        "url": str(resp.url),
        "final_status": resp.status_code,
        "title": title,
        "content": content,
        "content_length": len(content),
    }


class WebFetchTool(BaseTool):
    """Fetch a URL and return its readable main content as markdown."""
    name: str = "web_fetch"
    description: str = (
        "Fetch a single http(s) URL and return its readable main content as "
        "Markdown. Use this when you have a specific page (tender notice, "
        "article, portal listing) and need its text — not for open-ended "
        "search. Returns {status, url, title, content, content_length}."
    )
    args_schema: Type[BaseModel] = WebFetchInput

    def _run(
        self,
        url: str,
        include_links: bool = True,
        include_tables: bool = True,
        max_chars: Optional[int] = None,
    ) -> str:
        result = fetch_url_content(
            url,
            include_links=include_links,
            include_tables=include_tables,
            max_chars=max_chars or _MAX_CONTENT_CHARS,
        )
        return json.dumps(result, default=str)
