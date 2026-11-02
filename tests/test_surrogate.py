import re

import pytest

from pii_gateway.normalize import normalize_for_hash
from pii_gateway.surrogate import (
    SurrogateCollisionError,
    choose_surrogate,
    hmac_hex,
)


def _token(entity_type: str, value: str, key: str = "tenant-secret-2026") -> str:
    digest = hmac_hex(normalize_for_hash(entity_type, value), key)
    token, extended = choose_surrogate(entity_type, digest, {})
    assert extended is False
    return token


def test_same_value_is_deterministic_and_formatting_collapses():
    formatted = _token("CPF", "123.456.789-09")
    digits = _token("CPF", "12345678909")
    assert formatted == digits
    assert re.fullmatch(r"CPF_[0-9a-f]{8}", formatted)

    phone = _token("PHONE", "(11) 98877-6655")
    assert phone == _token("PHONE", "11988776655")
    assert phone == _token("PHONE", "+55 11 98877-6655")

    assert _token("PERSON", "José da Silva") == _token("PERSON", "jose  da   silva")
    assert _token("EMAIL", "Helena.Duarte@Example.com") == _token(
        "EMAIL", "helena.duarte@example.com"
    )


def test_tenant_key_changes_the_surrogate_and_type_does_not_enter_the_hmac():
    left = _token("PERSON", "Acme", "chave-a")
    right = _token("PERSON", "Acme", "chave-b")
    assert left != right
    as_person = _token("PERSON", "Acme")
    as_org = _token("ORG", "Acme")
    assert as_person != as_org
    assert as_person.split("_", 1)[1] == as_org.split("_", 1)[1]


def test_collision_extends_and_full_collision_is_flagged():
    digest_old = "deadbeef" + "11" * 28
    digest_new = "deadbeef" + "22" * 28
    index = {"PERSON_deadbeef": digest_old}
    token, extended = choose_surrogate("PERSON", digest_new, index)
    assert extended is True
    assert token == "PERSON_" + digest_new[:12]

    blocked = {f"PERSON_{digest_new[:length]}": "ff" * 32 for length in (8, 12, 16, 24, 32, 64)}
    with pytest.raises(SurrogateCollisionError):
        choose_surrogate("PERSON", digest_new, blocked)

    same, extended_again = choose_surrogate("PERSON", digest_new, {"PERSON_" + digest_new[:8]: digest_new})
    assert same == "PERSON_" + digest_new[:8]
    assert extended_again is False
