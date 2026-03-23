"""Tool for fetching web pages and converting them to readable text."""

import hashlib
import logging
import mimetypes
import time
from pathlib import Path

import html2text
import httpx

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# Cache: url -> (timestamp, content_text)
_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL_SEC = 15 * 60  # 15 minutes

# Binary content types that should be saved to disk
_BINARY_PREFIXES = (
    "application/pdf",
    "application/octet-stream",
    "application/zip",
    "audio/",
    "video/",
    "image/",
    "application/vnd.",  # Office docs
    "application/msword",
)

_MAX_CONTENT_LENGTH = 80_000  # chars — keep tool results manageable


def _is_binary(content_type: str) -> bool:
    return any(content_type.startswith(p) for p in _BINARY_PREFIXES)


def _cache_get(url: str) -> str | None:
    """Return cached content if fresh, else None."""
    if url in _cache:
        ts, content = _cache[url]
        if time.monotonic() - ts < _CACHE_TTL_SEC:
            return content
        del _cache[url]
    return None


def _cache_set(url: str, content: str) -> None:
    # Evict expired entries (simple self-cleaning)
    now = time.monotonic()
    expired = [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL_SEC]
    for k in expired:
        del _cache[k]
    _cache[url] = (now, content)


def _html_to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0  # no wrapping
    h.skip_internal_links = True
    return h.handle(html)


def _save_binary(url: str, data: bytes, content_type: str) -> Path:
    """Save binary content to /tmp and return the path."""
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    path = Path(f"/tmp/web_fetch_{url_hash}{ext}")
    path.write_bytes(data)
    return path


class WebFetchTool(LocalTool):
    """Fetch a URL and return its content as readable text."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a URL and return its content. HTML pages are converted to "
                "markdown for readability. Binary content (PDFs, images, etc.) is "
                "saved to disk and the file path is returned. Responses are cached "
                "for 15 minutes."
            ),
            args=[
                ArgSpec(
                    name="url",
                    type=str,
                    description="The URL to fetch.",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, url: str, **kwargs) -> ToolResult:  # pyre-ignore[14]
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        # Check cache
        cached = _cache_get(url)
        if cached is not None:
            return ToolResult.success(f"[cached] {cached}")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers={"User-Agent": "Yarvis/1.0"},
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as e:
            return ToolResult.error(f"HTTP error fetching {url}: {e}")

        content_type = resp.headers.get("content-type", "text/html").lower()

        redirect_note = ""
        if str(resp.url) != url:
            redirect_note = f"[redirected to {resp.url}]\n\n"

        if resp.status_code >= 400:
            return ToolResult.error(
                f"HTTP {resp.status_code} for {url}\n{resp.text[:2000]}"
            )

        # Binary content → save to disk
        if _is_binary(content_type):
            path = _save_binary(url, resp.content, content_type)
            result = f"{redirect_note}Binary content ({content_type}) saved to: {path} ({len(resp.content)} bytes)"
            _cache_set(url, result)
            return ToolResult.success(result)

        # Text/HTML content → convert to markdown
        text = resp.text
        if "html" in content_type:
            text = _html_to_markdown(text)

        if len(text) > _MAX_CONTENT_LENGTH:
            text = (
                text[:_MAX_CONTENT_LENGTH]
                + f"\n\n[truncated — {len(resp.text)} chars total]"
            )

        result = f"{redirect_note}{text}"
        _cache_set(url, result)
        return ToolResult.success(result)


def build_web_fetch_tools() -> list[LocalTool]:
    return [WebFetchTool()]
