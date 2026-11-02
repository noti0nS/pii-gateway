from pii_gateway.detect import (
    canonical_phone,
    cnpj_valid,
    cpf_valid,
    detect_l1,
    phone_valid,
)
from pii_gateway.sample import SAMPLE_TEXT


def test_cpf_valid_and_invalid():
    assert cpf_valid("123.456.789-09")
    assert cpf_valid("12345678909")
    assert not cpf_valid("123.456.789-00")
    assert not cpf_valid("111.111.111-11")
    assert not cpf_valid("123.456.789-091")


def test_cnpj_valid_and_invalid():
    assert cnpj_valid("12.345.678/0001-95")
    assert cnpj_valid("12345678000195")
    assert not cnpj_valid("12.345.678/0001-00")
    assert not cnpj_valid("11.111.111/1111-11")
    assert not cnpj_valid("12.345.678/0001-9")


def test_phone_br_formats():
    assert phone_valid("(11) 98877-6655")
    assert phone_valid("11988776655")
    assert phone_valid("+55 11 98877-6655")
    assert phone_valid("(11) 3877-6655")
    assert canonical_phone("+55 (11) 98877-6655") == "11988776655"
    assert not phone_valid("123")
    assert not phone_valid("(00) 98877-6655")
    assert not phone_valid("11111111111")


def test_sample_placeholders_and_no_phone_inside_cnpj():
    assert "123.456.789-09" in SAMPLE_TEXT
    assert "12.345.678/0001-95" in SAMPLE_TEXT
    assert cpf_valid("123.456.789-09")
    assert cnpj_valid("12.345.678/0001-95")
    hits = detect_l1("CNPJ 12.345.678/0001-95 e telefone (11) 98877-6655.")
    assert [hit.type for hit in hits] == ["CNPJ", "PHONE"]
    assert hits[0].text == "12.345.678/0001-95"
    assert hits[1].text == "(11) 98877-6655"
