"""Unit tests for the fixed-point Money value object."""

from __future__ import annotations

from typing import Any

import pytest

from ledgerdemain.money import ZERO, Money, format_money, parse_money


def test_money_construction_and_from_cents() -> None:
    assert Money(0) == ZERO
    assert Money(1234).cents == 1234
    assert Money.from_cents(-505) == Money(-505)
    assert Money.from_cents("  4200 ") == Money(4200)
    assert Money.from_cents("-0") == ZERO

    bad_float: Any = 12.34
    bad_bool: Any = True
    with pytest.raises(TypeError):
        Money(True)
    with pytest.raises(TypeError):
        Money(bad_float)
    with pytest.raises(TypeError):
        Money.from_cents(bad_bool)
    with pytest.raises(TypeError):
        Money.from_cents(bad_float)

    for bad_cents_str in ("", "  ", "12.34", "abc"):
        with pytest.raises(ValueError):
            Money.from_cents(bad_cents_str)


def test_money_parse_valid_formats_and_defaults() -> None:
    assert parse_money(None, default=ZERO) == ZERO
    assert parse_money(Money(250)) == Money(250)
    assert parse_money(5) == Money(500)
    assert parse_money(-12) == Money(-1200)
    assert parse_money(0.1 + 0.2) == Money(30)
    assert parse_money(-0.0) == ZERO

    assert parse_money("$1,234.56") == Money(123456)
    assert parse_money("($1,234.56)") == Money(-123456)
    assert parse_money("(-50.00)") == Money(5000)
    assert parse_money("-0.00") == ZERO
    assert parse_money(".5") == Money(50)
    assert parse_money("12.") == Money(1200)
    assert parse_money("2.5e3") == Money(250000)
    assert parse_money("1e-5") == ZERO
    assert parse_money("N/A", default=ZERO) == ZERO
    assert parse_money("--", default=Money(100)) == Money(100)


def test_money_parse_half_up_rounding_boundaries() -> None:
    assert parse_money("0.005") == Money(1)
    assert parse_money("-0.005") == Money(-1)
    assert parse_money("0.004") == ZERO
    assert parse_money("-0.004") == ZERO
    assert parse_money("-0.001") == ZERO
    assert parse_money("1.235") == Money(124)
    assert parse_money("-1.235") == Money(-124)


def test_money_parse_invalid_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        parse_money(None)

    for bad in ("", "N/A", "abc", "--5.00", "1e5000", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            parse_money(bad)

    with pytest.raises(TypeError):
        parse_money(True)


def test_money_strict_type_safety_and_operators() -> None:
    a = Money(150)
    b = Money(50)
    assert a + b == Money(200)
    assert a - b == Money(100)
    assert +Money(-25) == Money(-25)
    assert -a == Money(-150)
    assert -ZERO == ZERO
    assert abs(Money(-25)) == Money(25)
    assert bool(a) is True
    assert bool(ZERO) is False

    bad_eq: Any = 150
    bad_int: Any = 1
    bad_float_op: Any = 1.5
    bad_cmp: Any = 100
    assert (a == bad_eq) is False
    with pytest.raises(TypeError):
        _ = a + bad_int
    with pytest.raises(TypeError):
        _ = a - bad_int
    with pytest.raises(TypeError):
        _ = bad_int + a
    with pytest.raises(TypeError):
        _ = a + bad_float_op
    with pytest.raises(TypeError):
        _ = a < bad_cmp


def test_money_mul_ratio_edge_cases_and_errors() -> None:
    assert Money(100000).mul_ratio(1, 1000) == Money(100)
    assert Money(100000).mul_ratio(4, 100) + Money(35) == Money(4035)
    assert Money(1).mul_ratio(1, 2) == Money(1)
    assert Money(-1).mul_ratio(1, 2) == Money(-1)
    assert Money(1).mul_ratio(1, -2) == Money(-1)
    assert Money(-1).mul_ratio(-1, 2) == Money(1)
    assert Money(1).mul_ratio(1, 3) == ZERO
    assert Money(500).mul_ratio(0, 7) == ZERO

    with pytest.raises(ZeroDivisionError):
        Money(100).mul_ratio(1, 0)
    with pytest.raises(TypeError):
        Money(100).mul_ratio(True, 2)


def test_money_formatting() -> None:
    assert format_money(ZERO) == "0.00"
    assert str(Money(5)) == "0.05"
    assert format_money(Money(-5)) == "-0.05"
