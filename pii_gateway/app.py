"""API mínima da demonstração e a página estática."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from pii_gateway.config import Settings
from pii_gateway.detect import ENTITY_TYPES, Term, infer_type
from pii_gateway.llm import LLM, LLMCallError, LLMNotConfigured
from pii_gateway.ocr import ExtractionCancelled
from pii_gateway.ocr_backends import Backend, Device, CATALOG, OcrOptions, backend_status
from pii_gateway.pdf import PdfExtractionError, extract_pdf, pdf_events
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
    conservative_pii: bool = False
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

    def selected_ocr(backend, device):
        selected_backend = backend or resolved.ocr_backend
        selected_device = device or resolved.ocr_device
        if selected_backend not in CATALOG or selected_device not in {"cpu", "cuda"}:
            raise HTTPException(422, "Configuração de OCR desconhecida.")
        if selected_device == "cuda" and selected_backend not in {"smoldocling", "paddleocr-vl"}:
            raise HTTPException(422, "Este adaptador OCR usa CPU; selecione CPU.")
        return OcrOptions(selected_backend, selected_device)

    @app.get("/ocr/options")
    def ocr_options() -> JSONResponse:
        defaults = selected_ocr(None, None)
        return JSONResponse({
            "default_backend": defaults.backend, "default_device": defaults.device,
            "backends": [backend_status(name) for name in CATALOG],
            "llm_mode": "mock" if resolved.llm_mock else "live",
            "ner_enabled": resolved.enable_ner,
        }, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_INDEX)

    @app.get("/assets/ui.css")
    def ui_styles() -> FileResponse:
        return FileResponse(_INDEX.parent / "ui.css", media_type="text/css")

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
            result = gateway.sanitize(body.text, _terms(body), _allow(body), conservative_pii=body.conservative_pii)
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

    @app.post("/extract-pdf")
    async def extract_document(request: Request, force_ocr: bool = False, backend: Backend | None = None, device: Device | None = None) -> JSONResponse:
        # Corpo binário em vez de multipart: não cria arquivo temporário em disco.
        headers = {"Cache-Control": "no-store"}
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/pdf":
            raise HTTPException(415, "Envie o arquivo com Content-Type application/pdf.", headers=headers)
        content = await request.body()
        options = selected_ocr(backend, device)
        try:
            result = await run_in_threadpool(extract_pdf, content, force_ocr=force_ocr, ocr_options=options)
        except PdfExtractionError as exc:
            raise HTTPException(422, str(exc), headers=headers) from None
        return JSONResponse(
            result.payload(), headers=headers
        )

    @app.post("/extract-pdf/stream")
    async def extract_document_stream(request: Request, force_ocr: bool = False, backend: Backend | None = None, device: Device | None = None) -> StreamingResponse:
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/pdf":
            raise HTTPException(415, "Envie o arquivo com Content-Type application/pdf.", headers=headers)
        content = await request.body()
        options = selected_ocr(backend, device)

        async def events():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            cancel = Event()

            def send(event):
                if not cancel.is_set() and not loop.is_closed():
                    loop.call_soon_threadsafe(queue.put_nowait, event)

            def produce():
                try:
                    for event in pdf_events(content, force_ocr=force_ocr, cancel=cancel, ocr_options=options):
                        send(event)
                except ExtractionCancelled:
                    pass
                except PdfExtractionError as exc:
                    send({"type": "error", "detail": str(exc)})
                except Exception:
                    send({"type": "error", "detail": "Falha na extração local do PDF."})

            worker = asyncio.create_task(asyncio.to_thread(produce))
            try:
                while True:
                    event = await queue.get()
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] in {"complete", "error"}:
                        break
            finally:
                # Desconectar/abortar sinaliza também o subprocesso de OCR ativo.
                cancel.set()
                worker.cancel()

        return StreamingResponse(events(), media_type="application/x-ndjson", headers=headers)

    @app.post("/ask")
    def ask(body: AskIn) -> dict[str, object]:
        try:
            result = gateway.ask(body.text, body.question, _terms(body), _allow(body), conservative_pii=body.conservative_pii)
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
