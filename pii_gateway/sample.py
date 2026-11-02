"""Caso fictício para a demonstração.

Pessoas, empresa e endereço são inventados. CPF e CNPJ têm dígito
verificador válido e são os exemplos clássicos de placeholder
(123.456.789-09 e 12.345.678/0001-95). O e-mail usa example.com,
domínio reservado. Não commitar documento real de processo.
"""

from __future__ import annotations

SAMPLE_TEXT = (
    "Autos de demonstração, sem valor jurídico, em trâmite no "
    "Tribunal de Justiça do Estado de São Paulo. A autora Helena Duarte Ferreira, "
    "CPF 123.456.789-09, residente na Rua das Acácias, 120, Vila Mariana, "
    "São Paulo/SP, e-mail helena.duarte@example.com, telefone "
    "(11) 98877-6655, ajuizou cobrança em face de Oficina Luminar Ltda, "
    "CNPJ 12.345.678/0001-95. A inicial indica o contato alternativo 11988776655 "
    "e cita o sócio Marcos Teixeira Pacheco. Em depoimento, HELENA DUARTE FERREIRA "
    "confirmou o que H. Ferreira narrara. A ré contestou. O Ministério Público "
    "manifestou-se apenas sobre a regularidade formal."
)

SAMPLE_QUESTION = (
    "Quem são as partes, qual o documento de cada uma e qual o telefone da autora?"
)

SAMPLE_TERMS: list[dict[str, str]] = [
    {"value": "Helena Duarte Ferreira", "type": "PERSON"},
    {"value": "Marcos Teixeira Pacheco", "type": "PERSON"},
    {"value": "Oficina Luminar Ltda", "type": "ORG"},
    {
        "value": "Rua das Acácias, 120, Vila Mariana, São Paulo/SP",
        "type": "ADDRESS",
    },
]

DEFAULT_ALLOW: list[str] = [
    "Tribunal de Justiça do Estado de São Paulo",
    "Ministério Público",
]
