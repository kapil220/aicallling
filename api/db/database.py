import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_async_engine(DATABASE_URL, echo=True)
# async_sessionmaker, not sessionmaker: the latter builds a sync Session, which
# raises "'Session' object does not support the asynchronous context manager
# protocol" for every `async with async_session() as s` caller.
async_session = async_sessionmaker(engine, expire_on_commit=False)
