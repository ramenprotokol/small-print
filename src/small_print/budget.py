"""The daily AI budget ledger.

Runs inside a SQLite-backed Durable Object in production (one global
instance), and against Python's ``sqlite3`` in tests: the SQL is the same.

Atomicity comes from the Durable Object: it handles one call at a time and
nothing in ``consume`` awaits, so the read-check-write below cannot
interleave with another request.

Two limits apply per UTC day:

* ``limit``: model calls across all visitors;
* ``per_visitor``: model calls per visitor. A visitor is an HMAC of the
  client IP under a random salt that is created per day and deleted with that
  day's rows, so raw IPs are never stored and hashes cannot be linked across
  days.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Callable

# sql(query, *params) -> list of row dicts
SqlFn = Callable[..., list]

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, used INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS visitor_usage ("
    " day TEXT NOT NULL, visitor TEXT NOT NULL, used INTEGER NOT NULL,"
    " PRIMARY KEY (day, visitor))",
    "CREATE TABLE IF NOT EXISTS day_salt (day TEXT PRIMARY KEY, salt TEXT NOT NULL)",
)


class BudgetLedger:
    def __init__(self, sql: SqlFn, *, token_hex: Callable[[int], str] = secrets.token_hex):
        self._sql = sql
        self._token_hex = token_hex

    def setup(self) -> None:
        for stmt in SCHEMA:
            self._sql(stmt)

    # -- helpers ---------------------------------------------------------

    def _purge_before(self, day: str) -> None:
        self._sql("DELETE FROM usage WHERE day < ?", day)
        self._sql("DELETE FROM visitor_usage WHERE day < ?", day)
        self._sql("DELETE FROM day_salt WHERE day < ?", day)

    def _salt(self, day: str) -> str:
        rows = self._sql("SELECT salt FROM day_salt WHERE day = ?", day)
        if rows:
            return rows[0]["salt"]
        salt = self._token_hex(16)
        self._sql("INSERT INTO day_salt (day, salt) VALUES (?, ?)", day, salt)
        return salt

    def _visitor(self, day: str, client: str | None) -> str | None:
        if not client:
            return None
        digest = hmac.new(self._salt(day).encode(), client.encode(), hashlib.sha256)
        return digest.hexdigest()[:32]

    def _used(self, day: str) -> int:
        rows = self._sql("SELECT used FROM usage WHERE day = ?", day)
        return int(rows[0]["used"]) if rows else 0

    def _visitor_used(self, day: str, visitor: str | None) -> int:
        if visitor is None:
            return 0
        rows = self._sql(
            "SELECT used FROM visitor_usage WHERE day = ? AND visitor = ?", day, visitor
        )
        return int(rows[0]["used"]) if rows else 0

    @staticmethod
    def _report(allowed: bool, reason: str | None, used: int, limit: int, vused: int, per_visitor: int) -> dict:
        limit = max(0, limit)
        return {
            "allowed": allowed,
            "reason": reason,
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "visitor_remaining": max(0, per_visitor - vused),
        }

    # -- public API ------------------------------------------------------

    def status(self, day: str, limit: int, client: str | None = None, per_visitor: int = 0) -> dict:
        self._purge_before(day)
        vused = self._visitor_used(day, self._visitor(day, client))
        return self._report(True, None, self._used(day), limit, vused, per_visitor)

    def consume(self, day: str, limit: int, client: str | None, per_visitor: int) -> dict:
        """Take one model call from today's budget, if both limits allow it."""
        self._purge_before(day)
        visitor = self._visitor(day, client)
        vused = self._visitor_used(day, visitor)
        used = self._used(day)
        if limit <= 0 or used >= limit:
            return self._report(False, "daily", used, limit, vused, per_visitor)
        if visitor is not None and vused >= per_visitor:
            return self._report(False, "visitor", used, limit, vused, per_visitor)
        # Conditional increment: never passes the limit, even if called
        # outside a Durable Object's one-at-a-time guarantee.
        rows = self._sql(
            "INSERT INTO usage (day, used) VALUES (?, 1) "
            "ON CONFLICT (day) DO UPDATE SET used = used + 1 WHERE used < ? "
            "RETURNING used",
            day,
            limit,
        )
        if not rows:
            return self._report(False, "daily", self._used(day), limit, vused, per_visitor)
        if visitor is not None:
            self._sql(
                "INSERT INTO visitor_usage (day, visitor, used) VALUES (?, ?, 1) "
                "ON CONFLICT (day, visitor) DO UPDATE SET used = used + 1",
                day,
                visitor,
            )
            vused += 1
        return self._report(True, None, int(rows[0]["used"]), limit, vused, per_visitor)

    def refund(self, day: str, client: str | None) -> None:
        """Give back one call (the model call failed before it was billed)."""
        self._sql("UPDATE usage SET used = used - 1 WHERE day = ? AND used > 0", day)
        visitor = self._visitor(day, client)
        if visitor is not None:
            self._sql(
                "UPDATE visitor_usage SET used = used - 1 WHERE day = ? AND visitor = ? AND used > 0",
                day,
                visitor,
            )
