"""Reidratação de stream com hold-back de 32 caracteres.

O surrogate pode chegar partido (`PERS` + `ON_a91f3c7e`). A cauda só é
liberada quando não há prefixo de surrogate incompleto.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator

from pii_gateway.surrogate import COMPLETE_HEX_LENGTHS, PREFIXES, SURROGATE_RE

HOLDBACK = 32
Replace = Callable[[str], str]


def _incomplete(fragment: str) -> bool:
    for prefix in PREFIXES:
        if prefix.startswith(fragment) and 2 <= len(fragment) < len(prefix):
            return True
        if fragment == prefix:
            return True
        needle = prefix + "_"
        if not fragment.startswith(needle):
            continue
        hexpart = fragment[len(needle) :]
        if not re.fullmatch(r"[0-9a-f]*", hexpart):
            return False
        if len(hexpart) > 64 or len(hexpart) in COMPLETE_HEX_LENGTHS:
            return False
        return True
    return False


def _incomplete_suffix(text: str) -> str:
    start = max(0, len(text) - 80)
    segment = text[start:]
    best = ""
    for index in range(len(segment)):
        if index and segment[index - 1].isalnum():
            continue
        fragment = segment[index:]
        if _incomplete(fragment) and len(fragment) > len(best):
            best = fragment
    return best


def safe_prefix_length(buffer: str, holdback: int = HOLDBACK) -> int:
    """Quantos caracteres do início já podem sair sem cortar um surrogate."""
    if len(buffer) <= holdback:
        return 0
    cut = len(buffer) - holdback
    suffix = _incomplete_suffix(buffer[:cut])
    if suffix:
        return max(0, cut - len(suffix))
    head = buffer[:cut]
    match = None
    for match in SURROGATE_RE.finditer(head):
        pass
    if match is not None and match.end() == len(head):
        if re.match(r"[0-9a-f]", buffer[cut:]):
            return match.start()
    return cut


class HoldbackRehydrator:
    def __init__(self, replace: Replace, holdback: int = HOLDBACK) -> None:
        self._replace = replace
        self._holdback = holdback
        self._buffer = ""

    def push(self, chunk: str) -> list[str]:
        self._buffer += chunk
        return self._drain(final=False)

    def finish(self) -> list[str]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[str]:
        emitted: list[str] = []
        if final:
            if self._buffer:
                emitted.append(self._replace(self._buffer))
            self._buffer = ""
            return emitted
        while len(self._buffer) > self._holdback:
            cut = safe_prefix_length(self._buffer, self._holdback)
            if cut <= 0:
                break
            emitted.append(self._replace(self._buffer[:cut]))
            self._buffer = self._buffer[cut:]
        return emitted


def rehydrate_stream(
    chunks: Iterable[str],
    replace: Replace,
    holdback: int = HOLDBACK,
) -> Iterator[str]:
    holder = HoldbackRehydrator(replace, holdback=holdback)
    for chunk in chunks:
        yield from holder.push(chunk)
    yield from holder.finish()
