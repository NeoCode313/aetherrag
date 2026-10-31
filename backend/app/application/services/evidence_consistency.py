"""Signed numeric bounds and explicit unit conversions; not semantic entailment."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

NUMBER = r"(?<![\w.+-])[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?(?!\w|[.,]\d)"
UNIT = r"(?:bar|kpa|mpa|pa|mm|cm|mw|kw|nm|rpm|hz|°c|°f|%|m)"
NUMBER_RE = re.compile(NUMBER)
QUANTITY_RE = re.compile(r"(?<![\w.+-])([+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s*(" + UNIT + r")(?=\W|$)", re.I)
COMPARATORS = [
    ("le", r"<=|≤|\b(?:no (?:debe(?:n)? )?(?:superar|exceder)|no (?:es |sea |ser )?mayor (?:a|que)|como maximo|a lo sumo|at most|no more than|up to|maxim[oa](?: de)?)\s*$"),
    ("ge", r">=|≥|\b(?:no (?:debe(?:n)? )?(?:ser )?(?:inferior|menor) (?:a|que)|al menos|at least|no less than|minim[oa](?: de)?)\s*$"),
    ("lt", r"(?<![<>=])<(?![=>])|\b(?:inferior a|menor (?:a|que)|por debajo de|below|less than|under)\s*$"),
    ("gt", r"(?<![<>=])>(?![=>])|\b(?:superior a|mayor (?:a|que)|por encima de|above|greater than|over|superar|exceder)\s*$"),
    ("approx", r"≈|~|\b(?:aproximadamente|aprox\.?|around|approximately)\s*$"),
]
UNITS = {
    "bar": ("pressure", Decimal(100000)), "kpa": ("pressure", Decimal(1000)),
    "mpa": ("pressure", Decimal(1000000)), "pa": ("pressure", Decimal(1)),
    "mm": ("length", Decimal(".001")), "cm": ("length", Decimal(".01")), "m": ("length", Decimal(1)),
    "kw": ("power", Decimal(1000)), "mw": ("power", Decimal(1000000)),
    "nm": ("torque", Decimal(1)), "rpm": ("speed", Decimal(1)), "hz": ("frequency", Decimal(1)),
    "%": ("percent", Decimal(1)), "°c": ("temperature", Decimal(1)), "°f": ("temperature", Decimal(1)),
}


def normalized(text: str) -> str:
    text = text.replace("−", "-").replace("–", "-").replace("＋", "+")
    return "".join(c for c in unicodedata.normalize("NFD", text.lower())
                   if unicodedata.category(c) != "Mn")


def number(value: str) -> Decimal:
    try:
        result = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric literal") from exc
    if not result.is_finite() or abs(result.adjusted()) > 308:
        raise ValueError("Numeric magnitude outside supported range")
    return result


def numbers(text: str) -> set[Decimal]:
    return {number(match.group()) for match in NUMBER_RE.finditer(normalized(text))}


def comparator(prefix: str) -> str:
    # Only the local clause can qualify a magnitude.
    clause = re.split(r"[;|\n.!?]", prefix)[-1][-100:].strip()
    matches = []
    for priority, (kind, pattern) in enumerate(COMPARATORS):
        for match in re.finditer(pattern, clause):
            if not clause[match.end():].strip():
                matches.append((match.start(), -priority, kind))
    return min(matches, key=lambda item: -item[1])[2] if matches else "eq"


@dataclass(frozen=True)
class Quantity:
    literal: Decimal
    dimension: str
    value: Decimal
    relation: str


def quantities(text: str) -> list[Quantity]:
    text = normalized(text)
    result = []
    for match in QUANTITY_RE.finditer(text):
        literal, unit = number(match.group(1)), match.group(2)
        dimension, scale = UNITS[unit]
        value = (literal - 32) * Decimal(5) / Decimal(9) if unit == "°f" else literal * scale
        result.append(Quantity(literal, dimension, value, comparator(text[:match.start()])))
    return result


def close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(a), abs(b), Decimal(1)) * Decimal("0.00000001")


def consistent(statement: str, quotes: list[str]) -> bool:
    """Require the same signed value, physical dimension AND bound direction.

    Explicit equivalent units are allowed. Unknown sentence semantics remain
    unverified and are never reported as a faithfulness probability.
    """
    try:
        claimed = quantities(statement)
        source = [q for quote in quotes for q in quantities(quote)]
        if any(not any(q.dimension == s.dimension and q.relation == s.relation and close(q.value, s.value)
                       for s in source) for q in claimed):
            return False
        if not numbers(statement).issubset(set().union(*(numbers(q) for q in quotes)) | {q.literal for q in claimed}):
            return False
        source_bounds = [(number(m.group()), comparator(normalized(q)[:m.start()]))
                         for q in quotes for m in NUMBER_RE.finditer(normalized(q))]
        for match in NUMBER_RE.finditer(normalized(statement)):
            relation = comparator(normalized(statement)[:match.start()])
            if relation != "eq" and not any(close(number(match.group()), n) and relation == bound
                                           for n, bound in source_bounds):
                if not any(close(number(match.group()), q.literal) and q.relation == relation for q in claimed):
                    return False
        return True
    except (ValueError, InvalidOperation, OverflowError):
        return False
