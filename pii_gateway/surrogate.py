"""Surrogate = PREFIXO_ + hex(HMAC-SHA256(Norm(v), K_tenant))[:8].

Colisão (o mesmo surrogate já aponta para outro valor) estende o hex
para 12, 16, 24, 32 e por fim os 64 caracteres. Se o HMAC inteiro ainda
colidir, a operação é recusada.
"""

from __future__ import annotations

import hashlib
import hmac
import re

PREFIXES: tuple[str, ...] = (
    "ADDRESS",
    "PERSON",
    "PHONE",
    "EMAIL",
    "CNPJ",
    "ORG",
    "CPF",
)
COMPLETE_HEX_LENGTHS: frozenset[int] = frozenset({8, 12, 16, 24, 32, 64})
_HEX_STEPS: tuple[int, ...] = (8, 12, 16, 24, 32, 64)

SURROGATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(PREFIXES)
    + r")_[0-9a-f]{8,64}(?![0-9a-f])"
)


class SurrogateCollisionError(Exception):
    def __init__(self, surrogate: str) -> None:
        super().__init__(f"colisão de surrogate em {surrogate}")
        self.surrogate = surrogate


def hmac_hex(normalized: str, tenant_key: str) -> str:
    return hmac.new(
        tenant_key.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def choose_surrogate(
    entity_type: str,
    digest: str,
    index: dict[str, str],
) -> tuple[str, bool]:
    """Escolhe o surrogate. `index` mapeia surrogate -> HMAC completo.

    Retorna `(surrogate, estendido)`.
    """
    extended = False
    for length in _HEX_STEPS:
        token = f"{entity_type}_{digest[:length]}"
        previous = index.get(token)
        if previous is None or previous == digest:
            return token, extended
        extended = True
    raise SurrogateCollisionError(f"{entity_type}_{digest[:8]}")
