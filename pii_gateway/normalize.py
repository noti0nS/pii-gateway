"""Normalização canônica antes do HMAC e da busca do dicionário."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D")


def fold(value: str) -> str:
    """NFKC, sem acento, casefold, espaços colapsados.

    Usada na busca do dicionário (L3) e no HMAC de nomes, e-mails e endereços.
    """
    text = unicodedata.normalize("NFKC", value)
    text = "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )
    text = text.casefold()
    return _WS.sub(" ", text).strip()


def normalize_for_hash(entity_type: str, value: str) -> str:
    """Forma canônica que entra no HMAC.

    CPF, CNPJ e telefone perdem a pontuação, então `123.456.789-09` e
    `12345678909` geram o mesmo surrogate. O tipo não entra no hash.
    """
    if entity_type in {"CPF", "CNPJ"}:
        return _NON_DIGIT.sub("", value)
    if entity_type == "PHONE":
        digits = _NON_DIGIT.sub("", value)
        if digits.startswith("55") and len(digits) in {12, 13}:
            digits = digits[2:]
        return digits
    return fold(value)
