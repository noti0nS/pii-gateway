"""Extração híbrida local, página a página, sem persistir o documento."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import dataclass, field
from io import BytesIO
from threading import Event, Lock
from collections.abc import Iterator

import pypdfium2 as pdfium
from pypdf import PdfReader

from pii_gateway.ocr import ExtractionCancelled, OcrError, check_cancelled, recognize_page
from pii_gateway.ocr_backends import OcrOptions, OcrWorker

_document_lock = Lock()  # PDFium não suporta chamadas simultâneas em threads distintas.

# Mensagens do parser podem conter fragmentos do documento não confiável.
for name in ("pypdf", "pypdfium2"):
    parser_logger = logging.getLogger(name)
    parser_logger.addHandler(logging.NullHandler())
    parser_logger.propagate = False


class PdfExtractionError(ValueError):
    """Erro público sem conteúdo ou metadados do documento."""


@dataclass(frozen=True)
class ExtractedPdf:
    text: str
    pages: int
    page_info: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ocr_backend: str = "tesseract"

    def payload(self) -> dict[str, object]:
        return {
            "text": self.text, "pages": self.pages, "page_info": self.page_info,
            "warnings": self.warnings, "conservative_pii": True,
            "ocr_backend": self.ocr_backend,
        }


def poor_text(text: str) -> bool:
    """Heurística, não garantia: camadas ausentes, corrompidas ou ilegíveis."""
    visible = [char for char in text if not char.isspace()]
    return (
        not visible or "\ufffd" in text
        or sum(char.isalnum() for char in visible) / len(visible) < 0.5
        or any(ord(char) < 32 and char not in "\n\r\t" for char in text)
    )


def has_large_image(page: object) -> bool:
    """Reconhece páginas mistas; um cabeçalho textual não dispensa OCR do scan."""
    width, height = page.get_size()
    area = width * height
    image_area = 0.0
    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = obj.get_bounds()
        image_area += max(0, min(width, right) - max(0, left)) * max(0, min(height, top) - max(0, bottom))
    return area > 0 and image_area / area >= 0.15


def pdf_events(content: bytes, *, force_ocr: bool = False, cancel: Event | None = None, ocr_options: OcrOptions | None = None) -> Iterator[dict[str, object]]:
    cancel = cancel if cancel is not None else Event()
    while not _document_lock.acquire(timeout=0.25):
        check_cancelled(cancel)
    try:
        yield from _pdf_events(content, force_ocr=force_ocr, cancel=cancel, ocr_options=ocr_options or OcrOptions())
    finally:
        _document_lock.release()


def _pdf_events(content: bytes, *, force_ocr: bool, cancel: Event, ocr_options: OcrOptions) -> Iterator[dict[str, object]]:
    """Só emite o texto quando TODAS as páginas terminarem com sucesso."""
    cancel = cancel if cancel is not None else Event()
    if not content.startswith(b"%PDF-"):
        raise PdfExtractionError("O arquivo não é um PDF válido.")
    try:
        with ExitStack() as resources:
            check_cancelled(cancel)
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise PdfExtractionError("PDF protegido por senha não é aceito.")
            pages = len(reader.pages)
            if not pages:
                raise PdfExtractionError("O PDF não contém páginas para extrair.")
            rendered_document = None
            worker = None
            parts: list[str] = []
            page_info: list[dict[str, object]] = []
            warnings: list[str] = []
            yield {"type": "progress", "completed": 0, "pages": pages, "method": "prepare"}
            for index, page in enumerate(reader.pages):
                check_cancelled(cancel)
                text = ""
                if not force_ocr:
                    try:
                        text = (page.extract_text() or "").strip()
                    except Exception:
                        pass  # Tenta OCR de uma camada textual defeituosa.
                needs_ocr = force_ocr or poor_text(text)
                rendered_page = None
                try:
                    if needs_ocr or len(page.images):
                        if rendered_document is None:
                            rendered_document = resources.enter_context(pdfium.PdfDocument(content))
                        rendered_page = rendered_document[index]
                        needs_ocr |= has_large_image(rendered_page)
                    method = "ocr" if needs_ocr else "text"
                    yield {"type": "progress", "completed": index, "pages": pages, "page": index + 1, "method": method}
                    low_confidence = False
                    if needs_ocr:
                        if ocr_options.backend == "tesseract":
                            result = recognize_page(rendered_page, cancel)
                        else:
                            if worker is None:
                                worker = resources.enter_context(OcrWorker(ocr_options))
                            result = worker.recognize(rendered_page, cancel)
                        text = result.text.strip()
                        low_confidence = result.low_confidence
                    if not text:
                        method = "empty"
                        low_confidence = True
                        warnings.append(
                            f"Página {index + 1}: sem texto reconhecido após OCR; "
                            "pode estar em branco ou ilegível. Confira essa página; a importação continuou."
                        )
                    check_cancelled(cancel)
                    if text:
                        parts.append(text)
                    page_info.append({"page": index + 1, "method": method, "review_required": low_confidence})
                    if low_confidence and text:
                        warnings.append(f"Página {index + 1}: o motor sinalizou necessidade de revisão; confira antes de consultar.")
                    yield {"type": "progress", "completed": index + 1, "pages": pages, "method": method}
                finally:
                    if rendered_page is not None:
                        rendered_page.close()
            check_cancelled(cancel)
            result = ExtractedPdf("\n\n".join(parts), pages, page_info, warnings, ocr_options.backend)
            yield {"type": "complete", **result.payload()}
    except (PdfExtractionError, ExtractionCancelled):
        raise
    except OcrError as exc:
        raise PdfExtractionError(str(exc)) from None
    except Exception:
        raise PdfExtractionError("Não foi possível extrair o texto deste PDF.") from None


def extract_pdf(content: bytes, *, force_ocr: bool = False, ocr_options: OcrOptions | None = None) -> ExtractedPdf:
    for event in pdf_events(content, force_ocr=force_ocr, ocr_options=ocr_options):
        if event["type"] == "complete":
            return ExtractedPdf(event["text"], event["pages"], event["page_info"], event["warnings"], event["ocr_backend"])
    raise PdfExtractionError("A extração não foi concluída.")
