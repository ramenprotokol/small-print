from small_print.budget import BudgetLedger

from conftest import sqlite_sql

DAY = "2026-09-26"
NEXT = "2026-09-27"


def ledger():
    sql = sqlite_sql()
    led = BudgetLedger(sql)
    led.setup()
    return led, sql


def test_consume_counts_up_to_the_daily_limit_then_refuses():
    led, _ = ledger()
    results = [led.consume(DAY, 3, None, 99) for _ in range(5)]
    assert [r["allowed"] for r in results] == [True, True, True, False, False]
    assert [r["remaining"] for r in results] == [2, 1, 0, 0, 0]
    assert results[-1]["reason"] == "daily"
    assert led.status(DAY, 3)["used"] == 3


def test_zero_or_negative_limit_means_ai_is_off():
    led, _ = ledger()
    assert led.consume(DAY, 0, None, 5)["allowed"] is False
    assert led.consume(DAY, -1, None, 5)["allowed"] is False
    assert led.status(DAY, 0)["used"] == 0


def test_per_visitor_limit_applies_before_the_daily_limit():
    led, _ = ledger()
    a = [led.consume(DAY, 10, "203.0.113.7", 2) for _ in range(3)]
    assert [r["allowed"] for r in a] == [True, True, False]
    assert a[-1]["reason"] == "visitor"
    # Another visitor still has their own allowance, from the same daily pool.
    b = led.consume(DAY, 10, "198.51.100.4", 2)
    assert b["allowed"] and b["used"] == 3 and b["visitor_remaining"] == 1


def test_counter_resets_on_a_new_utc_day_and_old_rows_are_purged():
    led, sql = ledger()
    led.consume(DAY, 1, "203.0.113.7", 1)
    assert led.consume(DAY, 1, "203.0.113.7", 1)["allowed"] is False
    assert led.consume(NEXT, 1, "203.0.113.7", 1)["allowed"] is True
    assert sql("SELECT day FROM usage") == [{"day": NEXT}]
    assert {r["day"] for r in sql("SELECT day FROM visitor_usage")} == {NEXT}
    assert {r["day"] for r in sql("SELECT day FROM day_salt")} == {NEXT}


def test_raw_ip_addresses_are_never_stored():
    led, sql = ledger()
    ip = "203.0.113.7"
    led.consume(DAY, 5, ip, 5)
    dump = repr(sql("SELECT * FROM usage")) + repr(sql("SELECT * FROM visitor_usage")) + repr(sql("SELECT * FROM day_salt"))
    assert ip not in dump


def test_visitor_hash_uses_a_fresh_salt_each_day():
    led, sql = ledger()
    ip = "203.0.113.7"
    led.consume(DAY, 5, ip, 5)
    first = sql("SELECT visitor FROM visitor_usage")[0]["visitor"]
    led.consume(NEXT, 5, ip, 5)
    second = sql("SELECT visitor FROM visitor_usage")[0]["visitor"]
    assert first != second


def test_refund_gives_back_one_call_and_never_goes_negative():
    led, _ = ledger()
    led.consume(DAY, 2, "203.0.113.7", 1)
    assert led.consume(DAY, 2, "203.0.113.7", 1)["allowed"] is False  # visitor cap hit
    led.refund(DAY, "203.0.113.7")
    assert led.status(DAY, 2)["used"] == 0
    assert led.consume(DAY, 2, "203.0.113.7", 1)["allowed"] is True
    led.refund(DAY, None)
    led.refund(DAY, None)
    assert led.status(DAY, 2)["used"] == 0


def test_conditional_increment_cannot_pass_the_limit():
    """Even if two calls raced past the read (which a Durable Object prevents),
    the UPDATE ... WHERE used < limit guard stops the counter at the limit."""
    led, sql = ledger()
    sql("INSERT INTO usage (day, used) VALUES (?, ?)", DAY, 4)
    rows = sql(
        "INSERT INTO usage (day, used) VALUES (?, 1) "
        "ON CONFLICT (day) DO UPDATE SET used = used + 1 WHERE used < ? RETURNING used",
        DAY, 4,
    )
    assert rows == []
    assert led.status(DAY, 4)["used"] == 4


def test_status_reports_remaining_for_the_visitor():
    led, _ = ledger()
    led.consume(DAY, 40, "203.0.113.7", 8)
    s = led.status(DAY, 40, "203.0.113.7", 8)
    assert s == {"allowed": True, "reason": None, "used": 1, "limit": 40, "remaining": 39, "visitor_remaining": 7}
