"""Fixed-point integer-cent monetary type and exact parsing/formatting helpers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import ClassVar

_PARENS_RE = re.compile(r"^\((.+)\)$")
_NUMERIC_RE = re.compile(r"^(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$")
_MAX_POSITIVE_EXPONENT_SHIFT = 1000


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """Immutable fixed-point USD monetary value stored as exact integer cents."""

    cents: int

    ZERO: ClassVar[Money]

    def __post_init__(self) -> None:
        if isinstance(self.cents, bool) or not isinstance(self.cents, int):
            raise TypeError(f"Money.cents must be int, got {type(self.cents).__name__}")

    @classmethod
    def from_cents(cls, value: int | str) -> Money:
        """Construct Money directly from an integer-cents value or integer string."""
        if isinstance(value, bool):
            raise TypeError("Money.from_cents does not accept bool")
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("Empty integer-cents value")
            try:
                return cls(int(text))
            except ValueError as exc:
                raise ValueError(f"Invalid integer-cents value: {value!r}") from exc
        raise TypeError(f"Money.from_cents expected int or str, got {type(value).__name__}")

    @classmethod
    def parse(
        cls,
        value: Money | int | float | str | None,
        default: Money | None = None,
    ) -> Money:
        """Parse a numeric or currency string into fixed-point integer cents (half-up)."""
        if value is None:
            if default is not None:
                return default
            raise ValueError("Cannot parse None into Money without default")

        if isinstance(value, cls):
            return value

        if isinstance(value, bool):
            raise TypeError("Cannot parse bool into Money")

        if isinstance(value, int):
            return cls(value * 100)

        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise ValueError(f"Invalid monetary value: {value!r}")
            return cls._parse_str(repr(value), raw_value=value, default=default)

        if isinstance(value, str):
            return cls._parse_str(value, raw_value=value, default=default)

        raise TypeError(f"Unsupported monetary type: {type(value).__name__}")

    @classmethod
    def _parse_str(
        cls,
        raw_text: str,
        *,
        raw_value: object,
        default: Money | None,
    ) -> Money:
        text = raw_text.strip()
        if not text or text in {"-", "--", "N/A", "n/a"}:
            if default is not None:
                return default
            raise ValueError(f"Empty monetary value: {raw_value!r}")

        negative = False
        parens_match = _PARENS_RE.match(text)
        if parens_match:
            negative = True
            text = parens_match.group(1).strip()

        text = text.replace("$", "").replace(",", "").replace(" ", "")
        if not text or text in {"-", "--", "N/A", "n/a"}:
            if default is not None:
                return default
            raise ValueError(f"Empty monetary value: {raw_value!r}")

        if text.startswith("-"):
            negative = not negative
            text = text[1:]
        elif text.startswith("+"):
            text = text[1:]

        match = _NUMERIC_RE.match(text)
        if not match:
            raise ValueError(f"Invalid monetary value: {raw_value!r}")

        int_part, frac_after_dot, frac_only, exp_str = match.groups()
        frac_part = frac_after_dot if frac_after_dot is not None else (frac_only or "")
        digits_raw = (int_part or "") + frac_part
        stripped_digits = digits_raw.lstrip("0")
        if not stripped_digits:
            return cls(0)

        exp = int(exp_str) if exp_str is not None else 0
        shift = exp - len(frac_part) + 2

        digits_int = int(stripped_digits)
        if shift >= 0:
            if shift > _MAX_POSITIVE_EXPONENT_SHIFT:
                raise ValueError(f"Monetary exponent out of range: {raw_value!r}")
            abs_cents = digits_int * (10**shift)
        else:
            neg_shift = -shift
            if neg_shift > len(stripped_digits):
                abs_cents = 0
            else:
                divisor = 10**neg_shift
                quot, rem = divmod(digits_int, divisor)
                if rem * 2 >= divisor:
                    quot += 1
                abs_cents = quot

        if abs_cents == 0:
            return cls(0)
        return cls(-abs_cents if negative else abs_cents)

    def mul_ratio(self, numerator: int, denominator: int) -> Money:
        """Scale by rational numerator / denominator using exact half-up rounding."""
        if (
            isinstance(numerator, bool)
            or isinstance(denominator, bool)
            or not isinstance(numerator, int)
            or not isinstance(denominator, int)
        ):
            raise TypeError("mul_ratio numerator and denominator must be ints")
        if denominator == 0:
            raise ZeroDivisionError("mul_ratio denominator cannot be zero")

        if self.cents == 0 or numerator == 0:
            return ZERO

        negative = (self.cents < 0) ^ (numerator < 0) ^ (denominator < 0)
        num = abs(self.cents) * abs(numerator)
        den = abs(denominator)
        quot, rem = divmod(num, den)
        if rem * 2 >= den:
            quot += 1
        if quot == 0:
            return ZERO
        return Money(-quot if negative else quot)

    def format(self) -> str:
        """Format cleanly as a fixed 2-decimal string (e.g. '-1234.56')."""
        sign = "-" if self.cents < 0 else ""
        whole, frac = divmod(abs(self.cents), 100)
        return f"{sign}{whole}.{frac:02d}"

    def format_comma(self) -> str:
        """Format as a fixed 2-decimal string with thousands separators."""
        sign = "-" if self.cents < 0 else ""
        whole, frac = divmod(abs(self.cents), 100)
        return f"{sign}{whole:,}.{frac:02d}"

    def __str__(self) -> str:
        return self.format()

    def __bool__(self) -> bool:
        return self.cents != 0

    def __neg__(self) -> Money:
        return ZERO if self.cents == 0 else Money(-self.cents)

    def __pos__(self) -> Money:
        return self

    def __abs__(self) -> Money:
        return self if self.cents >= 0 else Money(-self.cents)

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.cents + other.cents)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.cents - other.cents)


ZERO = Money(0)
Money.ZERO = ZERO
parse_money = Money.parse


def format_money(value: Money) -> str:
    """Format a Money value cleanly as a 2-decimal string."""
    return value.format()
