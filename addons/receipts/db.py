from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from addons.receipts.models import ReceiptsBase


def make_engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(database_url: str):
    engine = make_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def init_receipt_tables(engine) -> None:
    ReceiptsBase.metadata.create_all(bind=engine)


def session_scope(SessionLocal: sessionmaker) -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
