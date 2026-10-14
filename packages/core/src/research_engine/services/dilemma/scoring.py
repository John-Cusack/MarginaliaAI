"""Score shortlisted claim pairs for entailment or contradiction.

Stage 1 shortlists pairs of horn-eligible claims by embedding similarity; this
stage asks a model to judge each pair. A pair is one structured call: the two
statements with their modality and polarity labels, answered as JSON with a
relation (``entail`` | ``contradict`` | ``neutral``), a confidence, and a
one-sentence rationale. An answer that fails schema validation gets one
corrective retry carrying the rejection text, mirroring the extraction
executor; a second failure propagates.

Scoring is idempotent per prompt. Each prompt's sha256 is stored on its row,
so a pair already scored under the same prompt hash and model makes no LLM
call. All judgments are inserted (contradictions and non-contradictions
alike, so the cache holds), but only ``contradict`` judgments at or above
the caller's threshold are returned.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from research_engine.ports.llm import LLMPort

#: What the NLI judgment must satisfy to be accepted, first try or retry.
NLI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["entail", "contradict", "neutral"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string", "minLength": 1},
    },
    "required": ["relation", "confidence", "rationale"],
}


class PairScoreDraft(BaseModel):
    """One pair judgment before it is stored."""

    run_id: UUID
    claim_a_id: UUID
    claim_b_id: UUID
    relation: Literal["entail", "contradict", "neutral"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    llm_call_id: UUID | None = None
    model: str
    prompt_hash: str


class _NLIJudgment(BaseModel):
    """The model's answer, validated before anything is stored."""

    relation: Literal["entail", "contradict", "neutral"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)


class PairScorer:
    """Judges shortlisted claim pairs with one structured call each."""

    def __init__(
        self, llm: LLMPort, pair_scores: Any, *, caller: str = "dilemma"
    ) -> None:
        self._llm = llm
        self._pair_scores = pair_scores
        self._caller = caller

    async def score_pairs(
        self,
        run_id: UUID,
        pairs: list[tuple[UUID, UUID]],
        claim_texts: dict[UUID, tuple[str, str, str]],
        tau: float,
        model: str,
        prompt_version: str,
    ) -> list[PairScoreDraft]:
        """Judge each pair, insert every judgment, return kept contradictions.

        ``claim_texts`` maps each claim id to ``(statement, modality,
        polarity)``. Pairs already scored under the same prompt hash and
        model make no LLM call. Returned drafts are the ``contradict``
        judgments at or above ``tau``, fresh or cached; every fresh judgment
        is inserted, whatever its relation.
        """
        existing = await self._pair_scores.for_run(run_id)
        scored = {
            (
                _field(row, "claim_a_id"),
                _field(row, "claim_b_id"),
                _field(row, "prompt_hash"),
                _field(row, "model"),
            )
            for row in existing
        }
        fresh: list[PairScoreDraft] = []
        kept: list[PairScoreDraft] = []
        for first, second in pairs:
            a, b = (
                (first, second) if str(first) < str(second) else (second, first)
            )
            if a == b or a not in claim_texts or b not in claim_texts:
                continue
            prompt = _render_prompt(
                claim_texts[a], claim_texts[b], prompt_version
            )
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            if (a, b, prompt_hash, model) in scored:
                cached = _kept_cached(existing, a, b, prompt_hash, model, tau)
                kept.extend(cached)
                continue
            judgment, call_id = await self._ask(prompt, model)
            draft = PairScoreDraft(
                run_id=run_id,
                claim_a_id=a,
                claim_b_id=b,
                relation=judgment.relation,
                confidence=judgment.confidence,
                rationale=judgment.rationale,
                llm_call_id=call_id,
                model=model,
                prompt_hash=prompt_hash,
            )
            fresh.append(draft)
            if _kept(draft, tau):
                kept.append(draft)
        if fresh:
            await self._pair_scores.insert(run_id, fresh)
        return kept

    async def _ask(
        self, prompt: str, model: str
    ) -> tuple[_NLIJudgment, UUID]:
        """One call, then at most one corrective retry carrying the error text."""
        response, call_id = await self._llm.structured(
            messages=[{"role": "user", "content": prompt}],
            schema=NLI_SCHEMA,
            model=model,
            caller=self._caller,
            purpose="nli_scoring",
        )
        try:
            return _NLIJudgment.model_validate(response), call_id
        except ValidationError as exc:
            # Bound to a name here because Python unbinds the exception
            # variable on leaving the block, and the retry needs the text.
            rejection = str(exc)

        retry_prompt = (
            f"{prompt}\n\nYour previous answer was rejected: {rejection}\n"
            "Return the same judgment with that corrected, as JSON with "
            "relation, confidence, and rationale."
        )
        response, call_id = await self._llm.structured(
            messages=[{"role": "user", "content": retry_prompt}],
            schema=NLI_SCHEMA,
            model=model,
            caller=self._caller,
            purpose="nli_scoring_retry",
        )
        return _NLIJudgment.model_validate(response), call_id


