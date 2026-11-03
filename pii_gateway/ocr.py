"""OCR local via stdin/stdout; imagens e texto não passam por arquivos temporários."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from threading import Event


class OcrError(ValueError):
    """Falha pública sem saída bruta do processo ou conteúdo do documento."""


class ExtractionCancelled(Exception):
    """Cancelamento solicitado pelo cliente."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    low_confidence: bool


def check_cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise ExtractionCancelled()


def tesseract_command() -> str:
    configured = os.environ.get("TESSERACT_CMD")
    if configured:
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    for root in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if root:
            candidate = Path(root) / "Tesseract-OCR" / "tesseract.exe"
            if candidate.is_file():
                return str(candidate)
    raise OcrError("OCR indisponível: instale o Tesseract e o idioma português (por).")


def render_png(page: object, cancel: Event) -> bytes:
    """Renderiza somente a página atual, sem escrever a imagem em disco."""
    check_cancelled(cancel)
    bitmap = page.render(scale=300 / 72)
    try:
        image = bitmap.to_pil()
        try:
            output = BytesIO()
            image.save(output, format="PNG")
            png = output.getvalue()
        finally:
            image.close()
    finally:
        bitmap.close()
    check_cancelled(cancel)
    return png


def tesseract_data_options(language: str) -> list[str]:
    data_options: list[str] = []
    if not os.environ.get("TESSDATA_PREFIX") and os.environ.get("LOCALAPPDATA"):
        data = Path(os.environ["LOCALAPPDATA"]) / "pii-gateway" / "tessdata"
        if all((data / f"{part}.traineddata").is_file() for part in language.split("+")):
            data_options = ["--tessdata-dir", str(data)]
    return data_options


def recognize_page(page: object, cancel: Event) -> OcrResult:
    """Renderiza uma página a 300 DPI e encerra o processo ao cancelar."""
    check_cancelled(cancel)
    command = tesseract_command()
    png = render_png(page, cancel)
    language = os.environ.get("TESSERACT_LANG", "por")
    data_options = tesseract_data_options(language)
    try:
        process = subprocess.Popen(
            [command, "stdin", "stdout", *data_options, "-l", language,
             "--dpi", "300", "-c", "tessedit_create_tsv=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError:
        raise OcrError("Não foi possível iniciar o Tesseract local. Confira TESSERACT_CMD.") from None
    try:
        first = True
        while True:
            check_cancelled(cancel)
            try:
                stdout, _stderr = process.communicate(input=png if first else None, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode:
            raise OcrError("O OCR local falhou. Confira o Tesseract e o idioma configurado (por).")
        return parse_tsv(stdout.decode("utf-8", errors="replace"))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def parse_tsv(tsv: str) -> OcrResult:
    lines: dict[tuple[str, ...], list[str]] = {}
    low_confidence = False
    for row in csv.DictReader(StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5":
            continue
        word = (row.get("text") or "").strip()
        if not word:
            continue
        key = tuple(row.get(field, "") for field in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(word)
        try:
            low_confidence |= float(row.get("conf", "0")) < 60
        except ValueError:
            low_confidence = True
    return OcrResult("\n".join(" ".join(words) for words in lines.values()), low_confidence)
