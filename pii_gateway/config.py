"""Configuração por ambiente. Nenhum segredo tem valor padrão no código."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Carrega um .env local sem sobrescrever variáveis já definidas."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


@dataclass(frozen=True)
class Settings:
    tenant_key: str
    vault_key: str
    analysis_db_url: str = "sqlite:///./data/analysis.db"
    vault_db_url: str = "sqlite:///./data/vault.db"
    enable_ner: bool = True
    gliner_model: str = "urchade/gliner_multi_pii-v1"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_mock: bool = False
    ocr_backend: str = "tesseract"
    ocr_device: str = "cpu"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        tenant = os.environ.get("TENANT_KEY", "").strip()
        vault = os.environ.get("VAULT_KEY", "").strip()
        if not tenant or not vault:
            raise RuntimeError(
                "Defina TENANT_KEY e VAULT_KEY. Copie .env.example para .env."
            )
        return cls(
            tenant_key=tenant,
            vault_key=vault,
            analysis_db_url=os.environ.get(
                "ANALYSIS_DB_URL", "sqlite:///./data/analysis.db"
            ),
            vault_db_url=os.environ.get("VAULT_DB_URL", "sqlite:///./data/vault.db"),
            enable_ner=_flag("ENABLE_NER", True),
            gliner_model=os.environ.get(
                "GLINER_MODEL", "urchade/gliner_multi_pii-v1"
            ),
            llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            llm_mock=_flag("LLM_MOCK", False),
            ocr_backend=os.environ.get("OCR_BACKEND", "tesseract"),
            ocr_device=os.environ.get("OCR_DEVICE", "cpu"),
        )
