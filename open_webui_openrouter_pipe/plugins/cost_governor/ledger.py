"""Append-only cost ledger on SQLAlchemy Core; portable across SQLite, PostgreSQL and MariaDB."""

from __future__ import annotations

import threading
from typing import Any

LEDGER_TABLE_NAME = "openrouter_cost_ledger"

_LEDGER_WIDTHS = {
    "request_id": 64,
    "user_id": 64,
    "user_name": 128,
    "user_email": 255,
    "user_groups": 255,
    "pipe_id": 64,
    "kind": 12,
    "model_id": 128,
    "requested_model": 128,
    "status": 12,
    "chat_id": 64,
    "message_id": 64,
}

LEDGER_ROW_FIELDS = (
    "ts",
    "request_id",
    "user_id",
    "user_name",
    "user_email",
    "user_groups",
    "pipe_id",
    "kind",
    "model_id",
    "requested_model",
    "status",
    "degraded",
    "tokens_in",
    "tokens_out",
    "tokens_reasoning",
    "tokens_cached",
    "cost",
    "chat_id",
    "message_id",
)


def _ledger_table(metadata: Any, name: str, schema: str | None) -> Any:
    from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, Table

    w = _LEDGER_WIDTHS
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("ts", BigInteger, nullable=False),
        Column("request_id", String(w["request_id"]), nullable=False, unique=True),
        Column("user_id", String(w["user_id"]), nullable=False),
        Column("user_name", String(w["user_name"])),
        Column("user_email", String(w["user_email"])),
        Column("user_groups", String(w["user_groups"])),
        Column("pipe_id", String(w["pipe_id"])),
        Column("kind", String(w["kind"])),
        Column("model_id", String(w["model_id"])),
        Column("requested_model", String(w["requested_model"])),
        Column("status", String(w["status"])),
        Column("degraded", Integer, nullable=False, default=0),
        Column("tokens_in", Integer),
        Column("tokens_out", Integer),
        Column("tokens_reasoning", Integer),
        Column("tokens_cached", Integer),
        Column("cost", Float, nullable=False, default=0.0),
        Column("chat_id", String(w["chat_id"])),
        Column("message_id", String(w["message_id"])),
        Index(f"ix_{name}_user_ts", "user_id", "ts"),
        Index(f"ix_{name}_ts", "ts"),
        schema=schema,
    )


class CostLedger:
    """Synchronous ledger; call from a worker thread, never from the event loop."""

    def __init__(self, engine: Any, *, schema: str | None = None, table_name: str = LEDGER_TABLE_NAME) -> None:
        from sqlalchemy import MetaData

        self.engine = engine
        self._metadata = MetaData()
        self.table = _ledger_table(self._metadata, table_name, schema)
        self._ready = False
        self._lock = threading.Lock()

    def ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                self._metadata.create_all(self.engine, checkfirst=True)
                self._ready = True

    @staticmethod
    def _fit(row: dict[str, Any]) -> dict[str, Any]:
        out = {key: row.get(key) for key in LEDGER_ROW_FIELDS}
        for key, width in _LEDGER_WIDTHS.items():
            value = out.get(key)
            if value is not None:
                out[key] = str(value)[:width]
        out["cost"] = float(out.get("cost") or 0.0)
        out["degraded"] = 1 if out.get("degraded") else 0
        out["ts"] = int(out.get("ts") or 0)
        return out

    def record(self, row: dict[str, Any]) -> bool:
        """Insert one row; ``False`` when its request id is already recorded."""
        from sqlalchemy.exc import IntegrityError

        self.ensure()
        values = self._fit(row)
        try:
            with self.engine.begin() as conn:
                conn.execute(self.table.insert().values(**values))
        except IntegrityError:
            return False
        return True

    def spend(self, user_id: str, start: int, end: int) -> float:
        from sqlalchemy import func, select

        self.ensure()
        t = self.table
        stmt = select(func.coalesce(func.sum(t.c.cost), 0.0)).where(
            t.c.user_id == user_id, t.c.ts >= start, t.c.ts < end
        )
        with self.engine.connect() as conn:
            return float(conn.execute(stmt).scalar() or 0.0)

    def spend_by_user(self, start: int, end: int) -> list[dict[str, Any]]:
        from sqlalchemy import func, select

        self.ensure()
        t = self.table
        stmt = (
            select(
                t.c.user_id,
                func.max(t.c.user_name).label("user_name"),
                func.max(t.c.user_email).label("user_email"),
                func.count().label("requests"),
                func.sum(t.c.tokens_in).label("tokens_in"),
                func.sum(t.c.tokens_out).label("tokens_out"),
                func.sum(t.c.cost).label("cost"),
            )
            .where(t.c.ts >= start, t.c.ts < end)
            .group_by(t.c.user_id)
            .order_by(func.sum(t.c.cost).desc())
        )
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    def rows(self, start: int, end: int, user_id: str | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import select

        self.ensure()
        t = self.table
        stmt = select(*[t.c[name] for name in LEDGER_ROW_FIELDS]).where(t.c.ts >= start, t.c.ts < end)
        if user_id:
            stmt = stmt.where(t.c.user_id == user_id)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt.order_by(t.c.ts))]
