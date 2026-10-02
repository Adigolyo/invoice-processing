"""Monetary amount normalisation (USR-002-03).

``normalize_amount`` turns one extracted amount string into an exact ``Decimal`` that
keeps the source precision (``"1.234,50"`` -> ``Decimal("1234.50")``). No rounding
happens here; the per-currency rounding rule belongs to the booking step (USR-004-02).

Supported notations (the separator set is ``.``, ``,`` and whitespace, including
non-breaking, narrow non-breaking and thin spaces):

- **US**: comma thousands, dot decimal: ``1,234.56``, ``1,234,567``.
- **Hungarian, dot**: dot thousands, comma decimal: ``1.234,56``, ``1.234.567``.
- **Hungarian, space**: space thousands, comma decimal: ``1 234,56``, ``1 234``.
- **No thousands separator**: ``1234,56``, ``1234.56``, ``1234``.
- Space thousands with a dot decimal (``1 234.56``) is accepted too: whitespace is
  never a decimal separator, so the dot can only be one.

Classification rules: when two separator kinds appear, the last one is the decimal
separator and must occur once; the other is the thousands separator. Thousands groups
must be well formed (a leading group of 1-3 digits not starting with ``0``, then groups
of exactly 3). A single ``,`` or ``.`` followed by anything other than exactly three
digits is a decimal separator. A single ``,`` or ``.`` followed by exactly three digits
after a 1-3 digit leading group (``1.234``, ``12,345``) reads equally well as a
thousands separator (US or Hungarian-dot) and as a decimal separator, so it is
**flagged as ambiguous**, never guessed (AC5).

Also accepted, because each has exactly one reading:

- the Hungarian whole-amount marker ``,-`` (``12 500,-`` = 12500), which must use the
  amount's decimal separator;
- one currency symbol or code (up to three letters/currency signs, optional trailing
  dot) before or after the number: ``€1,234.56``, ``1 234,56 Ft``, ``USD 12.50``. The
  currency itself is normalised separately (``currency.to_iso_code``);
- a leading minus sign (``-`` or U+2212), before or after a leading currency symbol.

Anything else (accounting parentheses, trailing minus, exponents, other grouping
styles, free text) returns a ``NormalizationFailure``.

``format_hungarian_amount`` renders a value in Hungarian locale notation (comma decimal,
non-breaking-space thousands separator) for human-facing text. The ledger itself
receives the ``Decimal``: ``LedgerRow`` writes numbers, and the sheet's own (Hungarian)
locale controls how they are displayed.

``round_for_ledger`` (USR-004-02) applies the per-currency precision rule at the booking
step, after currency ISO resolution (USR-002-05) and this module's normalisation:

- **HUF** is booked as a whole number: rounded to the nearest integer, with an exact
  ``.5`` rounding away from zero (``999.50`` -> ``1000``, ``-0.5`` -> ``-1``; Decimal's
  ``ROUND_HALF_UP``, not banker's rounding). The result has no decimal places.
- **Every other currency** passes through unchanged, keeping its source precision
  (``EUR 12.345`` stays ``12.345``; nothing is rounded to the currency's minor unit).
- **No resolved currency** (missing, blank, or not an upper-case ISO 4217 code as
  ``to_iso_code`` returns it) is a ``NormalizationFailure``: no rounding decision is
  made, rather than defaulting to either convention.

``round_amounts_for_ledger`` applies the same rule independently to Net and Gross.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from babel.numbers import is_currency

from intake.models import InvoiceExtraction
from intake.normalization.result import NormalizationFailure

AMOUNT_FIELDS: Final = ("net", "gross")

HU_DECIMAL_SEPARATOR: Final = ","
HU_THOUSANDS_SEPARATOR: Final = " "  # CLDR "hu": no-break space

_MINUS_SIGNS: Final = "-−"
_CURRENCY = r"[^\s\d.,\-−–+()]{1,3}\.?"
_AMOUNT = re.compile(
    rf"(?P<pre>{_CURRENCY})?\s*"
    rf"(?P<sign>[{_MINUS_SIGNS}])?\s*"
    rf"(?P<pre2>{_CURRENCY})?\s*"
    r"(?P<body>[0-9](?:[0-9.,\s]*[0-9])?)"
    r"(?P<whole>[.,][-–])?\s*"
    rf"(?P<post>{_CURRENCY})?"
)
_SEPARATOR = re.compile(r"([.,]|\s+)")
_SPACE = " "


def _is_currency_affix(text: str) -> bool:
    core = text.removesuffix(".")
    return bool(core) and all(ch.isalpha() or unicodedata.category(ch) == "Sc" for ch in core)


def _valid_grouping(groups: list[str]) -> bool:
    """Thousands grouping: leading group 1-3 digits (no leading 0), then groups of 3."""
    head, *tail = groups
    return (
        1 <= len(head) <= 3
        and not head.startswith("0")
        and bool(tail)
        and all(len(group) == 3 for group in tail)
    )


def _split(body: str) -> tuple[list[str], list[str]]:
    parts = _SEPARATOR.split(body)
    groups = parts[0::2]
    separators = [_SPACE if sep.isspace() else sep for sep in parts[1::2]]
    return groups, separators


def _parse_body(raw: str, body: str, whole_marker: str | None) -> Decimal | NormalizationFailure:
    """Classify the separators of a sign- and currency-free numeric body."""
    groups, separators = _split(body)
    if any(not group for group in groups):
        return NormalizationFailure(f"malformed separators in amount {raw!r}")

    if whole_marker is not None:
        # "12 500,-": the marker is the decimal separator, with no fraction digits.
        decimal_sep: str | None = whole_marker
        integer_groups, fraction = groups, ""
    elif not separators:
        return Decimal(groups[0])
    else:
        kinds = set(separators)
        last = separators[-1]
        if len(kinds) > 2:
            return NormalizationFailure(f"too many separator kinds in amount {raw!r}")
        if len(kinds) == 2:
            if last == _SPACE or separators.count(last) != 1:
                return NormalizationFailure(f"no valid decimal separator in amount {raw!r}")
            decimal_sep, integer_groups, fraction = last, groups[:-1], groups[-1]
        elif last == _SPACE:
            decimal_sep, integer_groups, fraction = None, groups, ""
        elif len(separators) > 1:
            # "1,234,567" / "1.234.567": repeated, so it can only be thousands grouping.
            decimal_sep, integer_groups, fraction = None, groups, ""
        else:
            head, tail = groups
            if len(tail) == 3 and _valid_grouping(groups):
                return NormalizationFailure(
                    f"amount {raw!r} is ambiguous: {last!r} could be a thousands "
                    "or a decimal separator"
                )
            decimal_sep, integer_groups, fraction = last, [head], tail

    thousands = {sep for sep in separators if sep != decimal_sep}
    if decimal_sep is not None and decimal_sep in separators[: len(integer_groups) - 1]:
        return NormalizationFailure(f"decimal separator repeated in amount {raw!r}")
    if len(integer_groups) > 1 and (len(thousands) != 1 or not _valid_grouping(integer_groups)):
        return NormalizationFailure(f"invalid thousands grouping in amount {raw!r}")

    digits = "".join(integer_groups)
    return Decimal(f"{digits}.{fraction}" if fraction else digits)


def normalize_amount(raw: str | None) -> Decimal | NormalizationFailure:
    """Parse one extracted amount into an exact ``Decimal``; flag, never guess."""
    if raw is None or not raw.strip():
        return NormalizationFailure("amount is missing")
    match = _AMOUNT.fullmatch(raw.strip())
    if match is None:
        return NormalizationFailure(f"unrecognised amount format {raw!r}")

    affixes = [a for a in (match["pre"], match["pre2"], match["post"]) if a is not None]
    if len(affixes) > 1 or not all(_is_currency_affix(a) for a in affixes):
        return NormalizationFailure(f"unexpected text around amount {raw!r}")

    whole = match["whole"]
    result = _parse_body(raw, match["body"], whole[0] if whole else None)
    if isinstance(result, NormalizationFailure):
        return result
    if match["sign"] and result:
        return -result
    return result.copy_abs()  # "-0,00" is plain zero


def format_hungarian_amount(amount: Decimal) -> str:
    """Render ``amount`` in Hungarian notation (``1 234,56``) without changing precision."""
    if not amount.is_finite():
        raise ValueError(f"can only format a finite amount, got {amount}")
    sign = "-" if amount < 0 else ""
    integer, _, fraction = format(abs(amount), "f").partition(".")
    groups: list[str] = []
    while len(integer) > 3:
        integer, group = integer[:-3], integer[-3:]
        groups.insert(0, group)
    text = HU_THOUSANDS_SEPARATOR.join([integer, *groups])
    return f"{sign}{text}{HU_DECIMAL_SEPARATOR}{fraction}" if fraction else f"{sign}{text}"


type AmountValue = Decimal | NormalizationFailure | None


@dataclass(frozen=True, slots=True)
class NormalizedAmounts:
    """The invoice's header amounts after normalisation (USR-002-03 AC6).

    Each field is a ``Decimal``, a ``NormalizationFailure`` (printed but unusable), or
    ``None`` (not extracted; whether that matters is the consumer's decision).
    """

    net: AmountValue = None
    gross: AmountValue = None

    @property
    def failures(self) -> tuple[NormalizationFailure, ...]:
        values = (getattr(self, name) for name in AMOUNT_FIELDS)
        return tuple(value for value in values if isinstance(value, NormalizationFailure))

    def formatted(self) -> dict[str, str | None]:
        """Hungarian notation per field; ``None`` where there is no usable amount."""
        result: dict[str, str | None] = {}
        for name in AMOUNT_FIELDS:
            value = getattr(self, name)
            result[name] = format_hungarian_amount(value) if isinstance(value, Decimal) else None
        return result


def normalize_amounts(extraction: InvoiceExtraction) -> NormalizedAmounts:
    """Normalise every header amount independently with the same rule set."""
    values: dict[str, AmountValue] = {}
    for name in AMOUNT_FIELDS:
        raw: str | None = getattr(extraction, name)
        if raw is None:
            values[name] = None
            continue
        result = normalize_amount(raw)
        if isinstance(result, NormalizationFailure):
            result = dataclasses.replace(result, field=name)
        values[name] = result
    return NormalizedAmounts(**values)


WHOLE_NUMBER_CURRENCIES: Final = frozenset({"HUF"})


def _is_resolved_iso_code(currency_iso: str | None) -> bool:
    """An upper-case ISO 4217 code exactly as ``to_iso_code`` returns it."""
    return (
        currency_iso is not None
        and len(currency_iso) == 3
        and currency_iso.isascii()
        and currency_iso.isalpha()
        and currency_iso.isupper()
        and is_currency(currency_iso)
    )


def round_for_ledger(amount: Decimal, currency_iso: str | None) -> Decimal | NormalizationFailure:
    """Apply the booking precision rule: HUF to a whole number, others unchanged."""
    if currency_iso is None or not _is_resolved_iso_code(currency_iso):
        return NormalizationFailure(
            f"cannot round for the ledger: currency {currency_iso!r} is not a resolved "
            "ISO 4217 code",
            field="currency",
        )
    if not amount.is_finite():
        return NormalizationFailure(f"cannot round a non-finite amount {amount}")
    if currency_iso not in WHOLE_NUMBER_CURRENCIES:
        return amount
    # int() is exact at any magnitude and drops both the sign of zero and any exponent.
    return Decimal(int(amount.to_integral_value(rounding=ROUND_HALF_UP)))


def round_amounts_for_ledger(
    amounts: NormalizedAmounts, currency_iso: str | None
) -> NormalizedAmounts:
    """Round Net and Gross independently; failures and unextracted fields pass through."""
    values: dict[str, AmountValue] = {}
    for name in AMOUNT_FIELDS:
        value: AmountValue = getattr(amounts, name)
        if isinstance(value, Decimal):
            value = round_for_ledger(value, currency_iso)
            if isinstance(value, NormalizationFailure):
                value = dataclasses.replace(value, field=name)
        values[name] = value
    return NormalizedAmounts(**values)


__all__ = [
    "AMOUNT_FIELDS",
    "HU_DECIMAL_SEPARATOR",
    "HU_THOUSANDS_SEPARATOR",
    "WHOLE_NUMBER_CURRENCIES",
    "NormalizedAmounts",
    "format_hungarian_amount",
    "normalize_amount",
    "normalize_amounts",
    "round_amounts_for_ledger",
    "round_for_ledger",
]
