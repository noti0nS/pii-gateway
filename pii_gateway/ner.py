"""L2: GLiNER em processo, só CPU, carregado na primeira uso.

O nome do modelo foi conferido no Hugging Face:
`urchade/gliner_multi_pii-v1` (GLiNER PII multilíngue, inclui português).
Os testes mockam `load_model` e não baixam peso nenhum.
"""

from __future__ import annotations

import threading

from pii_gateway.detect import Detection

MODEL_ID = "urchade/gliner_multi_pii-v1"

REQUEST_LABELS: tuple[str, ...] = (
    "person",
    "organization",
    "address",
    "email",
    "phone number",
    "cpf",
    "cnpj",
)

LABEL_TO_TYPE = {
    "person": "PERSON",
    "organization": "ORG",
    "address": "ADDRESS",
    "email": "EMAIL",
    "email address": "EMAIL",
    "phone number": "PHONE",
    "mobile phone number": "PHONE",
    "landline phone number": "PHONE",
    "cpf": "CPF",
    "cnpj": "CNPJ",
}

_lock = threading.Lock()
_models: dict[str, object] = {}


def load_model(model_id: str = MODEL_ID) -> object:
    cached = _models.get(model_id)
    if cached is not None:
        return cached
    with _lock:
        cached = _models.get(model_id)
        if cached is not None:
            return cached
        from gliner import GLiNER

        model = GLiNER.from_pretrained(model_id)
        if hasattr(model, "to"):
            model.to("cpu")
        _models[model_id] = model
        return model


def detect_l2(text: str, *, model_id: str = MODEL_ID) -> list[Detection]:
    if not text.strip():
        return []
    model = load_model(model_id)
    raw_entities = model.predict_entities(text, list(REQUEST_LABELS), threshold=0.5)
    found: list[Detection] = []
    for entity in raw_entities:
        mapped = _map_entity(text, entity)
        if mapped is not None:
            found.append(mapped)
    return found


def _map_entity(text: str, entity: object) -> Detection | None:
    if not isinstance(entity, dict):
        return None
    label = str(entity.get("label") or "").casefold()
    entity_type = LABEL_TO_TYPE.get(label)
    snippet = str(entity.get("text") or "")
    if entity_type is None or not snippet:
        return None
    start = entity.get("start")
    end = entity.get("end")
    if isinstance(start, int) and isinstance(end, int) and text[start:end] == snippet:
        return Detection(start, end, snippet, entity_type, "L2")
    index = text.find(snippet)
    if index < 0:
        return None
    return Detection(index, index + len(snippet), snippet, entity_type, "L2")
