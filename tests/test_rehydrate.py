from fastapi.testclient import TestClient

from pii_gateway.app import create_app
from pii_gateway.config import Settings
from pii_gateway.llm import MockLLM


def test_round_trip_restores_the_first_surface(stack):
    client, _llm, _tmp = stack
    response = client.post(
        "/ask",
        json={
            "text": "Helena Duarte Ferreira, CPF 123.456.789-09, procurou o escritório.",
            "question": "O CPF 123.456.789-09 é o da autora Helena Duarte Ferreira?",
            "case_terms": ["Helena Duarte Ferreira"],
            "allow_terms": [],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "Helena Duarte Ferreira" not in data["sent_to_llm"]
    assert "123.456.789-09" not in data["sent_to_llm"]
    assert "12345678909" not in data["sent_to_llm"]
    assert "PERSON_" in data["llm_answer"]
    assert "CPF_" in data["llm_answer"]
    assert "Helena Duarte Ferreira" not in data["llm_answer"]
    assert "Helena Duarte Ferreira" in data["rehydrated"]
    assert "123.456.789-09" in data["rehydrated"]
    assert "PERSON_" not in data["rehydrated"]
    assert "CPF_" not in data["rehydrated"]

    stores = client.get("/stores").json()
    assert stores["vault"]["vault_access_log"]
    assert {row["purpose"] for row in stores["vault"]["vault_access_log"]} == {"rehydrate"}
    assert len([row for row in stores["analysis"]["entity"] if row["type"] == "CPF"]) == 1


def test_index_and_reset(stack):
    client, _llm, _tmp = stack
    page = client.get("/")
    assert page.status_code == 200
    assert "Sanitizar e perguntar" in page.text
    sample = client.get("/sample").json()
    assert "Helena Duarte Ferreira" in sample["text"]
    client.post(
        "/sanitize",
        json={"text": "CPF 123.456.789-09", "case_terms": [], "allow_terms": []},
    )
    assert client.get("/stores").json()["analysis"]["entity"]
    assert client.post("/reset").json() == {"ok": True}
    assert client.get("/stores").json()["analysis"]["entity"] == []


def test_sanitize_payload_does_not_echo_values(stack):
    client, _llm, _tmp = stack
    response = client.post(
        "/sanitize",
        json={
            "text": "CPF 123.456.789-09 e Helena Duarte Ferreira",
            "case_terms": ["Helena Duarte Ferreira"],
            "allow_terms": [],
        },
    )
    assert response.status_code == 200
    assert "123.456.789-09" not in response.text
    assert "12345678909" not in response.text
    assert "Helena" not in response.text
    body = response.json()
    assert "CPF_" in body["sanitized"]
    assert "PERSON_" in body["sanitized"]
    assert "text" not in body["detections"][0]
    assert set(body["detections"][0]) == {"type", "layer", "start", "end", "surrogate"}


def test_missing_model_does_not_crash_or_download(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("download bloqueado")

    monkeypatch.setattr("pii_gateway.service.detect_l2", explode)
    settings = Settings(
        tenant_key="test-tenant-key",
        vault_key="test-vault-key",
        analysis_db_url=f"sqlite:///{tmp_path / 'analysis.db'}",
        vault_db_url=f"sqlite:///{tmp_path / 'vault.db'}",
        enable_ner=True,
        llm_mock=True,
    )
    app = create_app(settings, llm=MockLLM())
    with TestClient(app) as client:
        response = client.post(
            "/sanitize",
            json={"text": "telefone (11) 98877-6655", "allow_terms": []},
        )
    assert response.status_code == 200
    body = response.json()
    assert "PHONE_" in body["sanitized"]
    assert "98877" not in body["sanitized"]
    assert body["ner"]["warning"]
