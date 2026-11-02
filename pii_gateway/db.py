"""Dois bancos separados. Os tipos do SQLAlchemy são portáveis: Postgres é outra URL."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, Integer, LargeBinary, String, create_engine, delete, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from pii_gateway.config import Settings
from pii_gateway.crypto import decrypt as decrypt_bytes
from pii_gateway.crypto import derive_vault_key
from pii_gateway.crypto import encrypt as encrypt_bytes
from pii_gateway.normalize import normalize_for_hash
from pii_gateway.surrogate import SurrogateCollisionError, choose_surrogate, hmac_hex


class AnalysisBase(DeclarativeBase):
    pass


class VaultBase(DeclarativeBase):
    pass


class Entity(AnalysisBase):
    """Metadado analítico. Não há coluna de texto claro."""

    __tablename__ = "entity"

    surrogate: Mapped[str] = mapped_column(String, primary_key=True)
    entity_type: Mapped[str] = mapped_column("type", String, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VaultSecret(VaultBase):
    __tablename__ = "vault"

    surrogate: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VaultAccessLog(VaultBase):
    __tablename__ = "vault_access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    surrogate: Mapped[str] = mapped_column(String, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _sqlite_path(url: str) -> str | None:
    if not url.startswith("sqlite"):
        return None
    if url.endswith(":memory:") or url in {"sqlite://", "sqlite:///"}:
        return ":memory:"
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    return None


def make_engine(url: str):
    path = _sqlite_path(url)
    if path not in {None, ":memory:"}:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if path == ":memory:":
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.close()

    return engine


class Stores:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.key = derive_vault_key(settings.vault_key)
        self.analysis = make_engine(settings.analysis_db_url)
        self.vault = make_engine(settings.vault_db_url)
        self.index: dict[str, str] = {}

    def init(self) -> None:
        AnalysisBase.metadata.create_all(self.analysis)
        VaultBase.metadata.create_all(self.vault)
        self.reload()

    def reload(self) -> None:
        with Session(self.analysis) as session:
            rows = session.scalars(select(Entity)).all()
            self.index = {row.surrogate: row.value_hash for row in rows}

    def reset(self) -> None:
        with Session(self.analysis) as session:
            session.execute(delete(Entity))
            session.commit()
        with Session(self.vault) as session:
            session.execute(delete(VaultAccessLog))
            session.execute(delete(VaultSecret))
            session.commit()
        self.index.clear()

    def surrogate_for(self, entity_type: str, surface: str) -> str:
        digest = hmac_hex(normalize_for_hash(entity_type, surface), self.settings.tenant_key)
        token, _extended = choose_surrogate(entity_type, digest, self.index)
        self._persist(token, entity_type, digest, surface)
        self.index[token] = digest
        return token

    def _persist(self, token: str, entity_type: str, digest: str, surface: str) -> None:
        now = datetime.now(timezone.utc)
        with Session(self.analysis) as analysis, Session(self.vault) as vault:
            existing = analysis.get(Entity, token)
            if existing is not None:
                if existing.value_hash != digest:
                    raise SurrogateCollisionError(token)
                return
            analysis.add(
                Entity(
                    surrogate=token,
                    entity_type=entity_type,
                    value_hash=digest,
                    created_at=now,
                )
            )
            nonce = os.urandom(12)
            vault.add(
                VaultSecret(
                    surrogate=token,
                    ciphertext=encrypt_bytes(self.key, nonce, surface.encode("utf-8")),
                    nonce=nonce,
                    created_at=now,
                )
            )
            vault.commit()
            analysis.commit()

    def decrypt(self, token: str, purpose: str) -> str | None:
        with Session(self.vault) as session:
            row = session.get(VaultSecret, token)
            if row is None:
                return None
            plain = decrypt_bytes(self.key, bytes(row.nonce), bytes(row.ciphertext))
            session.add(
                VaultAccessLog(
                    surrogate=token,
                    purpose=purpose,
                    at=datetime.now(timezone.utc),
                )
            )
            session.commit()
        return plain.decode("utf-8")

    def dump(self) -> dict[str, object]:
        with Session(self.analysis) as session:
            entities = session.scalars(
                select(Entity).order_by(Entity.created_at, Entity.surrogate)
            ).all()
            analysis_rows = [
                {
                    "surrogate": row.surrogate,
                    "type": row.entity_type,
                    "value_hash": row.value_hash,
                    "created_at": row.created_at.isoformat(),
                }
                for row in entities
            ]
        with Session(self.vault) as session:
            secrets = session.scalars(
                select(VaultSecret).order_by(VaultSecret.created_at, VaultSecret.surrogate)
            ).all()
            logs = session.scalars(select(VaultAccessLog).order_by(VaultAccessLog.id)).all()
            vault_rows = [
                {
                    "surrogate": row.surrogate,
                    "ciphertext_b64": base64.b64encode(bytes(row.ciphertext)).decode("ascii"),
                    "nonce_hex": bytes(row.nonce).hex(),
                    "created_at": row.created_at.isoformat(),
                }
                for row in secrets
            ]
            log_rows = [
                {"surrogate": row.surrogate, "purpose": row.purpose, "at": row.at.isoformat()}
                for row in logs
            ]
        return {
            "analysis": {"entity": analysis_rows},
            "vault": {"vault": vault_rows, "vault_access_log": log_rows},
        }
