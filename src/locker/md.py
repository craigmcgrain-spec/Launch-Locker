"""A minimal markdown renderer for untrusted Hugging Face model cards.

Deliberately limited. The standard library has no markdown parser and vendoring
one would break the zero-dependency rule, so fidelity plays second fiddle to a
security property: **the whole document is escaped before any transform runs.**
Raw HTML in a README therefore cannot reach the page as markup, and every tag in
the output is one this module generated.
"""

from __future__ import annotations

import html
import re

MAX_README_CHARS = 200_000

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*&gt;\s?(.*)$")

_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")

_SAFE_SCHEMES = ("http://", "https://")


def _is_safe_url(target: str) -> bool:
    """Only http(s) may become an href."""
    return target.startswith(_SAFE_SCHEMES)


def _inline(text: str) -> str:
    """Apply inline transforms to already-escaped text.

    Generated markup is stashed behind placeholders so that later transforms
    cannot reach inside it: without this, `**a*b*c**` and code spans containing
    asterisks would be mangled by the emphasis rules.
    """
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        return f"\x00{len(stash) - 1}\x00"

    def code(match: re.Match[str]) -> str:
        return keep(f"<code>{match.group(1)}</code>")

    def image(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        if not _is_safe_url(target):
            return match.group(0)
        # Deliberately not an <img>: loading a remote image would turn viewing a
        # model card into a tracking beacon against the reader.
        return keep(f'<a href="{target}">{alt or target}</a>')

    def link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if not _is_safe_url(target):
            return match.group(0)
        return keep(f'<a href="{target}">{label}</a>')

    text = _CODE.sub(code, text)
    text = _IMAGE.sub(image, text)
    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    for index, markup in enumerate(stash):
        text = text.replace(f"\x00{index}\x00", markup)
    return text


def render(text: str) -> str:
    """Render markdown to HTML that is safe to insert into a page.

    Unsupported constructs render as literal escaped text; they never become
    markup and never swallow the rest of the document.
    """
    if not text:
        return ""

    escaped = html.escape(text[:MAX_README_CHARS], quote=True)
    lines = escaped.split("\n")
    out: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        if _FENCE.match(line):
            index += 1
            block: list[str] = []
            while index < len(lines) and not _FENCE.match(lines[index]):
                block.append(lines[index])
                index += 1
            index += 1  # consume the closing fence, or run past the end
            out.append("<pre><code>" + "\n".join(block) + "</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            index += 1
            continue

        match = _QUOTE.match(line)
        if match:
            block = []
            while index < len(lines) and _QUOTE.match(lines[index]):
                block.append(_QUOTE.match(lines[index]).group(1))
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(block))}</blockquote>")
            continue

        match = _BULLET.match(line)
        if match:
            items = []
            while index < len(lines) and _BULLET.match(lines[index]):
                items.append(_BULLET.match(lines[index]).group(1))
                index += 1
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<ul>{body}</ul>")
            continue

        match = _NUMBERED.match(line)
        if match:
            items = []
            while index < len(lines) and _NUMBERED.match(lines[index]):
                items.append(_NUMBERED.match(lines[index]).group(1))
                index += 1
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<ol>{body}</ol>")
            continue

        if not line.strip():
            index += 1
            continue

        out.append(f"<p>{_inline(line)}</p>")
        index += 1

    return "\n".join(out)
