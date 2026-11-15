"""The paper's title as the report pages show it: plain text, one line, bounded.

The title is paper text, so it is untrusted (invariant 6): it is shown as text only, never
put in a prompt from here, and cleaned of LaTeX markup and of every control, format or other
invisible character (ANSI escapes, bidi overrides, fillers) before it is stored. The input is
cut and its whitespace collapsed before any pattern runs, every quantifier is bounded and the
unwrapping makes a fixed number of passes, so a hostile title costs linear time.
"""

import re
import unicodedata

MAX_TITLE_CHARS = 300  # runs.title is String(300)
_MAX_RAW_CHARS = 4 * MAX_TITLE_CHARS  # markup is dropped, so read a little more than is kept
_UNWRAP_PASSES = 8  # nesting deeper than this keeps its innermost braces dropped, not unwrapped

_OPT = r"(?:\[[^\]]{0,64}\])?"
_DROPPED = re.compile(r"\\(?:thanks|footnote|footnotemark|label) ?" + _OPT + r" ?\{[^{}]{0,400}\}")
_WITH_ARG = re.compile(r"\\[A-Za-z]{1,32}\*? ?" + _OPT + r" ?\{([^{}]{0,400})\}")
_COMMAND = re.compile(r"\\[A-Za-z]{1,32}\*?")
_ESCAPED = re.compile(r"\\([&%_#$])")
_LINE_BREAK = re.compile(r"\\\\" + _OPT)
# Invisible outside the C categories: Hangul fillers, Braille blank, grapheme joiner,
# variation selectors.
_FILLERS = {
    "\u115f",
    "\u1160",
    "\u3164",
    "\uffa0",
    "\u2800",
    "\u034f",
    *map(chr, range(0xFE00, 0xFE10)),
}


def _plain(raw: str) -> str:
    text = " ".join(raw[:_MAX_RAW_CHARS].split())
    text = _LINE_BREAK.sub(" ", text).replace("~", " ")
    text = _DROPPED.sub("", text)
    for _ in range(_UNWRAP_PASSES):  # nested commands, innermost first: \emph{\textbf{X}} -> X
        unwrapped = _WITH_ARG.sub(r"\1", text)
        if unwrapped == text:
            break
        text = unwrapped
    text = _ESCAPED.sub(r"\1", _COMMAND.sub("", text.replace(r"\$", "\x00")))
    text = text.replace("$", "").replace("\x00", "$").replace("{", "").replace("}", "")
    spaced = "".join(" " if ch.isspace() else ch for ch in text)
    visible = "".join(
        ch for ch in spaced if not unicodedata.category(ch).startswith("C") and ch not in _FILLERS
    )
    plain = " ".join(visible.split())
    return plain if any(ch.isalnum() for ch in plain) else ""


def _bounded(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    cut = text[: MAX_TITLE_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "…"


def display_title(arxiv_title: str | None, source_title: str | None) -> str | None:
    """The arXiv listing's title when it has one, else the source's \\title{}; None if neither
    leaves any visible text."""
    for raw in (arxiv_title, source_title):
        if raw:
            text = _plain(raw)
            if text:
                return _bounded(text)
    return None
