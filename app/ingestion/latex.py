"""LaTeX source -> sections and tables. Pure functions (run in the CPU pool, invariant 12).
The text stays untrusted: it is only ever put into prompts through wrap_untrusted().

Everything here is linear in the input: every regex is bounded (no unbounded `.*?` or
`[^}]*` from many start positions), brace matching stops after a few thousand characters and
begin/end pairs are found with forward-only scans, because a hostile source can be crafted to
make naive parsing quadratic.
"""

import bisect
import hashlib
import itertools
import re

from app.domain.paper import IngestedPaper, Section, Table

MAX_DEPTH = 5
MAX_FLATTEN_CHARS = 600_000  # ~ a long paper with appendices; arXiv sources are untrusted
MAX_INPUTS = 500  # input/include resolutions per paper
MAX_FLATTEN_WORK = 8_000_000  # source characters read, summed over every visit
MAX_FILE_BYTES = 2_000_000  # per source file, before decoding
MAX_BRACED = 4_000  # a title or caption longer than this is cut
MAX_TABLES = 200
MAX_SECTIONS = 500
_INPUT = re.compile(r"\\(?:input|include)[ \t]*\{([^{}\n]{0,256})\}")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_SECTION = re.compile(r"\\section\*?[ \t]*\{")
_TABLE_BEGIN = re.compile(r"\\begin\{table\*?\}")
_TABLE_END = re.compile(r"\\end\{table\*?\}")
_TABULAR_SPEC = re.compile(r"\s{0,64}(?:\[[^\]\n]{0,64}\])?\s{0,64}\{[^{}\n]{0,256}\}")
_RULES = re.compile(r"\\(?:hline|toprule|midrule|bottomrule|cline\{[^{}\n]{0,32}\})")
_DOCUMENTCLASS = re.compile(rb"^[^%\n]{0,4096}\\documentclass", re.MULTILINE)


def _text(raw: bytes) -> str:
    return _COMMENT.sub("", raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace"))


def braced(text: str, open_index: int, limit: int = MAX_BRACED) -> str:
    """Content of the {...} group starting at text[open_index] == '{' (nesting-aware), at
    most `limit` characters: an unclosed group never makes the caller scan the whole text."""
    depth = 0
    for i in range(open_index, min(len(text), open_index + limit + 2)):
        if text[i] == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
    return text[open_index + 1 : open_index + 1 + limit]


def _command_arg(text: str, command: str) -> str | None:
    match = re.search(rf"\\{command}[ \t]*\{{", text)
    return braced(text, match.end() - 1).strip() if match else None


def find_main(files: dict[str, bytes]) -> str:
    tex = sorted(name for name in files if name.endswith(".tex"))
    with_class = [n for n in tex if _DOCUMENTCLASS.search(files[n][:MAX_FILE_BYTES])]
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
    output is capped at MAX_FLATTEN_CHARS, and the work at MAX_INPUTS resolutions and
    MAX_FLATTEN_WORK source characters read: a file can input another one many times over,
    even one that expands to nothing."""
    left = {"out": MAX_FLATTEN_CHARS, "inputs": MAX_INPUTS, "work": MAX_FLATTEN_WORK}
    decoded: dict[str, str] = {}

    def exhausted() -> bool:
        return min(left.values()) <= 0

    def expand(name: str, depth: int) -> str:
        left["work"] -= len(files[name])  # charged before any work on the file
        if exhausted():
            return "[input size limit]"
        if name not in decoded:
            decoded[name] = _text(files[name])

        def replace(match: re.Match[str]) -> str:
            target = _resolve(match.group(1))
            if target is None:
                return "[blocked input]"
            if depth + 1 > MAX_DEPTH:
                return "[input depth limit]"
            if target not in files:
                return f"[missing input: {match.group(1).strip()}]"
            left["inputs"] -= 1
            if exhausted():
                return "[input size limit]"
            inlined = expand(target, depth + 1)
            left["out"] -= len(inlined)
            return inlined

        return _INPUT.sub(replace, decoded[name])

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


def _tabular(body: str) -> tuple[str, str] | None:
    """(the whole tabular, its content) for the first tabular in a table body."""
    start = body.find("\\begin{tabular}")
    if start < 0:
        return None
    spec = _TABULAR_SPEC.match(body, start + len("\\begin{tabular}"))
    if spec is None:
        return None
    end = body.find("\\end{tabular}", spec.end())
    if end < 0:
        return None
    return body[start : end + len("\\end{tabular}")], body[spec.end() : end]


def _tables(text: str) -> list[Table]:
    """Pair each table begin with the next end after it (one forward pass, no backtracking)."""
    ends = [m.start() for m in _TABLE_END.finditer(text)]
    tables: list[Table] = []
    position = 0
    for begin in _TABLE_BEGIN.finditer(text):
        if begin.start() < position:
            continue  # nested or overlapping begin: already inside a table
        k = bisect.bisect_left(ends, begin.end())
        if k == len(ends):
            break  # no end left for any later begin either
        body = text[begin.end() : ends[k]]
        position = ends[k]
        tabular = _tabular(body)
        tables.append(
            Table(
                number=len(tables) + 1,
                label=_command_arg(body, "label"),
                caption=_command_arg(body, "caption") or "",
                latex=tabular[0] if tabular else body.strip()[:20_000],
                rows=_rows(tabular[1]) if tabular else [],
            )
        )
        if len(tables) >= MAX_TABLES:
            break
    return tables


def _sections(text: str) -> list[Section]:
    sections = []
    open_tag, close_tag = "\\begin{abstract}", "\\end{abstract}"
    begin = text.find(open_tag)
    end = text.find(close_tag, begin + len(open_tag)) if begin >= 0 else -1
    if end >= 0:
        sections.append(Section(title="Abstract", text=text[begin + len(open_tag) : end].strip()))
    document_end = text.find("\\end{document}")
    matches = list(itertools.islice(_SECTION.finditer(text), MAX_SECTIONS))
    for i, match in enumerate(matches):
        title = braced(text, match.end() - 1, limit=300)
        start = match.end() + len(title) + 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        if 0 <= document_end < end:
            end = max(document_end, start)
        sections.append(Section(title=title.strip(), text=text[start:end].strip()))
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