def _render_prompt(
    a: tuple[str, str, str], b: tuple[str, str, str], prompt_version: str
) -> str:
    """The NLI prompt for one pair; its sha256 is the idempotency key."""
    (a_statement, a_modality, a_polarity) = a
    (b_statement, b_modality, b_polarity) = b
    return (
        f"NLI scoring prompt {prompt_version}:\n"
        "Judge the logical relation between the two statements below. "
        "Answer entail when A makes B true, contradict when they cannot "
        "both be true as stated, and neutral otherwise. Modality says how "
        "a statement was presented (for instance, a question or a "
        "hypothetical does not assert), and polarity says whether its "
        "passage put it forward or denied it; weigh both before judging.\n"
        f"Statement A (modality: {a_modality}; polarity: {a_polarity}): "
        f'"{a_statement}"\n'
        f"Statement B (modality: {b_modality}; polarity: {b_polarity}): "
        f'"{b_statement}"\n'
        "Reply as JSON with three keys: relation (one of entail, "
        "contradict, neutral), confidence (0 to 1), and rationale (one "
        "sentence).\n"
        "Example (invented): A (modality: asserts; polarity: affirms): "
        '"The caravan paid the gate toll." '
        "B (modality: asserts; polarity: affirms): "
        '"The caravan paid no toll at the gate." '
        'Answer: {"relation": "contradict", "confidence": 0.95, '
        '"rationale": "Paying the toll and paying no toll cannot both '
        'be true of the same crossing."}'
    )


def _field(row: Any, name: str) -> Any:
    """One column of a stored row, by attribute or by key."""
    return row[name] if isinstance(row, dict) else getattr(row, name)


def _kept_cached(
    existing: list[Any],
    a: UUID,
    b: UUID,
    prompt_hash: str,
    model: str,
    tau: float,
) -> list[PairScoreDraft]:
    """Kept drafts for a pair scored under the same prompt hash and model."""
    kept: list[PairScoreDraft] = []
    for row in existing:
        if (
            _field(row, "claim_a_id") != a
            or _field(row, "claim_b_id") != b
            or _field(row, "prompt_hash") != prompt_hash
            or _field(row, "model") != model
        ):
            continue
        draft = PairScoreDraft.model_validate(
            {
                "run_id": _field(row, "run_id"),
                "claim_a_id": _field(row, "claim_a_id"),
                "claim_b_id": _field(row, "claim_b_id"),
                "relation": _field(row, "relation"),
                "confidence": _field(row, "confidence"),
                "rationale": _field(row, "rationale"),
                "llm_call_id": _field(row, "llm_call_id"),
                "model": _field(row, "model"),
                "prompt_hash": _field(row, "prompt_hash"),
            }
        )
        if _kept(draft, tau):
            kept.append(draft)
    return kept


def _kept(draft: PairScoreDraft, tau: float) -> bool:
    return draft.relation == "contradict" and draft.confidence >= tau
