"""Line-item quantity parsing (USR-002-01 AC3).

Invoices and TIGs print quantities with a unit: ``"43 db"``, ``"4 alkalom"``,
``"38 óra"``, ``"1 200 db"``, ``"2,5 óra"``. ``parse_quantity`` strips a trailing unit
and reads the number with the shared amount rules (``normalize_amount``), so separators
behave exactly as for money: ``"1 200"`` is 1200, ``"2,5"`` is 2.5, and a lone separator
before three digits (``"1.200"``) is ambiguous and returns ``None`` -- never guessed.

Anything else (leading text such as ``"kb. 40 óra"``, ranges, sums, no number at all)
returns ``None`` as well.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from intake.normalization.amounts import normalize_amount

# A number (digits with ., , or whitespace grouping), then an optional unit that starts
# with a letter and contains no further digits ("db", "óra", "m²", "db.").
_QUANTITY: Final = re.compile(
    r"(?P<sign>[-−])?\s*(?P<number>\d(?:[\d.,\s]*\d)?)\s*(?P<unit>[^\W\d_][^\d]*)?"
)


def parse_quantity(raw: str | None) -> Decimal | None:
    """Parse a printed quantity such as ``"1 200 db"``; ``None`` when not reliably readable."""
    if raw is None:
        return None
    match = _QUANTITY.fullmatch(raw.strip())
    if match is None:
        return None
    value = normalize_amount(match["number"])
    if not isinstance(value, Decimal):
        return None
    return -value if match["sign"] and value else value


__all__ = ["parse_quantity"]
