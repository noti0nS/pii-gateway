import base64

from pii_gateway.app import create_app
from pii_gateway.config import Settings
from pii_gateway.detect import Detection
from pii_gateway.llm import MockLLM
from pii_gateway.sample import DEFAULT_ALLOW, SAMPLE_TERMS, SAMPLE_TEXT
from fastapi.testclient import TestClient

SECRETS = [
    "Helena Duarte Ferreira",
    "HELENA DUARTE FERREIRA",
    "H. Ferreira",
    "Marcos Teixeira Pacheco",
    "Iara Nogueira Campos",
    "IARA NOGUEIRA CAMPOS",
    "Oficina Luminar Ltda",
    "123.456.789-09",
    "12345678909",
    "12.345.678/0001-95",
    "12345678000195",
    "helena.duarte@example.com",
    "(11) 98877-6655",
    "11988776655",
    "Rua das Acácias, 120, Vila Mariana, São Paulo/SP",
]


def test_prompt_and_analysis_db_contain_no_plaintext(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("o modelo não pode ser baixado nos testes")

    monkeypatch.setattr("pii_gateway.ner.load_model", explode)

    def ner(text: str) -> list[Detection]:
        needle = "Iara Nogueira Campos"
        start = text.find(needle)
        if start < 0:
            return []
        return [Detection(start, start + len(needle), needle, "PERSON", "L2")]

    settings = Settings(
        tenant_key="test-tenant-key",
        vault_key="test-vault-key",
        analysis_db_url=f"sqlite:///{tmp_path / 'analysis.db'}",
        vault_db_url=f"sqlite:///{tmp_path / 'vault.db'}",
        enable_ner=True,
        llm_mock=True,
    )
    llm = MockLLM()
    app = create_app(settings, llm=llm, ner=ner)
    text = (
        SAMPLE_TEXT
        + " A testemunha Iara Nogueira Campos repetiu: IARA NOGUEIRA CAMPOS."
    )
    question = (
        "Confirme se o e-mail helena.duarte@example.com e o CPF 123.456.789-09 "
        "pertencem à autora, e se Iara Nogueira Campos foi ouvida."
    )
    with TestClient(app) as client:
        response = client.post(
            "/ask",
            json={
                "text": text,
                "question": question,
                "case_terms": SAMPLE_TERMS,
                "allow_terms": DEFAULT_ALLOW,
            },
        )
        assert response.status_code == 200
        data = response.json()
        stores = client.get("/stores").json()

    assert llm.messages
    outbound = "\n".join(
        item.get("content", "") for batch in llm.messages for item in batch
    )
    analysis_bytes = (tmp_path / "analysis.db").read_bytes()
    for secret in SECRETS:
        assert secret not in outbound
        assert secret.encode("utf-8") not in analysis_bytes
        assert secret not in data["sent_to_llm"]
        assert secret not in data["llm_answer"]

    assert "Tribunal de Justiça do Estado de São Paulo" in outbound
    assert "Ministério Público" in outbound
    assert "PERSON_" in outbound and "CPF_" in outbound and "PHONE_" in outbound
    assert "Iara Nogueira Campos" in data["rehydrated"]
    assert "Helena Duarte Ferreira" in data["rehydrated"]
    assert "123.456.789-09" in data["rehydrated"]

    entities = stores["analysis"]["entity"]
    assert entities
    assert sum(row["type"] == "PHONE" for row in entities) == 1
    assert sum(row["type"] == "CPF" for row in entities) == 1
    for row in entities:
        assert set(row) == {"surrogate", "type", "value_hash", "created_at"}
        assert len(row["value_hash"]) == 64
        for secret in SECRETS:
            assert secret not in row["surrogate"]
            assert secret not in row["value_hash"]

    assert stores["vault"]["vault_access_log"]
    for row in stores["vault"]["vault"]:
        raw = base64.b64decode(row["ciphertext_b64"])
        for secret in SECRETS:
            assert secret.encode("utf-8") not in raw
            assert secret not in row["ciphertext_b64"]
