"""Country-of-origin determination, shared by USR-002-02, USR-002-04 and USR-002-05.

``determine_origin`` is the single place an invoice is classified as domestic,
US or other-foreign, so date parsing, performance-date selection and currency
disambiguation can never disagree.

The signal is the supplier's country as printed on the invoice
(``InvoiceExtraction.supplier_country``): an ISO 3166-1 alpha-2 code, the English or
Hungarian country name (accents and case ignored), or one of a few common aliases
(``HUN``, ``USA``, ``U.S.A.``). Currency, language and date style are deliberately *not*
used: a Hungarian supplier may invoice in EUR and a foreign one in HUF, so they would
be guesses. Anything missing or unrecognisable yields ``Origin.UNKNOWN``, which the
callers flag instead of guessing.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from functools import cache

from babel import Locale

from intake.models import InvoiceExtraction

_DOMESTIC_CODE = "HU"
_US_CODE = "US"

# CLDR pseudo-territories that are not countries.
_NON_COUNTRY_CODES = frozenset({"EU", "EZ", "UN", "QO", "ZZ", "XA", "XB"})
_NAME_LOCALES = ("en", "hu")
_ALIASES = {
    "hun": _DOMESTIC_CODE,
    "usa": _US_CODE,
    "united states of america": _US_CODE,
}


class Origin(StrEnum):
    """Which date/currency convention an invoice follows."""

    DOMESTIC = "domestic"
    US = "us"
    OTHER_FOREIGN = "other_foreign"
    UNKNOWN = "unknown"

    @property
    def is_foreign(self) -> bool:
        """US and other-foreign invoices share the foreign performance-date rule."""
        return self in (Origin.US, Origin.OTHER_FOREIGN)

    @property
    def is_known(self) -> bool:
        return self is not Origin.UNKNOWN


def _key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    without_dots = stripped.replace(".", "")
    return re.sub(r"\s+", " ", without_dots).strip().casefold()


@cache
def _country_lookup() -> dict[str, str]:
    """Normalised code/name/alias -> ISO 3166-1 alpha-2 code."""
    lookup: dict[str, str] = {}
    for locale_name in _NAME_LOCALES:
        for code, name in Locale(locale_name).territories.items():
            if len(code) != 2 or not code.isalpha() or code in _NON_COUNTRY_CODES:
                continue
            lookup[_key(code)] = code
            lookup[_key(name)] = code
    lookup.update(_ALIASES)
    return lookup


def determine_origin(extraction: InvoiceExtraction) -> Origin:
    """Classify the invoice by its supplier's country; ``UNKNOWN`` if not determinable."""
    raw = extraction.supplier_country
    if raw is None or not raw.strip():
        return Origin.UNKNOWN
    code = _country_lookup().get(_key(raw))
    if code is None:
        return Origin.UNKNOWN
    if code == _DOMESTIC_CODE:
        return Origin.DOMESTIC
    if code == _US_CODE:
        return Origin.US
    return Origin.OTHER_FOREIGN


__all__ = ["Origin", "determine_origin"]
