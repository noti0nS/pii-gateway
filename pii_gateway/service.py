"""Sanitiza, chama o LLM e reidrata. O texto claro não entra no prompt."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from pii_gateway.config import Settings
from pii_gateway.db import Stores
from pii_gateway.detect import (
    Detection,
    Term,
    allow_spans,
    build_dictionary,
    detect_dictionary,
    detect_l1,
    is_allowed,
    merge_detections,
)
from pii_gateway.llm import SYSTEM_PROMPT, LLM, MockLLM, build_llm
from pii_gateway.ner import detect_l2
from pii_gateway.surrogate import SURROGATE_RE

logger = logging.getLogger("pii_gateway")

Ner = Callable[[str], list[Detection]]


@dataclass
class Sanitized:
    text: str
    detections: list[dict[str, object]]


@dataclass
class AskResult:
    original: str
    question: str
    sent_to_llm: str
    llm_answer: str
    rehydrated: str
    detections: list[dict[str, object]]
    ner_warning: str | None
    llm_mode: str


class Gateway:
    def __init__(
        self,
        settings: Settings,
        llm: LLM | None = None,
        ner: Ner | None = None,
    ) -> None:
        self.settings = settings
        self.stores = Stores(settings)
        self.llm = build_llm(settings) if llm is None else llm
        self._ner = ner
        self.ner_warning: str | None = None

    def init(self) -> None:
        self.stores.init()

    def reset(self) -> None:
        self.stores.reset()

    def sanitize(self, text: str, terms: list[Term], allow: list[str]) -> Sanitized:
        return self._prepare([text], terms, allow)[0]

    def ask(self, text: str, question: str, terms: list[Term], allow: list[str]) -> AskResult:
        sanitized_text, sanitized_question = self._prepare([text, question], terms, allow)
        sent = f"Texto:\n{sanitized_text.text}\n\nPergunta:\n{sanitized_question.text}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": sent},
        ]
        answer = self.llm.complete(messages)
        return AskResult(
            original=text,
            question=question,
            sent_to_llm=sent,
            llm_answer=answer,
            rehydrated=self.rehydrate(answer),
            detections=sanitized_text.detections,
            ner_warning=self.ner_warning,
            llm_mode="mock" if isinstance(self.llm, MockLLM) else "live",
        )

    def rehydrate(self, text: str) -> str:
        cache: dict[str, str] = {}

        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            cached = cache.get(token)
            if cached is None:
                plain = self.stores.decrypt(token, purpose="rehydrate")
                cached = token if plain is None else plain
                cache[token] = cached
            return cached

        return SURROGATE_RE.sub(replace, text)

    def _prepare(self, texts: list[str], terms: list[Term], allow: list[str]) -> list[Sanitized]:
        self.ner_warning = None
        l2_groups = [self._l2(text) for text in texts]
        dictionary = build_dictionary(
            terms,
            [hit for group in l2_groups for hit in group],
            allow,
        )
        return [
            self._substitute(text, l2, dictionary, allow)
            for text, l2 in zip(texts, l2_groups, strict=True)
        ]

    def _l2(self, text: str) -> list[Detection]:
        if self._ner is not None:
            return list(self._ner(text))
        if not self.settings.enable_ner:
            return []
        try:
            return detect_l2(text, model_id=self.settings.gliner_model)
        except Exception:
            self.ner_warning = "L2 indisponível; a detecção seguiu só com L1 e L3."
            logger.warning("l2 indisponivel")
            return []

    def _substitute(
        self,
        text: str,
        l2: list[Detection],
        dictionary: list[Term],
        allow: list[str],
    ) -> Sanitized:
        spans = allow_spans(text, allow)
        candidates = [
            item
            for item in (*detect_l1(text), *l2, *detect_dictionary(text, dictionary))
            if not is_allowed(item, allow, spans)
        ]
        parts: list[str] = []
        detections: list[dict[str, object]] = []
        cursor = 0
        for item in merge_detections(candidates):
            if item.start < cursor or item.end > len(text):
                continue
            surface = text[item.start : item.end]
            if not surface.strip():
                continue
            token = self.stores.surrogate_for(item.type, surface)
            parts.append(text[cursor : item.start])
            parts.append(token)
            detections.append(
                {
                    "type": item.type,
                    "layer": item.layer,
                    "start": item.start,
                    "end": item.end,
                    "surrogate": token,
                }
            )
            cursor = item.end
        parts.append(text[cursor:])
        return Sanitized("".join(parts), detections)
