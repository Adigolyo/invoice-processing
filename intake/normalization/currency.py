"""Currency normalisation to an ISO 4217 code (USR-002-05).

``to_iso_code(raw, origin, currency_map)`` resolves the currency as printed on the
invoice, in this order:

1. **Cleaning** (AC4): Unicode NFKC (so a full-width ``＄`` is ``$``), surrounding
   whitespace and dots stripped, case ignored: ``"huf"``, ``"HUF."`` and ``"Ft "`` all
   resolve like their canonical forms.
2. **ISO 4217 code** (AC2): a three-letter code CLDR knows as a currency (``USD``,
   ``gbp``) is returned upper-cased, whatever the origin.
3. **Configured symbol table** (AC1): ``Config.currency_map`` (the ``Config`` tab,
   seeded with ``Ft=HUF, €=EUR, $=USD``) maps symbols/abbreviations to codes. Only
   configured symbols are recognised; adding a currency is a Config-tab edit.
4. **Ambiguous symbols** (AC3): a symbol that is the local symbol of more than one
   country's currency in CLDR (``$``: USD, CAD, AUD, MXN, ...; ``kr``: DKK, NOK, SEK) is
   accepted only when the invoice's origin confirms the configured code, i.e. the
   origin country's own currency is that code (``$`` on a US invoice is USD). On an
   other-foreign, unknown-origin or domestic invoice ``$`` is flagged: ``Origin`` does
   not say which dollar country, and any default would be a guess.

Everything else (unknown text, missing value, a value with an amount embedded) returns
a ``NormalizationFailure``; origin never *supplies* a missing currency, since a
Hungarian supplier may invoice in EUR (AC5).
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from functools import cache
from typing import Final

from babel import Locale, UnknownLocaleError
from babel.numbers import get_territory_currencies, is_currency

from intake.normalization.country_of_origin import Origin
from intake.normalization.result import NormalizationFailure

_FIELD: Final = "currency"

# The country each origin stands for; OTHER_FOREIGN and UNKNOWN name no single country.
_ORIGIN_TERRITORY: Final[Mapping[Origin, str]] = {Origin.DOMESTIC: "HU", Origin.US: "US"}


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().strip(".").strip().casefold()


@cache
def _cldr_symbol_currencies() -> Mapping[str, frozenset[str]]:
    """Cleaned symbol -> ISO codes that use it as their local symbol somewhere (CLDR).

    For every territory, the symbol its currencies have in its most likely locale
    (e.g. ``und_CA`` -> ``en_CA``: CAD is ``$``).
    """
    table: defaultdict[str, set[str]] = defaultdict(set)
    for territory in Locale("en").territories:
        if not isinstance(territory, str) or len(territory) != 2 or not territory.isalpha():
            continue
        try:
            locale = Locale.parse(f"und_{territory}")
        except (UnknownLocaleError, ValueError):
            continue  # uninhabited / pseudo territories have no locale
        for code in get_territory_currencies(territory, tender=True):
            symbol = locale.currency_symbols.get(code)
            if symbol and (key := _clean(symbol)):
                table[key].add(code)
    return {key: frozenset(codes) for key, codes in table.items()}


def _home_currencies(origin: Origin) -> frozenset[str]:
    territory = _ORIGIN_TERRITORY.get(origin)
    if territory is None:
        return frozenset()
    return frozenset(get_territory_currencies(territory, tender=True))


def _is_iso_code(key: str) -> bool:
    return len(key) == 3 and key.isascii() and key.isalpha() and is_currency(key.upper())


def _fail(detail: str) -> NormalizationFailure:
    return NormalizationFailure(detail, field=_FIELD)


def to_iso_code(
    raw: str | None, origin: Origin, currency_map: Mapping[str, str]
) -> str | NormalizationFailure:
    """Resolve an extracted currency to an ISO 4217 code; flag, never guess."""
    key = _clean(raw) if raw is not None else ""
    if not key:
        return _fail("currency is missing")
    if _is_iso_code(key):
        return key.upper()

    configured = {
        code.strip().upper() for symbol, code in currency_map.items() if _clean(symbol) == key
    }
    if not configured:
        return _fail(f"unrecognised currency {raw!r}: not an ISO 4217 code or configured symbol")
    if len(configured) > 1:
        return _fail(
            f"currency {raw!r} is configured as more than one code: {', '.join(sorted(configured))}"
        )
    (code,) = configured

    sharing = _cldr_symbol_currencies().get(key, frozenset())
    if len(sharing) > 1 and code not in _home_currencies(origin):
        return _fail(
            f"currency symbol {raw!r} is ambiguous ({', '.join(sorted(sharing))}) and the "
            f"invoice's origin ({origin.value}) does not confirm {code}"
        )
    return code


__all__ = ["to_iso_code"]
