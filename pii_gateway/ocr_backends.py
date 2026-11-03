"""Catálogo de motores locais e worker isolado por importação de documento."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Literal

from pii_gateway.ocr import OcrError, OcrResult, check_cancelled, render_png, tesseract_command, tesseract_data_options

Backend = Literal["tesseract", "rapidocr", "paddleocr", "smoldocling", "paddleocr-vl"]
Device = Literal["cpu", "cuda"]

CATALOG = {
    "tesseract": {"name": "Tesseract", "description": "OCR tradicional com rede neural. Português instalado separadamente.", "hardware": "CPU · instalação leve", "modules": (), "env": None, "setup": "Instale Tesseract e por.traineddata. Consulte a seção OCR do README."},
    "rapidocr": {"name": "RapidOCR · PP-OCRv5 Mobile", "description": "Modelos ONNX compactos, com reconhecedor latino para português.", "hardware": "CPU · recomendado para PCs comuns", "modules": ("rapidocr", "onnxruntime"), "env": "RAPIDOCR_MODEL_DIR", "setup": "uv sync --extra ocr-rapid\nuv run --no-sync python -m pii_gateway.ocr_setup rapidocr"},
    "paddleocr": {"name": "PaddleOCR · PP-OCRv5", "description": "Pipeline de detecção e reconhecimento em português. Runtime Paddle separado.", "hardware": "CPU · instalação maior", "modules": ("paddleocr", "paddle"), "env": "PADDLEOCR_MODEL_DIR", "setup": "uv sync --extra ocr-paddle\nInstale PaddlePaddle para seu Python/SO (documentação oficial).\nPrepare modelos locais em PADDLEOCR_MODEL_DIR/det e /rec."},
    "smoldocling": {"name": "SmolDocling · 256M", "description": "Conversão multimodal com estrutura. Experimental: português precisa de avaliação.", "hardware": "CPU ou CUDA · mais lento em CPU", "modules": ("torch", "transformers", "docling_core"), "env": "SMOLDOCLING_MODEL_DIR", "setup": "uv sync --extra ocr-vlm\nuv run --no-sync python -m pii_gateway.ocr_setup smoldocling"},
    "paddleocr-vl": {"name": "PaddleOCR-VL · 1.6", "description": "Modelo multimodal para texto e documentos complexos. Saída exige revisão.", "hardware": "CPU ou CUDA · GPU recomendada", "modules": ("torch", "transformers"), "env": "PADDLEOCR_VL_MODEL_DIR", "setup": "uv sync --extra ocr-vlm\nuv run --no-sync python -m pii_gateway.ocr_setup paddleocr-vl"},
}


@dataclass(frozen=True)
class OcrOptions:
    backend: Backend = "tesseract"
    device: Device = "cpu"


def model_directory(backend: str) -> Path:
    variable = CATALOG[backend]["env"]
    configured = os.environ.get(variable, "") if variable else ""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "pii-gateway" / "models"
    return Path(configured) if configured else root / backend


def backend_status(backend: str) -> dict[str, object]:
    item = CATALOG[backend]
    missing = [module for module in item["modules"] if importlib.util.find_spec(module) is None]
    models_ready = False
    if backend == "tesseract":
        try:
            language = os.environ.get("TESSERACT_LANG", "por")
            result = subprocess.run([tesseract_command(), *tesseract_data_options(language), "--list-langs"], capture_output=True, timeout=5)
            languages = result.stdout.decode("utf-8", errors="replace").splitlines()
            models_ready = result.returncode == 0 and all(part in languages for part in language.split("+"))
        except (OcrError, OSError, subprocess.SubprocessError):
            pass
    else:
        directory = model_directory(backend)
        if backend == "rapidocr":
            models_ready = all((directory / name).is_file() for name in ("det.onnx", "rec.onnx", "keys.txt"))
        elif backend == "paddleocr":
            models_ready = all((directory / name / "inference.json").is_file() for name in ("det", "rec"))
        else:
            models_ready = (directory / "config.json").is_file() and any(directory.glob("*.safetensors")) and (directory / "preprocessor_config.json").is_file()
    ready = not missing and models_ready
    return {
        "id": backend, "name": item["name"], "description": item["description"],
        "hardware": item["hardware"], "ready": ready,
        "status": "Arquivos e dependências encontrados; inferência ainda não testada." if ready else "Instale as dependências e prepare os modelos locais.",
        "setup": item["setup"], "model_env": item["env"],
        "devices": ["cpu", "cuda"] if backend in {"smoldocling", "paddleocr-vl"} else ["cpu"],
    }


class OcrWorker:
    """Um processo por PDF: reutiliza modelos entre páginas e é encerrável."""

    def __init__(self, options: OcrOptions):
        self.options = options
        self.process = None

    def __enter__(self):
        if not backend_status(self.options.backend)["ready"]:
            raise OcrError("Motor OCR não preparado. Abra Configurar e siga as instruções desse motor.")
        if self.options.device == "cuda" and self.options.backend not in {"smoldocling", "paddleocr-vl"}:
            raise OcrError("Este adaptador usa CPU. Escolha CPU nas configurações.")
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True")
        for key in ("LLM_API_KEY", "TENANT_KEY", "VAULT_KEY", "ANALYSIS_DB_URL", "VAULT_DB_URL", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            env.pop(key, None)  # OCR não precisa das chaves do gateway ou do provedor.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pii_gateway.ocr_worker", self.options.backend, self.options.device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return self

    def __exit__(self, *_args):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()

    def recognize(self, page: object, cancel: Event) -> OcrResult:
        png = render_png(page, cancel)
        response: Queue = Queue()

        def exchange():
            try:
                self.process.stdin.write(json.dumps({"image": base64.b64encode(png).decode("ascii")}).encode() + b"\n")
                self.process.stdin.flush()
                response.put(json.loads(self.process.stdout.readline()))
            except Exception:
                response.put({"error": True})

        thread = Thread(target=exchange, daemon=True)
        thread.start()
        try:
            while True:
                check_cancelled(cancel)
                try:
                    result = response.get(timeout=0.1)
                    break
                except Empty:
                    continue
            if result.get("error") or not isinstance(result.get("text"), str):
                raise OcrError("Falha no motor OCR selecionado. Confira seus modelos locais, dependências e dispositivo em Configurar.")
            return OcrResult(result["text"], bool(result.get("low_confidence", True)))
        finally:
            if thread.is_alive():
                self.process.kill()
                thread.join(timeout=5)
