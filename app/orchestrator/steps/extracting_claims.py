"""EXTRACTING_CLAIMS: Reader extracts claims (the user's claim hint, as untrusted data, says
which first), Vision/LaTeX tables fill missing numbers, metric names are normalized, claims are
ranked and the top 3 (or the hinted ones) selected."""

import asyncio
import base64
import hashlib
import re

from pydantic import BaseModel, Field

from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import Claim, ClaimType, ErrorCode, StepName
from app.domain.paper import IngestedPaper, Table
from app.models.client import Message, ModelClient, Role
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.storage.artifacts import ArtifactStore

VISION_SKIPPED = "VISION_PAGE_SKIPPED"

MAX_PAPER_CHARS = 300_000
MAX_HINT_CHARS = 500  # the API's limit on claim_hint
HINT_SOURCE = "claim-hint"  # the Reader prompt names this block
READER_PROMPT = "reader_extract_claims"
MAX_CLAIMS = 15
SELECTED = 3
_TABLE_REF = re.compile(r"\btab(?:le|\.)?\s*(\d+)", re.IGNORECASE)
_SNAKE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ReaderClaim(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    claim_type: ClaimType
    metric: str = Field(min_length=1, max_length=200)
    higher_is_better: bool
    dataset: str = Field(max_length=200)
    setting: str = Field(max_length=500)
    reported_value: float | None
    reported_std: float | None = None
    baseline_name: str | None = Field(default=None, max_length=200)
    claimed_delta: float | None = None
    source_ref: str = Field(max_length=100)


class ReaderOutput(BaseModel):
    claims: list[ReaderClaim] = Field(max_length=MAX_CLAIMS)


class Row(BaseModel):
    name: str = Field(max_length=200)
    values: list[float | None] = Field(max_length=30)


class TableNumbers(BaseModel):
    table_label: str | None = Field(max_length=50)
    rows: list[Row] = Field(max_length=100)


class MetricNorm(BaseModel):
    metric: str = Field(max_length=64)
    higher_is_better: bool


# Deterministic aliases first; the Utility model only sees names not covered here.
_ALIASES: dict[str, tuple[str, bool]] = {
    "top1": ("top1_accuracy", True),
    "top1acc": ("top1_accuracy", True),
    "top1accuracy": ("top1_accuracy", True),
    "top5": ("top5_accuracy", True),
    "top5acc": ("top5_accuracy", True),
    "top5accuracy": ("top5_accuracy", True),
    "acc": ("accuracy", True),
    "accuracy": ("accuracy", True),
    "testaccuracy": ("accuracy", True),
    "f1": ("f1", True),
    "f1score": ("f1", True),
    "auc": ("auc", True),
    "rocauc": ("auc", True),
    "auroc": ("auc", True),
    "loss": ("loss", False),
    "testloss": ("loss", False),
    "error": ("error_rate", False),
    "errorrate": ("error_rate", False),
    "ppl": ("perplexity", False),
    "perplexity": ("perplexity", False),
    "bleu": ("bleu", True),
    "bleu4": ("bleu", True),
    "map": ("map", True),
    "miou": ("miou", True),
    "rmse": ("rmse", False),
    "mae": ("mae", False),
}


def normalize_metric(raw: str) -> tuple[str, bool] | None:
    key = re.sub(r"\(.*?\)|%", "", raw.lower())
    key = re.sub(r"[^a-z0-9]", "", key)
    return _ALIASES.get(key)


def _table_number(source_ref: str) -> int | None:
    match = _TABLE_REF.search(source_ref)
    return int(match.group(1)) if match else None


def _row_value(rows: list[tuple[str, list[float | None]]], claim: Claim) -> float | None:
    """First numeric value of the row named in the claim's text (the method), falling back to
    its setting. Longest names first, so "ResNet-18" doesn't match inside "ResNet-18-wide"."""
    by_length = sorted(rows, key=lambda r: -len(r[0]))
    for haystack in (claim.text.lower(), claim.setting.lower()):
        for name, values in by_length:
            if name and name.lower() in haystack:
                return next((v for v in values if v is not None), None)
    return None


def _latex_rows(table: Table) -> list[tuple[str, list[float | None]]]:
    rows = []
    for cells in table.rows:
        values: list[float | None] = []
        for cell in cells[1:]:
            match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*(?:\\?%)?\s*", cell)
            values.append(float(match.group(1)) if match else None)
        rows.append((cells[0], values))
    return rows


def _score(claim: Claim) -> int:
    verifiable = (claim.claim_type is ClaimType.ABSOLUTE and claim.reported_value is not None) or (
        claim.claim_type is ClaimType.RELATIVE and bool(claim.baseline_name)
    )
    return (
        3 * verifiable + 2 * bool(claim.dataset.strip()) + (claim.claim_type is ClaimType.RELATIVE)
    )


def reader_messages(paper: IngestedPaper, hint: str | None = None) -> list[Message]:
    """The Reader request for a paper: the prompt plus the paper wrapped as untrusted data, and
    the user's claim hint (if any) after it in its own untrusted block: it is user-typed text.
    One user message, so models whose chat template wants alternating roles still accept it.
    Shared with the extraction eval (eval/run_set_c.py), so the eval measures this prompt."""
    prompt = load_prompt(READER_PROMPT)
    text = "\n\n".join(f"## {s.title}\n{s.text}" for s in paper.sections)
    user = wrap_untrusted(text, source=f"arxiv:{paper.arxiv_id}", max_chars=MAX_PAPER_CHARS)
    if hint:
        user += "\n\n" + wrap_untrusted(hint, source=HINT_SOURCE, max_chars=MAX_HINT_CHARS)
    return [
        Message(role="system", content=f"{prompt.body}\n{UNTRUSTED_NOTICE}"),
        Message(role="user", content=user),
    ]


TableRows = dict[int, list[tuple[str, list[float | None]]]]


def latex_table_rows(paper: IngestedPaper) -> TableRows:
    return {t.number: _latex_rows(t) for t in paper.tables}


def fill_from_tables(claim: Claim, table_rows: TableRows) -> Claim:
    """An ABSOLUTE claim without a number takes it from the table its source_ref names, if it
    can. A RELATIVE claim never does: the row its text names can be the baseline's, and its
    reported_value (the method's own) can make the verdict INCONCLUSIVE (engine.py)."""
    number = _table_number(claim.source_ref)
    absolute = claim.claim_type is ClaimType.ABSOLUTE
    if absolute and claim.reported_value is None and number in table_rows:
        value = _row_value(table_rows[number], claim)
        if value is not None:
            return claim.model_copy(update={"reported_value": value})
    return claim


def select(claims: list[Claim], hint: str | None) -> set[str]:
    hinted = []
    if hint:
        needle = hint.lower()
        hinted = [c.id for c in claims if needle in c.text.lower() or needle == c.id.lower()]
    ranked = sorted(claims, key=lambda c: -_score(c))  # stable: ties keep the paper's order
    chosen = list(dict.fromkeys([*hinted, *(c.id for c in ranked)]))
    return set(chosen[:SELECTED])


class ExtractingClaimsStep:
    name = StepName.EXTRACTING_CLAIMS

    def __init__(self, models: ModelClient, artifacts: ArtifactStore) -> None:
        self._models = models
        self._artifacts = artifacts

    async def _read(
        self, ctx: RunContext, paper: IngestedPaper, hint: str | None
    ) -> list[ReaderClaim]:
        output = await self._models.structured(
            Role.READER,
            reader_messages(paper, hint),
            ReaderOutput,
            run_id=ctx.run_id,
            step=ctx.step.value,
            prompt=READER_PROMPT,
        )
        return output.claims

    async def _normalize(self, ctx: RunContext, raw: ReaderClaim) -> tuple[str, bool]:
        known = normalize_metric(raw.metric)
        if known:
            return known
        prompt = load_prompt("utility_normalize_claim")
        user = prompt.render(
            metric=wrap_untrusted(raw.metric, source="claim-metric", max_chars=200)
        )
        norm = await self._models.structured(
            Role.UTILITY,
            [Message(role="system", content=UNTRUSTED_NOTICE), Message(role="user", content=user)],
            MetricNorm,
            run_id=ctx.run_id,
            step=ctx.step.value,
            prompt=prompt.name,
        )
        metric = norm.metric if _SNAKE.fullmatch(norm.metric) else "metric"
        return metric, norm.higher_is_better

    async def _vision_rows(
        self, ctx: RunContext, paper: IngestedPaper
    ) -> dict[int, list[tuple[str, list[float | None]]]]:
        """Table number -> rows read by the Vision role from rasterized pages (PDF papers). A
        page the model can't read is skipped with a warning: the others still count."""
        prompt = load_prompt("vision_table_numbers")
        tables: dict[int, list[tuple[str, list[float | None]]]] = {}
        for image in paper.page_images:
            png = await asyncio.to_thread(self._artifacts.get_bytes, image.key)
            url = "data:image/png;base64," + base64.b64encode(png).decode()
            content: list[dict[str, object]] = [
                {"type": "text", "text": prompt.body},
                {"type": "image_url", "image_url": {"url": url}},
            ]
            try:
                read = await self._models.structured(
                    Role.VISION,
                    [Message(role="user", content=content)],
                    TableNumbers,
                    run_id=ctx.run_id,
                    step=ctx.step.value,
                    prompt=prompt.name,
                )
            except P2PError as err:  # budget and kill switch are not P2PError: they still stop
                await ctx.emit(
                    EventType.WARNING,
                    {
                        "code": VISION_SKIPPED,
                        "message": f"page {image.page} was not read ({err.code.value})",
                    },
                )
                continue
            number = _table_number(read.table_label or "")
            if number is not None:
                tables[number] = [(r.name, r.values) for r in read.rows]
        return tables

    async def run(self, ctx: RunContext) -> StepResult:
        key = await output_of(ctx, StepName.INGESTING)
        paper = IngestedPaper.model_validate(await asyncio.to_thread(self._artifacts.get_json, key))
        run = await ctx.store.get_run(ctx.run_id)
        hint = run.claim_hint if run else None
        drafts = await self._read(ctx, paper, hint)
        if not drafts:
            raise P2PError(ErrorCode.NO_CLAIMS, "no testable quantitative claims found")

        table_rows = latex_table_rows(paper)
        if paper.page_images and any(d.reported_value is None for d in drafts):
            table_rows.update(await self._vision_rows(ctx, paper))

        claims: list[Claim] = []
        for index, draft in enumerate(drafts, start=1):
            metric, higher = await self._normalize(ctx, draft)
            claim = Claim(
                id=f"c{index}",
                **draft.model_dump(exclude={"metric", "higher_is_better"}),
                metric=metric,
                higher_is_better=higher,
            )
            claims.append(fill_from_tables(claim, table_rows))

        chosen = select(claims, hint)
        await ctx.store.save_claims(ctx.run_id, claims, selected=chosen)
        for claim in claims:
            await ctx.emit(EventType.CLAIM_EXTRACTED, {"claim": claim.model_dump(mode="json")})
        out = f"runs/{ctx.run_id}/claims.json"
        payload = {  # the hint shapes what the Reader extracts: kept with the claims
            "claims": [c.model_dump(mode="json") for c in claims],
            "selected": sorted(chosen),
            "claim_hint": hint or None,
            "claim_hint_sha256": hashlib.sha256(hint.encode("utf-8")).hexdigest() if hint else None,
        }
        await asyncio.to_thread(self._artifacts.put_json, out, payload)
        return StepResult(next=StepName.DISCOVERING, output_ref=out)
