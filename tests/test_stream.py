from pii_gateway.stream import HOLDBACK, HoldbackRehydrator, rehydrate_stream


def test_holdback_is_32_and_a_short_chunk_waits():
    assert HOLDBACK == 32
    holder = HoldbackRehydrator(lambda text: text)
    assert holder.push("curto") == []
    assert holder.finish() == ["curto"]


def test_surrogate_split_across_chunks_is_rehydrated():
    token = "PERSON_a91f3c7e"
    prefix = "x" * 50

    def replace(text: str) -> str:
        return text.replace(token, "Helena Duarte Ferreira")

    chunks = [prefix + "O cliente PERS", "ON_a91f3c7e assinou."]
    rendered = "".join(rehydrate_stream(chunks, replace))
    assert rendered == prefix + "O cliente Helena Duarte Ferreira assinou."
    assert token not in rendered
    assert "PERS" not in rendered


def test_hex_split_and_early_flush():
    token = "PHONE_abcd1234"

    def replace(text: str) -> str:
        return text.replace(token, "(11) 98877-6655")

    holder = HoldbackRehydrator(replace)
    first = "".join(holder.push("Ligue PHONE_ab"))
    assert token not in first
    assert "PHONE_ab" not in first
    rest = "".join(holder.push("cd1234 hoje.") + holder.finish())
    assert first + rest == "Ligue (11) 98877-6655 hoje."
