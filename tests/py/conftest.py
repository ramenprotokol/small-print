"""Shared fakes. Nothing here touches the network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from small_print.budget import BudgetLedger

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"


def run(coro):
    return asyncio.run(coro)


def sqlite_sql():
    """An sql(query, *params) -> [row dict] function over in-memory SQLite,
    mirroring the Durable Object's ctx.storage.sql.exec."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row

    def sql(query, *params):
        return [dict(r) for r in conn.execute(query, params).fetchall()]

    return sql


class MemoryCache:
    def __init__(self):
        self.rows: dict[tuple[str, str], str] = {}
        self.gets = 0
        self.puts = 0

    async def get(self, sha, pipeline):
        self.gets += 1
        raw = self.rows.get((sha, pipeline))
        return None if raw is None else json.loads(raw)

    async def put(self, sha, pipeline, analysis):
        self.puts += 1
        self.rows[(sha, pipeline)] = json.dumps(analysis)


class LedgerBudget:
    """The real BudgetLedger over SQLite, behind the async interface."""

    def __init__(self):
        self.ledger = BudgetLedger(sqlite_sql())
        self.ledger.setup()
        self.refunds = 0

    async def status(self, day, limit, client, per_visitor):
        return self.ledger.status(day, limit, client, per_visitor)

    async def consume(self, day, limit, client, per_visitor):
        return self.ledger.consume(day, limit, client, per_visitor)

    async def refund(self, day, client):
        self.refunds += 1
        self.ledger.refund(day, client)


class ScriptedReader:
    """Stands in for the model. ``respond(params) -> dict | Exception``."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[dict] = []

    async def read(self, params):
        self.calls.append(params)
        out = self.respond(params)
        if isinstance(out, Exception):
            raise out
        return {"data": out, "model": params["model"]}


def demo_text(slug: str) -> str:
    return (DEMO / f"{slug}.txt").read_text(encoding="utf-8")


def demo_analysis(slug: str) -> dict:
    return json.loads((DEMO / f"{slug}.analysis.json").read_text(encoding="utf-8"))


@pytest.fixture
def memory_cache():
    return MemoryCache()


@pytest.fixture
def ledger_budget():
    return LedgerBudget()
