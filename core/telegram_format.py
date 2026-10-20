"""
Markdown -> Telegram HTML conversion and message splitting, shared by the bot's
replies and its scheduled messages (morning briefing).
"""
import html
import re

# Telegram caps messages at 4096 chars; leave headroom for the HTML tags the
# markdown conversion adds.
MAX_CHUNK = 3500

# The agents answer in regular Markdown (**bold**, # headings, ``` blocks), which
# Telegram's own Markdown modes don't parse correctly. Converting to Telegram's
# small HTML subset is far more forgiving; if Telegram still rejects it, we fall
# back to plain text.

_CODE_BLOCK = re.compile(r"```[\w+-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[*-]\s+", re.M)


def _inline_md_to_html(text: str) -> str:
    # Pull inline code out first so nothing inside it gets formatted.
    codes = []

    def stash(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _BOLD.sub(r"<b>\2</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def md_to_telegram_html(text: str) -> str:
    out = []
    pos = 0
    for m in _CODE_BLOCK.finditer(text):
        out.append(_inline_md_to_html(text[pos:m.start()]))
        out.append(f"<pre>{html.escape(m.group(1).rstrip())}</pre>")
        pos = m.end()
    out.append(_inline_md_to_html(text[pos:]))
    return "".join(out)


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Splits on line boundaries, keeping ``` code fences balanced in every chunk."""
    chunks, current, in_code = [], "", False
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single monster line - hard-split it
            chunks.append(current + line[:limit])
            current, line = "", line[limit:]
        if len(current) + len(line) > limit and current:
            if in_code:
                current += "\n```"
            chunks.append(current)
            current = "```\n" if in_code else ""
        current += line
        if line.lstrip().startswith("```"):
            in_code = not in_code
    if current.strip():
        chunks.append(current)
    return chunks or [""]
