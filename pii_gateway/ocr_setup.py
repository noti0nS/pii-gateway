"""Preparação explícita de modelos: só este comando faz downloads de OCR."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path

import httpx

from pii_gateway.ocr_backends import model_directory

RAPID_MODELS = {
    "det.onnx": ("det/ch_PP-OCRv5_det_mobile.onnx", "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae"),
    "rec.onnx": ("rec/latin_PP-OCRv5_rec_mobile.onnx", "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a"),
}


def prepare_rapid(directory: Path):
    import onnxruntime as ort
    directory.mkdir(parents=True, exist_ok=True)
    for name, (relative, expected) in RAPID_MODELS.items():
        target = directory / name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("Modelo existente diverge do checksum esperado; verifique-o antes de substituir.")
            continue
        url = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/" + relative
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError("Checksum do modelo OCR inválido.")
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    session = ort.InferenceSession(str(directory / "rec.onnx"), providers=["CPUExecutionProvider"])
    characters = session.get_modelmeta().custom_metadata_map["character"]
    (directory / "keys.txt").write_text(characters, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Baixa modelos locais de OCR sem receber documentos.")
    parser.add_argument("backend", choices=["rapidocr", "smoldocling", "paddleocr-vl"])
    args = parser.parse_args()
    directory = model_directory(args.backend)
    if args.backend == "rapidocr":
        prepare_rapid(directory)
    else:
        from huggingface_hub import snapshot_download
        repository = "docling-project/SmolDocling-256M-preview" if args.backend == "smoldocling" else "PaddlePaddle/PaddleOCR-VL-1.6"
        snapshot_download(repository, local_dir=directory, allow_patterns=["*.safetensors", "*.json", "*.txt", "*.model", "*.jinja"])
    print(f"Modelos preparados em {directory}. Abra Configurar na demo para atualizar o status.")


if __name__ == "__main__":
    main()
