"""Adaptadores opcionais. Protocolo privado via pipes, sem logs de documentos."""

from __future__ import annotations

import base64
import json
import os
import sys
from io import BytesIO


def load_engine(backend: str, device: str):
    from pii_gateway.ocr_backends import model_directory
    directory = model_directory(backend)
    if backend == "rapidocr":
        from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
        engine = RapidOCR(params={
            "Global.use_cls": False, "Global.text_score": 0.0, "Global.log_level": "critical",
            "Det.model_path": str(directory / "det.onnx"), "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Rec.model_path": str(directory / "rec.onnx"), "Rec.rec_keys_path": str(directory / "keys.txt"),
            "Rec.lang_type": LangRec.LATIN, "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        })

        def recognize(image):
            result = engine(image, use_cls=False)
            texts = result.txts or ()
            scores = result.scores or ()
            return "\n".join(texts), any(score < 0.6 for score in scores)
        return recognize
    if backend == "paddleocr":
        import numpy as np
        from paddleocr import PaddleOCR
        engine = PaddleOCR(
            lang="pt", ocr_version="PP-OCRv5", device="cpu",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="latin_PP-OCRv5_mobile_rec",
            text_detection_model_dir=str(directory / "det"),
            text_recognition_model_dir=str(directory / "rec"),
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, text_rec_score_thresh=0.0,
        )

        def recognize(image):
            texts, scores = [], []
            for result in engine.predict(np.array(image.convert("RGB"))[:, :, ::-1]):
                payload = result.json["res"]
                texts.extend(payload["rec_texts"])
                scores.extend(payload["rec_scores"])
            return "\n".join(texts), any(score < 0.6 for score in scores)
        return recognize

    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA indisponível")
    processor = AutoProcessor.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False)
    model = AutoModelForImageTextToText.from_pretrained(
        str(directory), local_files_only=True, trust_remote_code=False,
        torch_dtype=torch.float32 if device == "cpu" else torch.float16,
    ).to(device).eval()

    def recognize(image):
        prompt = "Convert this page to docling." if backend == "smoldocling" else "OCR:"
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
        template = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=template, images=[image], return_tensors="pt").to(device)
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=8192, do_sample=False)
        tokens = output[0][inputs["input_ids"].shape[-1]:]
        # Não apresenta uma página silenciosamente truncada pelo orçamento do VLM.
        eos = model.generation_config.eos_token_id
        eos = eos if isinstance(eos, list) else [eos]
        if len(tokens) >= 8192 and int(tokens[-1]) not in eos:
            raise ValueError("Saída incompleta")
        text = processor.decode(tokens, skip_special_tokens=backend != "smoldocling")
        if backend == "smoldocling":
            from docling_core.types.doc import DoclingDocument
            from docling_core.types.doc.document import DocTagsDocument
            tags = DocTagsDocument.from_doctags_and_image_pairs([text], [image])
            text = DoclingDocument.load_from_doctags(tags, document_name="page").export_to_markdown()
        return text, True  # VLM não fornece confiança OCR calibrada; sempre revisar.
    return recognize


def main():
    # Captura inclusive prints nativos das dependências; só o protocolo sai no stdout.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    # Modelos devem estar preparados. Impede downloads HTTP implícitos pelo Python.
    import socket
    def offline(*_args, **_kwargs):
        raise OSError("Inferência OCR offline")
    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    from PIL import Image
    engine = None
    for line in sys.stdin.buffer:
        try:
            payload = json.loads(line)
            image = Image.open(BytesIO(base64.b64decode(payload["image"]))).convert("RGB")
            try:
                if engine is None:
                    engine = load_engine(sys.argv[1], sys.argv[2])
                text, uncertain = engine(image)
                result = {"text": text, "low_confidence": uncertain}
            finally:
                image.close()
        except Exception:
            result = {"error": True}
        protocol.write(json.dumps(result, ensure_ascii=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
