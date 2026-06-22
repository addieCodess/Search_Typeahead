"""
Primary data store setup.

Design choice: SQLite is the single source of truth for query counts.
The Trie (in app/core/trie.py) is an in-memory index built FROM this table
at startup, and updated incrementally as batches flush. The cache layer
(app/core/cache.py) sits in front of Trie lookups, not in front of SQLite
directly -- SQLite is never touched on the read path.
"""
from sqlalchemy import create_engine, Column, String, Integer, MetaData, Table
from sqlalchemy.orm import sessionmaker

import os
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./typeahead.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
metadata = MetaData()

query_counts = Table(
    "query_counts",
    metadata,
    Column("query", String, primary_key=True),
    Column("count", Integer, nullable=False, default=0),
)


def init_db():
    metadata.create_all(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
