from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pii_gateway import pdf
from pii_gateway.ocr import OcrError, OcrResult


@pytest.fixture(autouse=True)
def no_real_ocr(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OcrError("OCR indisponível no teste.")
    monkeypatch.setattr(pdf, "recognize_page", unavailable)


def make_pdf(*texts: str, encrypted: bool = False) -> bytes:
    """Gera documentos fictícios em memória, sem depender de arquivos locais."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        if text:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("fictional-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def upload(client, content, content_type="application/pdf"):
    return client.post("/extract-pdf", content=content, headers={"Content-Type": content_type})


def test_extract_pdf_pages_are_ordered_and_not_persisted(stack):
    client, llm, directory = stack
    before = client.get("/stores").json()
    response = upload(client, make_pdf("Caso ficticio de Maria Silva.", "Contato: maria@example.com"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {key: response.json()[key] for key in ("text", "pages")} == {
        "text": "Caso ficticio de Maria Silva.\n\nContato: maria@example.com", "pages": 2,
    }
    assert not llm.messages
    assert client.get("/stores").json() == before
    assert not list(directory.glob("*.pdf"))
    assert response.json()["conservative_pii"] is True


def test_extracted_pdf_uses_existing_safe_analysis_pipeline(stack):
    client, llm, directory = stack
    text = "Maria Silva CPF 123.456.789-09 email maria@example.com"
    extracted = upload(client, make_pdf(text)).json()["text"]
    response = client.post("/ask", json={
        "text": extracted,
        "question": "Resuma o caso de Maria Silva.",
        "case_terms": [{"value": "Maria Silva", "type": "PERSON"}],
        "allow_terms": [],
    })
    assert response.status_code == 200
    outbound = str(llm.messages)
    analysis = (directory / "analysis.db").read_bytes()
    for secret in ["Maria Silva", "123.456.789-09", "maria@example.com"]:
        assert secret not in outbound
        assert secret.encode() not in analysis
        assert secret not in response.json()["sent_to_llm"]
    assert "Maria Silva" in response.json()["rehydrated"]


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"%PDF-1.7\ncorrupt document"])
def test_invalid_pdf_is_rejected_without_echoing_content(stack, content):
    client, llm, _ = stack
    response = upload(client, content)
    assert response.status_code == 422
    assert "corrupt document" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert not llm.messages


def test_wrong_media_type_is_rejected(stack):
    client, _, _ = stack
    assert upload(client, make_pdf("Exemplo"), "text/plain").status_code == 415


def test_encrypted_pdf_is_rejected(stack):
    client, _, _ = stack
    response = upload(client, make_pdf("Exemplo", encrypted=True))
    assert response.status_code == 422
    assert "senha" in response.json()["detail"]


@pytest.mark.parametrize("texts", [("",), ("Texto extraivel", "")])
def test_scanned_and_mixed_documents_are_rejected_entirely(stack, texts):
    client, llm, _ = stack
    response = upload(client, make_pdf(*texts))
    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]
    assert "text" not in response.json()
    assert not llm.messages


def test_more_than_200_pages_are_accepted(stack):
    client, _, _ = stack
    response = upload(client, make_pdf(*[f"Pagina ficticia {i}" for i in range(201)]))
    assert response.status_code == 200
    assert response.json()["pages"] == 201
    assert response.json()["text"].endswith("Pagina ficticia 200")


def test_more_than_200_thousand_characters_are_accepted(stack):
    client, _, _ = stack
    text = "Texto ficticio. " * 13_000
    response = upload(client, make_pdf(text))
    assert response.status_code == 200
    assert response.json()["text"] == text.strip()
    assert len(response.json()["text"]) > 200_000


def test_upload_larger_than_20_mib_reaches_extractor_without_truncation(stack, monkeypatch):
    client, llm, _ = stack
    content = b"%PDF-" + b"x" * (21 * 1024 * 1024)

    def extract(received, **_kwargs):
        assert received == content
        return pdf.ExtractedPdf(text="Texto ficticio", pages=1)

    monkeypatch.setattr("pii_gateway.app.extract_pdf", extract)
    response = client.post(
        "/extract-pdf", content=iter([content[:8], content[8:]]),
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "Texto ficticio"
    assert not llm.messages


def test_pdf_without_pages_is_rejected(stack):
    client, _, _ = stack
    response = upload(client, make_pdf())
    assert response.status_code == 422
    assert "não contém páginas" in response.json()["detail"]


def test_parser_errors_are_generic_and_not_logged(stack, monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret-document-content")

    client, _, _ = stack
    monkeypatch.setattr(pdf, "PdfReader", fail)
    response = upload(client, b"%PDF-1.7")
    assert response.status_code == 422
    assert "secret-document-content" not in response.text + caplog.text


def test_scanned_page_uses_local_ocr(stack, monkeypatch):
    client, llm, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("Texto ficticio do scan", False))
    response = upload(client, make_pdf(""))
    assert response.status_code == 200
    assert response.json()["text"] == "Texto ficticio do scan"
    assert response.json()["page_info"] == [{"page": 1, "method": "ocr", "review_required": False}]
    assert not llm.messages


def test_force_ocr_replaces_bad_text_layer(stack, monkeypatch):
    client, _, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("Texto corrigido", True))
    response = client.post("/extract-pdf?force_ocr=true", content=make_pdf("Camada defeituosa"), headers={"Content-Type": "application/pdf"})
    assert response.status_code == 200
    assert response.json()["text"] == "Texto corrigido"
    assert response.json()["page_info"][0]["review_required"] is True
    assert response.json()["warnings"]


def test_mixed_page_with_header_uses_ocr(stack, monkeypatch):
    client, _, _ = stack
    from PIL import Image
    image = Image.new("RGB", (600, 800), "white")
    output = BytesIO()
    image.save(output, format="PDF")
    image.close()
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda *_: "Cabeçalho digital")
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("Conteudo completo do scan", False))
    response = upload(client, output.getvalue())
    assert response.status_code == 200
    assert response.json()["text"] == "Conteudo completo do scan"
    assert response.json()["page_info"][0]["method"] == "ocr"


def test_stream_progress_and_complete_result(stack, monkeypatch):
    client, llm, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("Segunda pagina OCR", False))
    response = client.post("/extract-pdf/stream", content=make_pdf("Primeira pagina", ""), headers={"Content-Type": "application/pdf"})
    import json
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert events[0]["type"] == "progress"
    assert [event["completed"] for event in events if event["type"] == "progress"] == [0, 0, 1, 1, 2]
    assert all("text" not in event for event in events[:-1])
    assert events[-1]["type"] == "complete"
    assert events[-1]["text"] == "Primeira pagina\n\nSegunda pagina OCR"
    assert not llm.messages


def test_stream_failure_does_not_return_partial_text(stack):
    client, llm, _ = stack
    response = client.post("/extract-pdf/stream", content=make_pdf("Texto parcial secreto", ""), headers={"Content-Type": "application/pdf"})
    import json
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error"
    assert all(event["type"] != "complete" and "text" not in event for event in events)
    assert "Texto parcial secreto" not in response.text
    assert not llm.messages


def test_cancellation_between_pages_discards_result():
    from threading import Event
    from pii_gateway.ocr import ExtractionCancelled
    cancel = Event()
    events = pdf.pdf_events(make_pdf("Primeira", "Segunda"), cancel=cancel)
    assert next(events)["type"] == "progress"
    next(events)
    next(events)
    cancel.set()
    with pytest.raises(ExtractionCancelled):
        next(events)


def test_empty_ocr_output_completes_with_warning(stack, monkeypatch):
    client, _, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("", False))
    response = upload(client, make_pdf(""))
    assert response.status_code == 200
    assert response.json()["text"] == ""
    assert response.json()["page_info"] == [{"page": 1, "method": "empty", "review_required": True}]
    assert "Página 1" in response.json()["warnings"][0]


def test_blank_page_between_text_pages_does_not_stop_import(stack, monkeypatch):
    client, llm, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("", False))
    response = upload(client, make_pdf("Primeira pagina", "", "Terceira pagina"))
    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Primeira pagina\n\nTerceira pagina"
    assert data["pages"] == 3
    assert [page["page"] for page in data["page_info"]] == [1, 2, 3]
    assert [page["method"] for page in data["page_info"]] == ["text", "empty", "text"]
    assert "Página 2" in data["warnings"][0]
    assert not llm.messages


def test_stream_completes_past_blank_page(stack, monkeypatch):
    import json
    client, llm, _ = stack
    monkeypatch.setattr(pdf, "recognize_page", lambda *_: OcrResult("", False))
    response = client.post("/extract-pdf/stream", content=make_pdf("", "Segunda pagina", ""), headers={"Content-Type": "application/pdf"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "complete"
    assert events[-1]["text"] == "Segunda pagina"
    assert events[-1]["pages"] == 3
    assert len(events[-1]["warnings"]) == 2
    assert [event["completed"] for event in events if event["type"] == "progress"][-1] == 3
    assert not llm.messages


def test_closing_stream_signals_worker_cancellation(stack, monkeypatch):
    import asyncio
    from threading import Event

    client, _, _ = stack
    observed = Event()

    def events(*_args, cancel, **_kwargs):
        yield {"type": "progress", "completed": 0, "pages": 1}
        if cancel.wait(2):
            observed.set()

    class Request:
        headers = {"content-type": "application/pdf"}

        async def body(self):
            return b"%PDF-ficticio"

    monkeypatch.setattr("pii_gateway.app.pdf_events", events)
    endpoint = next(route.endpoint for route in client.app.routes if route.path == "/extract-pdf/stream")

    async def exercise():
        response = await endpoint(Request())
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert await asyncio.to_thread(observed.wait, 2)

    asyncio.run(exercise())


@pytest.mark.parametrize("text,expected", [("", True), ("???", True), ("Texto \ufffd", True), ("Texto normal", False)])
def test_text_quality_heuristic(text, expected):
    assert pdf.poor_text(text) is expected
