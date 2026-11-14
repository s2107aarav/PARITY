"""Small transactional document store. Each service owns a separate database.

Logical tables are namespaces; JSON documents retain an inspectable business ledger.
Writes serialize per database, deliberately favoring correctness over throughput.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, delete, select, text
from sqlalchemy.engine import URL, make_url

metadata = MetaData()
records = Table(
    "records",
    metadata,
    Column("bucket", String(80), primary_key=True),
    Column("key", String(240), primary_key=True),
    Column("payload", Text, nullable=False),
)


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    def get(self, bucket: str, key: str, default=None) -> Any:
        value = self.connection.execute(
            select(records.c.payload).where(records.c.bucket == bucket, records.c.key == str(key))
        ).scalar_one_or_none()
        return json.loads(value) if value is not None else default

    def put(self, bucket: str, key: str, value: Any) -> None:
        self.connection.execute(
            delete(records).where(records.c.bucket == bucket, records.c.key == str(key))
        )
        self.connection.execute(
            records.insert().values(
                bucket=bucket, key=str(key), payload=json.dumps(value, sort_keys=True)
            )
        )

    def all(self, bucket: str) -> list:
        rows = self.connection.execute(select(records.c.payload).where(records.c.bucket == bucket))
        return [json.loads(row[0]) for row in rows]

    def next_id(self, name: str) -> int:
        value = self.get("counters", name, 0) + 1
        self.put("counters", name, value)
        return value


class Store:
    def __init__(self, url: str):
        if "://" not in url:
            Path(url).parent.mkdir(parents=True, exist_ok=True)
            engine_url = URL.create("sqlite", database=str(Path(url).resolve()))
        else:
            engine_url = make_url(url)
        self.url = str(engine_url)
        self.engine = create_engine(
            engine_url,
            connect_args={"timeout": 30, "check_same_thread": False}
            if engine_url.get_backend_name() == "sqlite"
            else {},
            pool_pre_ping=True,
        )
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
                conn.execute(text("SELECT pg_advisory_xact_lock(734271)"))
            try:
                yield Transaction(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def snapshot(self) -> dict:
        with self.engine.connect() as conn:
            result: dict[str, dict] = {}
            for bucket, key, payload in conn.execute(select(records)):
                result.setdefault(str(bucket), {})[str(key)] = json.loads(str(payload))
            return result

    def close(self):
        self.engine.dispose()
