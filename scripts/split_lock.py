"""Split a lock compiled with `--emit-index-annotation` by the index each package came from
(A-06). Standard library only: it runs inside the image builds, so each part is installed from
its own index alone (no index mixing at build time either).

Usage: python scripts/split_lock.py <requirements.lock> <out_dir>
Writes <out_dir>/pypi.txt (https://pypi.org/simple) and <out_dir>/pytorch.txt (anything from
download.pytorch.org, with its index URL on the first line as `# index: <url>`).
"""

import re
import sys
from pathlib import Path

PYPI = "https://pypi.org/simple"
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==")
_FROM = re.compile(r"^\s+# from (\S+)\s*$")


def split(lock: str) -> dict[str, str]:
    """index URL -> the lock entries (pin, hashes, comments) that came from it."""
    blocks: list[list[str]] = []
    for line in lock.splitlines():
        if _ENTRY.match(line):
            blocks.append([line])
        elif blocks and line.startswith(" "):
            blocks[-1].append(line)
    parts: dict[str, list[str]] = {}
    for block in blocks:
        sources = [m.group(1) for m in map(_FROM.match, block) if m]
        if len(sources) != 1:
            raise ValueError(f"{block[0].split()[0]}: expected one '# from' annotation")
        parts.setdefault(sources[0], []).extend(block)
    return {url: "\n".join(lines) + "\n" for url, lines in parts.items()}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    parts = split(Path(argv[1]).read_text(encoding="utf-8"))
    out = Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    others = [url for url in parts if url != PYPI]
    if len(others) > 1 or not all(u.startswith("https://download.pytorch.org/") for u in others):
        print(f"unexpected indexes: {others}", file=sys.stderr)
        return 1
    (out / "pypi.txt").write_text(parts.get(PYPI, ""), encoding="utf-8")
    torch = f"# index: {others[0]}\n{parts[others[0]]}" if others else ""
    (out / "pytorch.txt").write_text(torch, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
