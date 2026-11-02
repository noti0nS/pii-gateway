"""AES-256-GCM. O KMS é simulado: a chave vem de VAULT_KEY e fica no processo."""

from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_vault_key(raw: str) -> bytes:
    """Aceita 32 bytes em hex ou base64. Qualquer outro texto vira SHA-256.

    Em produção a chave sairia de um KMS. Aqui ela é derivada localmente.
    """
    text = raw.strip()
    try:
        blob = bytes.fromhex(text)
        if len(blob) == 32:
            return blob
    except ValueError:
        pass
    try:
        blob = base64.b64decode(text, validate=True)
        if len(blob) == 32:
            return blob
    except Exception:
        pass
    return hashlib.sha256(text.encode("utf-8")).digest()


def encrypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    return AESGCM(key).encrypt(nonce, data, None)


def decrypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, data, None)
