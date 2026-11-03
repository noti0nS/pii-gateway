import subprocess
from threading import Event

import pytest
from PIL import Image

from pii_gateway import ocr
from pii_gateway.detect import detect_l1
from pii_gateway.normalize import normalize_for_hash


TSV = "level\tpage_num\tblock_num\tpar_num\tline_num\tconf\ttext\n5\t1\t1\t1\t1\t95\tMaria\n5\t1\t1\t1\t1\t40\tSilva\n5\t1\t1\t1\t2\t92\tCPF\n"


def test_tsv_preserves_lines_and_flags_uncertain_words():
    result = ocr.parse_tsv(TSV)
    assert result.text == "Maria Silva\nCPF"
    assert result.low_confidence


class FakeBitmap:
    def __init__(self):
        self.closed = False

    def to_pil(self):
        return Image.new("RGB", (10, 10), "white")

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.bitmap = FakeBitmap()

    def render(self, *, scale):
        assert scale == 300 / 72
        return self.bitmap


def test_ocr_uses_pipes_and_never_echoes_process_errors(monkeypatch):
    commands = []

    class Process:
        returncode = 1

        def __init__(self, command, **kwargs):
            commands.append(command)
            assert kwargs["stdin"] == subprocess.PIPE
            assert kwargs["stdout"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.PIPE
            assert command[-2:] == ["-c", "tessedit_create_tsv=1"]

        def communicate(self, input=None, **kwargs):
            if input:
                assert input.startswith(b"\x89PNG")
            return b"secret-document-text", b"secret-error"

        def poll(self):
            return self.returncode

    monkeypatch.setattr(ocr, "tesseract_command", lambda: "fake-tesseract")
    monkeypatch.setattr(ocr.subprocess, "Popen", Process)
    page = FakePage()
    with pytest.raises(ocr.OcrError) as error:
        ocr.recognize_page(page, Event())
    assert "secret" not in str(error.value)
    assert commands[0][1:3] == ["stdin", "stdout"]
    assert page.bitmap.closed


def test_cancellation_kills_active_tesseract(monkeypatch):
    cancel = Event()
    killed = []

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, input=None, **kwargs):
            if self.returncode is None:
                cancel.set()
                raise subprocess.TimeoutExpired("tesseract", 0.25)
            return b"", b""

        def poll(self):
            return self.returncode

        def kill(self):
            killed.append(True)
            self.returncode = -1

    monkeypatch.setattr(ocr, "tesseract_command", lambda: "fake-tesseract")
    monkeypatch.setattr(ocr.subprocess, "Popen", Process)
    with pytest.raises(ocr.ExtractionCancelled):
        ocr.recognize_page(FakePage(), cancel)
    assert killed == [True]


@pytest.mark.parametrize("value,kind", [
    ("123.456.789-00", "CPF"),
    ("123.456.789-O9", "CPF"),
    ("123 456 789 00", "CPF"),
    ("12.345.678/0001-00", "CNPJ"),
    ("12.345.678/OOO1-95", "CNPJ"),
])
def test_conservative_detection_protects_ocr_candidates(value, kind):
    text = "Documento: " + value
    hits = detect_l1(text, conservative=True)
    assert any(hit.text == value and hit.type == kind and hit.review_required for hit in hits)
    assert not detect_l1(text)


def test_ocr_normalization_does_not_merge_distinct_errors():
    assert normalize_for_hash("CPF", "123.456.789-O9") != normalize_for_hash("CPF", "123.456.789-I9")
    assert normalize_for_hash("CPF", "123.456.789-O9") != normalize_for_hash("CPF", "1234567899")


def test_conservative_pipeline_protects_text_and_question(stack):
    client, llm, directory = stack
    value = "123.456.789-O9"
    response = client.post("/ask", json={
        "text": "Documento ficticio: " + value,
        "question": "Explique o documento " + value,
        "conservative_pii": True,
        "allow_terms": [],
    })
    assert response.status_code == 200
    assert value not in str(llm.messages)
    assert value.encode() not in (directory / "analysis.db").read_bytes()
    assert value in response.json()["rehydrated"]
    assert response.json()["detections"][0]["review_required"] is True


def test_longest_dictionary_hit_keeps_ocr_review_flag(stack):
    client, _, _ = stack
    response = client.post("/sanitize", json={
        "text": "Documento CPF 123.456.789-O9",
        "case_terms": [{"value": "CPF 123.456.789-O9", "type": "PERSON"}],
        "conservative_pii": True,
    })
    assert response.status_code == 200
    assert response.json()["detections"][0]["review_required"] is True
