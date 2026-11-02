import os

os.environ.setdefault("TENANT_KEY", "test-tenant-key")
os.environ.setdefault("VAULT_KEY", "test-vault-key")
os.environ.setdefault("ENABLE_NER", "false")
os.environ.setdefault("LLM_MOCK", "true")

import pytest
from fastapi.testclient import TestClient

from pii_gateway.app import create_app
from pii_gateway.config import Settings
from pii_gateway.llm import MockLLM


@pytest.fixture
def stack(tmp_path):
    settings = Settings(
        tenant_key="test-tenant-key",
        vault_key="test-vault-key",
        analysis_db_url=f"sqlite:///{tmp_path / 'analysis.db'}",
        vault_db_url=f"sqlite:///{tmp_path / 'vault.db'}",
        enable_ner=False,
        llm_mock=True,
        llm_api_key="",
        llm_base_url="http://llm.invalid/v1",
        llm_model="mock",
    )
    llm = MockLLM()
    app = create_app(settings, llm=llm)
    with TestClient(app) as client:
        yield client, llm, tmp_path
