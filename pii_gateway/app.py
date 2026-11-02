"""API mínima da demonstração e a página estática."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from pii_gateway.config import Settings
from pii_gateway.detect import ENTITY_TYPES, Term, infer_type
from pii_gateway.llm import LLM, LLMCallError, LLMNotConfigured
from pii_gateway.sample import DEFAULT_ALLOW, SAMPLE_QUESTION, SAMPLE_TERMS, SAMPLE_TEXT
from pii_gateway.service import Gateway, Ner
from pii_gateway.surrogate import SurrogateCollisionError

_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


class CaseTermIn(BaseModel):
    value: str
    type: str = "PERSON"

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in ENTITY_TYPES:
            raise ValueError("tipo de entidade desconhecido")
        return normalized


class TextIn(BaseModel):
    text: str
    case_terms: list[CaseTermIn | str] = Field(default_factory=list)
    allow_terms: list[str] | None = None

    @field_validator("case_terms", mode="before")
    @classmethod
    def _coerce_terms(cls, value: object) -> object:
        if not value:
            return []
        if not isinstance(value, list):
            return value
        coerced: list[object] = []
        for item in value:
            if isinstance(item, str):
                coerced.append({"value": item, "type": infer_type(item)})
            else:
                coerced.append(item)
        return coerced


class AskIn(TextIn):
    question: str


def _terms(body: TextIn) -> list[Term]:
    terms: list[Term] = []
    for item in body.case_terms:
        if isinstance(item, str):
            terms.append(Term(item, infer_type(item)))
        else:
            terms.append(Term(item.value, item.type))
    return terms


def _allow(body: TextIn) -> list[str]:
    if body.allow_terms is None:
        return list(DEFAULT_ALLOW)
    return list(body.allow_terms)


def create_app(
    settings: Settings | None = None,
    llm: LLM | None = None,
    ner: Ner | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else Settings.from_env()
    gateway = Gateway(resolved, llm=llm, ner=ner)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        gateway.init()
        yield

    app = FastAPI(title="pii-gateway", lifespan=lifespan)
    app.state.gateway = gateway

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_INDEX)

    @app.get("/sample")
    def sample() -> dict[str, object]:
        return {
            "text": SAMPLE_TEXT,
            "question": SAMPLE_QUESTION,
            "case_terms": SAMPLE_TERMS,
            "allow_terms": DEFAULT_ALLOW,
        }

    @app.post("/sanitize")
    def sanitize(body: TextIn) -> dict[str, object]:
        try:
            result = gateway.sanitize(body.text, _terms(body), _allow(body))
        except SurrogateCollisionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "colisao_de_surrogate", "surrogate": exc.surrogate},
            ) from None
        return {
            "sanitized": result.text,
            "detections": result.detections,
            "ner": {
                "enabled": gateway.settings.enable_ner,
                "warning": gateway.ner_warning,
            },
        }

    @app.post("/ask")
    def ask(body: AskIn) -> dict[str, object]:
        try:
            result = gateway.ask(body.text, body.question, _terms(body), _allow(body))
        except SurrogateCollisionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "colisao_de_surrogate", "surrogate": exc.surrogate},
            ) from None
        except LLMNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except LLMCallError:
            raise HTTPException(status_code=502, detail="Falha ao consultar o LLM.") from None
        return {
            "original": result.original,
            "question": result.question,
            "sent_to_llm": result.sent_to_llm,
            "llm_answer": result.llm_answer,
            "rehydrated": result.rehydrated,
            "detections": result.detections,
            "ner": {
                "enabled": gateway.settings.enable_ner,
                "warning": result.ner_warning,
            },
            "llm_mode": result.llm_mode,
        }

    @app.get("/stores")
    def stores() -> dict[str, object]:
        return gateway.stores.dump()

    @app.post("/reset")
    def reset() -> dict[str, bool]:
        gateway.reset()
        return {"ok": True}

    return app


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
