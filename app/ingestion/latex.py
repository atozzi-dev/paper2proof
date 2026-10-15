"""LaTeX source -> sections and tables. Pure functions (run in the CPU pool, invariant 12).
The text stays untrusted: it is only ever put into prompts through wrap_untrusted()."""

import hashlib
import re

from app.domain.paper import IngestedPaper, Section, Table

MAX_DEPTH = 5
MAX_FLATTEN_CHARS = 600_000  # ~ a long paper with appendices; arXiv sources are untrusted
_INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_SECTION = re.compile(r"\\section\*?\s*\{")
_TABLE = re.compile(r"\\begin\{table\*?\}(.*?)\\end\{table\*?\}", re.DOTALL)
_TABULAR = re.compile(
    r"\\begin\{tabular\}\s*(?:\[[^]]*\])?\s*\{[^}]*\}(.*?)\\end\{tabular\}", re.DOTALL
)
_RULES = re.compile(r"\\(?:hline|toprule|midrule|bottomrule|cline\{[^}]*\})")


def _text(raw: bytes) -> str:
    return _COMMENT.sub("", raw.decode("utf-8", errors="replace"))


def braced(text: str, open_index: int) -> str:
    """Content of the {...} group starting at text[open_index] == '{' (nesting-aware)."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
    return text[open_index + 1 :]


def _command_arg(text: str, command: str) -> str | None:
    match = re.search(rf"\\{command}\s*\{{", text)
    return braced(text, match.end() - 1).strip() if match else None


def find_main(files: dict[str, bytes]) -> str:
    tex = sorted(name for name in files if name.endswith(".tex"))
    with_class = [n for n in tex if b"\\documentclass" in files[n]]
    for name in with_class:
        if b"\\begin{document}" in files[name]:
            return name
    if with_class:
        return with_class[0]
    if tex:
        return tex[0]
    raise ValueError("no .tex file in the source")


def _resolve(name: str) -> str | None:
    name = name.strip().replace("\\", "/")
    if not name or name.startswith("/") or ".." in name.split("/"):
        return None
    return name if name.endswith(".tex") else f"{name}.tex"


def flatten(files: dict[str, bytes], main: str) -> str:
    """Inline \\input/\\include up to MAX_DEPTH levels; never reads outside the archive. The
    output is capped at MAX_FLATTEN_CHARS: a file can input another one many times over."""
    left = [MAX_FLATTEN_CHARS]

    def expand(name: str, depth: int) -> str:
        text = _text(files[name])
        if left[0] <= 0:
            return "[input size limit]"

        def replace(match: re.Match[str]) -> str:
            target = _resolve(match.group(1))
            if target is None:
                return "[blocked input]"
            if depth + 1 > MAX_DEPTH:
                return "[input depth limit]"
            if target not in files:
                return f"[missing input: {match.group(1).strip()}]"
            if left[0] <= 0:
                return "[input size limit]"
            inlined = expand(target, depth + 1)
            left[0] -= len(inlined)
            return inlined

        return _INPUT.sub(replace, text)

    out = expand(main, 0)
    if len(out) > MAX_FLATTEN_CHARS:
        out = out[:MAX_FLATTEN_CHARS] + "[input size limit]"
    return out


def _rows(tabular: str) -> list[list[str]]:
    rows = []
    for line in re.split(r"\\\\", _RULES.sub("", tabular)):
        cells = [c.strip() for c in line.split("&")]
        if any(cells):
            rows.append(cells)
    return rows


def _tables(text: str) -> list[Table]:
    tables = []
    for number, match in enumerate(_TABLE.finditer(text), start=1):
        body = match.group(1)
        tabular = _TABULAR.search(body)
        tables.append(
            Table(
                number=number,
                label=_command_arg(body, "label"),
                caption=_command_arg(body, "caption") or "",
                latex=tabular.group(0) if tabular else body.strip(),
                rows=_rows(tabular.group(1)) if tabular else [],
            )
        )
    return tables


def _sections(text: str) -> list[Section]:
    sections = []
    abstract = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", text, re.DOTALL)
    if abstract:
        sections.append(Section(title="Abstract", text=abstract.group(1).strip()))
    matches = list(_SECTION.finditer(text))
    for i, match in enumerate(matches):
        title = braced(text, match.end() - 1)
        start = match.end() + len(title) + 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].split("\\end{document}")[0]
        sections.append(Section(title=title.strip(), text=body.strip()))
    return sections


def parse_latex(files: dict[str, bytes], *, arxiv_id: str, title: str | None) -> IngestedPaper:
    text = flatten(files, find_main(files))
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\x00" + files[name])
    return IngestedPaper(
        arxiv_id=arxiv_id,
        sha256=digest.hexdigest(),
        title=_command_arg(text, "title") or title,
        sections=_sections(text),
        tables=_tables(text),
        source_kind="latex",
    )
