from types import SimpleNamespace

from pii_gateway import ner


class FakeModel:
    def __init__(self):
        self.calls = []
        self.config = SimpleNamespace(max_len=384)
        # Um token por caractere força o teste do orçamento sem modelo real.
        self.data_processor = SimpleNamespace(transformer_tokenizer=SimpleNamespace(encode=lambda text, **_: list(text)))

    def predict_entities(self, text, labels, threshold):
        assert len(text) <= 192
        self.calls.append(text)
        return [
            {"text": "Maria Silva", "label": "person", "start": start, "end": start + 11}
            for start in range(len(text)) if text.startswith("Maria Silva", start)
        ]


def test_long_text_is_processed_to_end_with_correct_offsets(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(ner, "load_model", lambda *_: model)
    text = "inicio " * 200 + "Maria Silva" + " fim" * 200 + " Maria Silva"
    hits = ner.detect_l2(text)
    assert len(model.calls) > 2
    assert len(hits) == 2
    assert [hit.start for hit in hits] == [text.index("Maria Silva"), text.rindex("Maria Silva")]
    assert all(text[hit.start:hit.end] == hit.text for hit in hits)


def test_overlap_protects_entity_at_window_boundary(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(ner, "load_model", lambda *_: model)
    # Primeira janela tem 125 caracteres; a entidade cruza esse limite.
    text = "x " * 59 + "Maria Silva" + " y" * 200
    hits = ner.detect_l2(text)
    assert len(hits) == 1
    assert hits[0].start == 118


def test_empty_text_never_loads_model(monkeypatch):
    monkeypatch.setattr(ner, "load_model", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert ner.detect_l2("   ") == []
