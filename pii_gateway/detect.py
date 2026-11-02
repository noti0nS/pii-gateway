"""L1 (regex + dígito), L3 (dicionário do caso) e fusão de camadas.

Na sobreposição vence o trecho mais longo. Empate de comprimento usa a
prioridade L1 > L3 > L2. Um CPF/CNPJ/e-mail/telefone da L1 que não fique
inteiro dentro do vencedor é preservado: o trecho mais longo é partido
ao redor dele, para não sobrar dígito em claro.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pii_gateway.normalize import fold

ENTITY_TYPES: frozenset[str] = frozenset(
    {"PERSON", "ORG", "CPF", "CNPJ", "EMAIL", "PHONE", "ADDRESS"}
)
LAYER_PRIORITY: dict[str, int] = {"L1": 3, "L3": 2, "L2": 1}

_CPF_RE = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?55[\s.-]*)?(?:\(\d{2}\)|\d{2})[\s.-]*(?:9\d{4}|\d{4})[\s.-]*\d{4}(?!\d)"
)
_ORG_HINT = re.compile(r"\b(?:ltda|s\.a\.|s/a|eireli|me|epp)\b", re.IGNORECASE)
_ADDRESS_HINT = re.compile(
    r"\b(?:rua|avenida|av|alameda|travessa|praca|praça|rodovia)\b",
    re.IGNORECASE,
)
_PARTICLES = frozenset({"de", "da", "do", "dos", "das", "e"})
_ACCENT_GROUPS = {
    "a": "aáàâãäå",
    "c": "cç",
    "e": "eéèêë",
    "i": "iíìîï",
    "n": "nñ",
    "o": "oóòôõö",
    "u": "uúùûü",
    "y": "yýÿ",
}

# DDDs em uso no Brasil. Telefone não tem dígito verificador nacional;
# a validação é de DDD e de forma (móvel com 9, fixo com 2–5).
_DDDS = frozenset(
    {
        11, 12, 13, 14, 15, 16, 17, 18, 19,
        21, 22, 24, 27, 28,
        31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49,
        51, 53, 54, 55,
        61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79,
        81, 82, 83, 84, 85, 86, 87, 88, 89,
        91, 92, 93, 94, 95, 96, 97, 98, 99,
    }
)


@dataclass(frozen=True)
class Detection:
    start: int
    end: int
    text: str
    type: str
    layer: str

    def shrink(self, start: int, end: int) -> Detection:
        rel_start = start - self.start
        rel_end = end - self.start
        return Detection(start, end, self.text[rel_start:rel_end], self.type, self.layer)


@dataclass(frozen=True)
class Term:
    value: str
    type: str


def cpf_valid(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for length in (9, 10):
        total = sum(int(digits[i]) * (length + 1 - i) for i in range(length))
        check = (total * 10) % 11 % 10
        if check != int(digits[length]):
            return False
    return True


def cnpj_valid(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    return _cnpj_digit(digits, 12) == int(digits[12]) and _cnpj_digit(digits, 13) == int(
        digits[13]
    )


def _cnpj_digit(digits: str, length: int) -> int:
    weights = (
        (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
        if length == 12
        else (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    )
    total = sum(int(digits[i]) * weights[i] for i in range(length))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def canonical_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("55") and len(digits) in {12, 13}:
        digits = digits[2:]
    if len(digits) not in {10, 11}:
        return None
    ddd = int(digits[:2])
    if ddd not in _DDDS:
        return None
    if len(digits) == 11 and digits[2] == "9":
        return digits
    if len(digits) == 10 and digits[2] in "2345":
        return digits
    return None


def phone_valid(value: str) -> bool:
    return canonical_phone(value) is not None


def infer_type(value: str) -> str:
    if "@" in value and _EMAIL_RE.fullmatch(value.strip()):
        return "EMAIL"
    if cpf_valid(value):
        return "CPF"
    if cnpj_valid(value):
        return "CNPJ"
    if phone_valid(value):
        return "PHONE"
    if _ORG_HINT.search(value):
        return "ORG"
    if _ADDRESS_HINT.search(fold(value)):
        return "ADDRESS"
    return "PERSON"


def detect_l1(text: str) -> list[Detection]:
    found: list[Detection] = []
    occupied: list[tuple[int, int]] = []

    def take(match: re.Match[str], entity_type: str) -> None:
        span = (match.start(), match.end())
        if any(span[0] < end and start < span[1] for start, end in occupied):
            return
        found.append(
            Detection(span[0], span[1], match.group(0), entity_type, "L1")
        )
        occupied.append(span)

    for match in _CNPJ_RE.finditer(text):
        if cnpj_valid(match.group(0)):
            take(match, "CNPJ")
    for match in _CPF_RE.finditer(text):
        if cpf_valid(match.group(0)):
            take(match, "CPF")
    for match in _EMAIL_RE.finditer(text):
        take(match, "EMAIL")
    for match in _PHONE_RE.finditer(text):
        if canonical_phone(match.group(0)):
            take(match, "PHONE")
    found.sort(key=lambda item: item.start)
    return found


def person_variants(name: str) -> list[str]:
    content: list[str] = []
    for token in name.split():
        bare = fold(token).strip(".")
        if bare in _PARTICLES or len(bare) <= 1:
            continue
        content.append(token)
    if len(content) < 2:
        return []
    initial = content[0][0]
    last = content[-1]
    return [f"{initial}. {last}", f"{initial} {last}"]


def build_dictionary(
    terms: list[Term],
    l2_hits: list[Detection],
    allow: list[str],
) -> list[Term]:
    """Termos do usuário mais a propagação das detecções da L2, com variações."""
    allow_folded = {fold(item) for item in allow if item.strip()}
    entries: list[Term] = []
    seen: set[tuple[str, str]] = set()

    def add(value: str, entity_type: str, *, with_variants: bool) -> None:
        cleaned = value.strip()
        if not cleaned or entity_type not in ENTITY_TYPES:
            return
        key = (fold(cleaned), entity_type)
        if key[0] in allow_folded or key in seen:
            return
        seen.add(key)
        entries.append(Term(cleaned, entity_type))
        if with_variants and entity_type == "PERSON":
            for variant in person_variants(cleaned):
                add(variant, "PERSON", with_variants=False)

    for term in terms:
        add(term.value, term.type, with_variants=True)
    for hit in l2_hits:
        add(hit.text, hit.type, with_variants=True)
    return entries


def _letter_class(char: str) -> str:
    group = _ACCENT_GROUPS.get(char, char)
    pieces: list[str] = []
    seen: set[str] = set()
    for base in group:
        for variant in (base, base.upper()):
            if variant in seen:
                continue
            seen.add(variant)
            if variant in "\\]^-":
                pieces.append("\\" + variant)
            else:
                pieces.append(variant)
    return "[" + "".join(pieces) + "]"


def pattern_for(term: str) -> re.Pattern[str]:
    folded = fold(term)
    if not folded:
        return re.compile(r"(?!x)x")
    parts: list[str] = []
    for char in folded:
        if char.isspace():
            parts.append(r"\s+")
        elif char.isalpha():
            parts.append(_letter_class(char))
        else:
            parts.append(re.escape(char))
    return re.compile(rf"(?<!\w){''.join(parts)}(?!\w)")


def detect_dictionary(text: str, terms: list[Term]) -> list[Detection]:
    found: list[Detection] = []
    ordered = sorted(terms, key=lambda term: len(term.value), reverse=True)
    for term in ordered:
        for match in pattern_for(term.value).finditer(text):
            found.append(
                Detection(match.start(), match.end(), match.group(0), term.type, "L3")
            )
    return found


def allow_spans(text: str, allow: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in allow:
        if not term.strip():
            continue
        for match in pattern_for(term).finditer(text):
            spans.append((match.start(), match.end()))
    return spans


def is_allowed(
    detection: Detection,
    allow: list[str],
    spans: list[tuple[int, int]],
) -> bool:
    folded = fold(detection.text)
    if any(fold(term) == folded for term in allow):
        return True
    return any(start <= detection.start and detection.end <= end for start, end in spans)


def _overlaps(left: Detection, right: Detection) -> bool:
    return left.start < right.end and right.start < left.end


def _covers(outer: Detection, inner: Detection) -> bool:
    return outer.start <= inner.start and outer.end >= inner.end


def merge_detections(spans: list[Detection]) -> list[Detection]:
    ranked = sorted(
        spans,
        key=lambda item: (
            -(item.end - item.start),
            -LAYER_PRIORITY.get(item.layer, 0),
            item.start,
        ),
    )
    kept: list[Detection] = []
    for span in ranked:
        if span.end <= span.start:
            continue
        if any(_overlaps(span, current) for current in kept):
            continue
        kept.append(span)

    l1_spans = [span for span in spans if span.layer == "L1" and span.end > span.start]
    for identifier in sorted(l1_spans, key=lambda item: -(item.end - item.start)):
        if any(_covers(current, identifier) for current in kept):
            continue
        if any(current.layer == "L1" and _overlaps(current, identifier) for current in kept):
            continue
        punched: list[Detection] = []
        for current in kept:
            if not _overlaps(current, identifier):
                punched.append(current)
                continue
            if current.start < identifier.start:
                piece = current.shrink(current.start, identifier.start)
                if piece.text.strip():
                    punched.append(piece)
            if current.end > identifier.end:
                piece = current.shrink(identifier.end, current.end)
                if piece.text.strip():
                    punched.append(piece)
        punched.append(identifier)
        kept = punched

    kept.sort(key=lambda item: (item.start, -(item.end - item.start)))
    resolved: list[Detection] = []
    cursor = -1
    for span in kept:
        if span.start < cursor or span.end <= span.start:
            continue
        resolved.append(span)
        cursor = span.end
    return resolved
