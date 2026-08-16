from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_HTML_TAG_RE = re.compile(
    r"<(?:html|body|div|p|br|table|tr|td|th|span|style|script)\b",
    re.IGNORECASE,
)
_MANY_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_INLINE_SPACE_RE = re.compile(r"[ \t]+")
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if lowered in _BLOCK_TAGS:
            self.parts.append("\n")
        if lowered == "a":
            href = dict(attrs).get("href")
            self._links.append(href)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if lowered == "a":
            href = self._links.pop() if self._links else None
            if href and not href.lower().startswith(("cid:", "data:")):
                self.parts.append(f" <{href}>")
        if lowered in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def clean_body(value: str | None, content_type: str | None = None) -> str:
    """Create a reversible derived body without modifying the source value."""
    text = value or ""
    is_html = (content_type or "").casefold() == "html" or bool(
        _HTML_TAG_RE.search(text)
    )
    if is_html:
        text = html_to_text(text)
    else:
        text = html.unescape(text)

    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INLINE_SPACE_RE.sub(" ", line).rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = _MANY_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def remove_exact_prior_content(
    current_body: str, earlier_bodies: list[str]
) -> tuple[str, int]:
    """
    Remove only an exact, long copy of an earlier body from a later message.

    This intentionally avoids heuristic signature or reply stripping. If no exact
    match is found, the complete derived body is returned.
    """
    result = current_body
    removed = 0
    for earlier in sorted(
        (body for body in earlier_bodies if len(body) >= 200),
        key=len,
        reverse=True,
    ):
        position = result.find(earlier)
        if position < 40:
            continue
        trailing = result[position + len(earlier) :].strip()
        if len(trailing) > 80:
            continue
        candidate = result[:position].rstrip()
        if len(candidate) < 20:
            continue
        removed += len(result) - len(candidate)
        result = candidate
    return result, removed


def normalized_evidence(value: str) -> str:
    return " ".join(value.split()).casefold()


def evidence_in_text(quote: str, source: str) -> bool:
    normalized_quote = normalized_evidence(quote)
    if len(normalized_quote) < 8:
        return False
    return normalized_quote in normalized_evidence(source)
