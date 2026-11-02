"""Cliente de chat compatível com a API da OpenAI. O mock não sai da máquina."""

from __future__ import annotations

import logging
from typing import Protocol

from pii_gateway.config import Settings
from pii_gateway.surrogate import SURROGATE_RE

logger = logging.getLogger("pii_gateway")

SYSTEM_PROMPT = (
    "Você é um assistente. O texto do usuário já foi pseudonimizado. "
    "Um substituto é um prefixo PERSON, ORG, CPF, CNPJ, EMAIL, PHONE ou ADDRESS, "
    "seguido de um sublinhado e de dígitos hexadecimais. "
    "Copie cada substituto exatamente como recebido, sem alterar, traduzir, "
    "abreviar ou explicar o formato. Responda em português."
)


class LLM(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class LLMNotConfigured(RuntimeError):
    pass


class LLMCallError(RuntimeError):
    pass


class MockLLM:
    """Resposta local para a demonstração e para os testes. Não chama rede."""

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.messages.append(messages)
        blob = "\n".join(item.get("content", "") for item in messages)
        found: list[str] = []
        for match in SURROGATE_RE.finditer(blob):
            token = match.group(0)
            if token not in found:
                found.append(token)
        if not found:
            return "Nenhuma entidade pseudonimizada foi encontrada no texto."
        return (
            "Síntese de demonstração: as referências preservadas são "
            + ", ".join(found)
            + "."
        )


class OpenAICompatibleLLM:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def complete(self, messages: list[dict[str, str]]) -> str:
        if not self._settings.llm_api_key.strip():
            raise LLMNotConfigured(
                "Defina LLM_API_KEY ou use LLM_MOCK=true para a demonstração sem provedor."
            )
        from openai import OpenAI

        client = OpenAI(
            api_key=self._settings.llm_api_key,
            base_url=self._settings.llm_base_url,
        )
        try:
            response = client.chat.completions.create(
                model=self._settings.llm_model,
                temperature=0,
                messages=messages,
            )
        except Exception as exc:
            logger.error("falha ao consultar o LLM (%s)", type(exc).__name__)
            raise LLMCallError from None
        content = response.choices[0].message.content
        return content or ""


def build_llm(settings: Settings) -> LLM:
    if settings.llm_mock:
        return MockLLM()
    return OpenAICompatibleLLM(settings)
