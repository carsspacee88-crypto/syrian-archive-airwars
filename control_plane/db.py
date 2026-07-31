from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def engine():
    url = get_settings().resolved_database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=engine(), expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine())


def get_session() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


def reset_database_caches() -> None:
    if engine.cache_info().currsize:
        engine().dispose()
    session_factory.cache_clear()
    engine.cache_clear()
