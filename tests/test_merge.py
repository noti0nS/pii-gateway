from pii_gateway.detect import (
    Detection,
    Term,
    allow_spans,
    build_dictionary,
    detect_dictionary,
    is_allowed,
    merge_detections,
)
from pii_gateway.ner import MODEL_ID
from pii_gateway.normalize import fold


def test_model_id_matches_the_spec():
    assert MODEL_ID == "urchade/gliner_multi_pii-v1"


def test_longest_span_wins_over_higher_layer():
    short = Detection(0, 6, "Helena", "PERSON", "L3")
    long = Detection(0, 14, "Helena Duarte", "PERSON", "L2")
    merged = merge_detections([short, long])
    assert [(item.layer, item.text) for item in merged] == [("L2", "Helena Duarte")]


def test_equal_length_uses_layer_priority():
    spans = [
        Detection(0, 10, "Helena Xyz", "PERSON", "L2"),
        Detection(0, 10, "Helena Xyz", "PERSON", "L3"),
        Detection(0, 10, "Helena Xyz", "EMAIL", "L1"),
    ]
    merged = merge_detections(spans)
    assert len(merged) == 1
    assert merged[0].layer == "L1"


def test_l1_that_sticks_out_is_preserved():
    identifier = Detection(0, 14, "123.456.789-09", "CPF", "L1")
    person = Detection(8, 30, "x" * 22, "PERSON", "L2")
    merged = merge_detections([person, identifier])
    assert [(item.layer, item.start, item.end) for item in merged] == [
        ("L1", 0, 14),
        ("L2", 14, 30),
    ]


def test_l1_fully_inside_a_longer_span_stays_covered():
    text = "João Silva CPF 123.456.789-09"
    person = Detection(0, len(text), text, "PERSON", "L2")
    start = text.index("123")
    identifier = Detection(start, start + 14, text[start : start + 14], "CPF", "L1")
    merged = merge_detections([person, identifier])
    assert len(merged) == 1
    assert merged[0].layer == "L2"


def test_disjoint_spans_are_both_kept_in_order():
    person = Detection(0, 6, "Helena", "PERSON", "L3")
    org = Detection(10, 14, "Ltda", "ORG", "L2")
    merged = merge_detections([org, person])
    assert [item.start for item in merged] == [0, 10]


def test_dictionary_matches_case_and_accents_and_propagates_l2():
    name = "Helena Duarte Ferreira"
    text = f"{name} disse. HELENA DUARTE FERREIRA saiu. H. Ferreira assinou."
    l2 = [Detection(0, len(name), name, "PERSON", "L2")]
    terms = build_dictionary([], l2, [])
    found = detect_dictionary(text, terms)
    folded = [fold(item.text) for item in found]
    assert folded.count("helena duarte ferreira") == 2
    assert "h. ferreira" in folded

    accent = detect_dictionary(
        "No rol consta JOSE  DA SILVA e tambem J. Silva.",
        build_dictionary([Term("José da Silva", "PERSON")], [], []),
    )
    assert {fold(item.text) for item in accent} >= {"jose da silva", "j. silva"}


def test_allow_list_drops_the_public_term():
    court = "Tribunal de Justiça do Estado de São Paulo"
    text = f"O {court} citou Helena Duarte Ferreira."
    allow = [court]
    terms = build_dictionary(
        [Term("Helena Duarte Ferreira", "PERSON"), Term(court, "ORG")],
        [],
        allow,
    )
    assert all(fold(term.value) != fold(court) for term in terms)
    spans = allow_spans(text, allow)
    kept = [
        hit.text
        for hit in detect_dictionary(text, terms)
        if not is_allowed(hit, allow, spans)
    ]
    assert kept == ["Helena Duarte Ferreira"]
