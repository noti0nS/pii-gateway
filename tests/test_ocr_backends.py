from dataclasses import replace

from fastapi.testclient import TestClient

from pii_gateway.app import create_app
from pii_gateway.ocr import OcrResult
from pii_gateway.ocr_backends import CATALOG, OcrOptions, backend_status
from pii_gateway.pdf import extract_pdf
from tests.test_pdf import make_pdf


def test_catalog_exposes_all_five_options_without_secrets(stack, monkeypatch):
    client, _, _ = stack
    monkeypatch.setenv("LLM_API_KEY", "must-not-be-exposed")
    monkeypatch.setattr("pii_gateway.app.backend_status", lambda name: {"id": name, "ready": False})
    response = client.get("/ocr/options")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {item["id"] for item in response.json()["backends"]} == set(CATALOG)
    assert response.json()["default_backend"] == "tesseract"
    assert "must-not-be-exposed" not in response.text


def test_status_requires_local_models_even_with_installed_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr("pii_gateway.ocr_backends.importlib.util.find_spec", lambda *_: object())
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))
    assert backend_status("rapidocr")["ready"] is False
    for name in ("det.onnx", "rec.onnx", "keys.txt"):
        (tmp_path / name).write_bytes(b"fixture")
    assert backend_status("rapidocr")["ready"] is True


def test_optional_worker_is_reused_for_all_pdf_pages(monkeypatch):
    workers = []

    class Worker:
        def __init__(self, options):
            assert options.backend == "rapidocr"
            self.calls = 0
            self.closed = False
            workers.append(self)

        def __enter__(self):
            return self

        def recognize(self, *_):
            self.calls += 1
            return OcrResult("Texto ficticio OCR", False)

        def __exit__(self, *_):
            self.closed = True

    monkeypatch.setattr("pii_gateway.pdf.OcrWorker", Worker)
    result = extract_pdf(make_pdf("", ""), ocr_options=OcrOptions("rapidocr"))
    assert len(workers) == 1 and workers[0].calls == 2 and workers[0].closed
    assert result.ocr_backend == "rapidocr"
    assert result.pages == 2


def test_worker_not_started_for_textual_pdf(monkeypatch):
    def fail(*_):
        raise AssertionError("Não deve inicializar modelos para texto digital")
    monkeypatch.setattr("pii_gateway.pdf.OcrWorker", fail)
    result = extract_pdf(make_pdf("Texto ficticio"), ocr_options=OcrOptions("paddleocr-vl"))
    assert result.text == "Texto ficticio"


def test_request_selects_backend_without_changing_server_default(stack, monkeypatch):
    client, _, _ = stack
    seen = []
    from pii_gateway.pdf import ExtractedPdf

    def extract(_content, **kwargs):
        options = kwargs["ocr_options"]
        seen.append(options)
        return ExtractedPdf("Texto ficticio", 1, ocr_backend=options.backend)

    monkeypatch.setattr("pii_gateway.app.extract_pdf", extract)
    headers = {"Content-Type": "application/pdf"}
    first = client.post("/extract-pdf?backend=smoldocling&device=cpu", content=b"%PDF-", headers=headers)
    second = client.post("/extract-pdf", content=b"%PDF-", headers=headers)
    assert first.status_code == second.status_code == 200
    assert [options.backend for options in seen] == ["smoldocling", "tesseract"]


def test_unknown_backend_and_unsupported_device_are_rejected(stack):
    client, _, _ = stack
    headers = {"Content-Type": "application/pdf"}
    assert client.post("/extract-pdf?backend=remote", content=b"%PDF-", headers=headers).status_code == 422
    assert client.post("/extract-pdf?backend=rapidocr&device=cuda", content=b"%PDF-", headers=headers).status_code == 422


def test_configured_server_defaults_are_used(stack, monkeypatch):
    client, llm, _ = stack
    settings = replace(client.app.state.gateway.settings, ocr_backend="rapidocr")
    monkeypatch.setattr("pii_gateway.app.backend_status", lambda name: {"id": name})
    with TestClient(create_app(settings, llm=llm)) as configured:
        assert configured.get("/ocr/options").json()["default_backend"] == "rapidocr"


def test_ui_has_configurable_workspace_and_local_styles(stack):
    client, _, _ = stack
    page = client.get("/")
    assert "Configurar OCR" in page.text
    assert 'id="settings-dialog"' in page.text
    assert 'id="ocr-backend"' in page.text
    assert client.get("/assets/ui.css").status_code == 200


def test_rapid_adapter_uses_local_latin_models_and_keeps_low_confidence(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from pii_gateway.ocr_worker import load_engine
    captured = {}

    class Engine:
        def __init__(self, *, params):
            captured.update(params)

        def __call__(self, *_args, **kwargs):
            assert kwargs["use_cls"] is False
            return SimpleNamespace(txts=("Texto ficticio",), scores=(0.4,))

    module = SimpleNamespace(
        RapidOCR=Engine, LangRec=SimpleNamespace(LATIN="latin"),
        ModelType=SimpleNamespace(MOBILE="mobile"), OCRVersion=SimpleNamespace(PPOCRV5="v5"),
    )
    monkeypatch.setitem(sys.modules, "rapidocr", module)
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))
    recognize = load_engine("rapidocr", "cpu")
    assert recognize(object()) == ("Texto ficticio", True)
    assert captured["Rec.lang_type"] == "latin"
    assert captured["Global.text_score"] == 0.0
    assert captured["Rec.model_path"] == str(tmp_path / "rec.onnx")


def test_paddle_adapter_uses_local_portuguese_models(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from pii_gateway.ocr_worker import load_engine
    captured = {}

    class Array:
        def __getitem__(self, _key):
            return self

    class Image:
        def convert(self, _mode):
            return self

    class Engine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def predict(self, _image):
            return [SimpleNamespace(json={"res": {"rec_texts": ["Texto ficticio"], "rec_scores": [0.9]}})]

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=Engine))
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(array=lambda _: Array()))
    monkeypatch.setenv("PADDLEOCR_MODEL_DIR", str(tmp_path))
    assert load_engine("paddleocr", "cpu")(Image()) == ("Texto ficticio", False)
    assert captured["lang"] == "pt"
    assert captured["ocr_version"] == "PP-OCRv5"
    assert captured["text_recognition_model_dir"] == str(tmp_path / "rec")


def test_worker_pipes_and_offline_environment(monkeypatch):
    from io import BytesIO
    from threading import Event
    from pii_gateway import ocr_backends
    created = []
    monkeypatch.setenv("VAULT_KEY", "never-forward-to-worker")

    class Process:
        def __init__(self, command, **kwargs):
            created.append(self)
            assert "pii_gateway.ocr_worker" in command
            assert kwargs["stderr"] == ocr_backends.subprocess.DEVNULL
            assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
            assert "VAULT_KEY" not in kwargs["env"]
            self.stdin = BytesIO()
            self.stdout = BytesIO(b'{"text":"Texto ficticio","low_confidence":false}\n')
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self):
            pass

    monkeypatch.setattr(ocr_backends, "backend_status", lambda _: {"ready": True})
    monkeypatch.setattr(ocr_backends, "render_png", lambda *_: b"fictional-png")
    monkeypatch.setattr(ocr_backends.subprocess, "Popen", Process)
    with ocr_backends.OcrWorker(OcrOptions("rapidocr")) as worker:
        assert worker.recognize(object(), Event()).text == "Texto ficticio"
    assert created[0].stopped
    assert created[0].stdin.closed and created[0].stdout.closed
