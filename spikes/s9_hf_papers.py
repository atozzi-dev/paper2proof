"""Spike S9: does the Hugging Face Papers API return the GitHub repo for an arXiv ID?

Run:   uv run python spikes/s9_hf_papers.py [ARXIV_ID ...]
Needs: nothing (HF_TOKEN optional, only raises rate limits). Free.

For each ID, calls GET /api/papers/<id> and GET /api/arxiv/<id>/repos, prints the top-level
keys (the response shape A-17 must parse) and any github.com URL found anywhere in the JSON.
PASS = at least 2 of the default papers yield a GitHub URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report, http

BASE = "https://huggingface.co/api"
# Papers with well-known official code: LoRA, ViT, ResNet, DDPM.
DEFAULT_IDS = ["2106.09685", "2010.11929", "1512.03385", "2006.11239"]
GITHUB_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


STRUCTURED_KEYS = ("githubRepo", "github_repo", "repo", "projectPage")
TEXT_KEYS = ("summary", "ai_summary", "abstract")


def github_urls(data: Any) -> list[str]:
    return sorted({u.rstrip(".") for u in GITHUB_RE.findall(json.dumps(data))})


def split_sources(data: Any) -> tuple[list[str], list[str]]:
    """GitHub URLs from structured fields vs. from free text (abstract/summary)."""
    if not isinstance(data, dict):
        return github_urls(data), []
    text = {k: data[k] for k in TEXT_KEYS if k in data}
    rest = {k: v for k, v in data.items() if k not in TEXT_KEYS}
    return github_urls(rest), github_urls(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", default=DEFAULT_IDS, help="arXiv IDs")
    args = parser.parse_args()

    headers = {}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    r = Report()
    hits = structured_hits = 0
    for arxiv_id in args.ids:
        print(f"== {arxiv_id}")
        paper = http("GET", f"{BASE}/papers/{arxiv_id}", headers=headers)
        repos = http("GET", f"{BASE}/arxiv/{arxiv_id}/repos", headers=headers)
        pdata, rdata = paper.json(), repos.json()
        keys = sorted(pdata) if isinstance(pdata, dict) else type(pdata).__name__
        r.fact(f"{arxiv_id}: /papers HTTP {paper.status} {paper.latency_ms} ms keys={keys}")
        if isinstance(pdata, dict):
            for key in STRUCTURED_KEYS:
                if key in pdata:
                    r.fact(f"{arxiv_id}: {key}={pdata[key]!r}")
        rkeys = sorted(rdata) if isinstance(rdata, dict) else type(rdata).__name__
        r.fact(f"{arxiv_id}: /arxiv/repos HTTP {repos.status} keys={rkeys}")
        structured, from_text = split_sources(pdata)
        structured += github_urls(rdata)
        if structured:
            hits += 1
            structured_hits += 1
            r.fact(f"{arxiv_id}: github (structured field) -> {sorted(set(structured))[:5]}")
        elif from_text:
            hits += 1
            r.fact(f"{arxiv_id}: github (abstract text only) -> {from_text[:5]}")
        else:
            r.fact(f"{arxiv_id}: no github URL")
        if paper.status == 429 or repos.status == 429:
            r.partial("rate limited by Hugging Face (429)")

    if hits == 0:
        r.fail("no GitHub URL for any paper -> fallback: Tavily + arXiv abstract parsing")
    elif hits < min(2, len(args.ids)):
        r.partial(f"GitHub URL for only {hits}/{len(args.ids)} papers")
    if hits and not structured_hits:
        r.partial("no structured repo field: URLs come from abstract text only -> Tavily fallback")
    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
