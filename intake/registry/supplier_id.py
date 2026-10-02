"""Supplier name normalisation to the [SUPPLIER8] registry-number component (USR-003-03).

The transformation is pure and deterministic, applied in this order:

1. Unicode NFKD decomposition, then removal of combining marks, so accented letters
   (including the Hungarian double-acute ``ő``/``ű``) become their base Latin letters.
2. A single leading ``Magyar`` *word* is dropped (case-insensitive, start of string only,
   after leading whitespace). ``Magyarország`` is not affected; neither is a ``Magyar``
   anywhere other than the start.
3. Upper-casing.
4. Removal of everything that is not an ASCII letter or digit (spaces, punctuation,
   control characters, and any non-Latin script that survives step 1).
5. Truncation to the first 8 characters; shorter names are kept in full, unpadded.

Legal-form suffixes such as ``Kft``/``Zrt`` are ordinary characters and are kept
("Kővári Kft" -> ``KOVARIKF``).
"""

import re
import unicodedata

SUPPLIER_ID_LENGTH = 8

_LEADING_MAGYAR = re.compile(r"^\s*magyar\b", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_supplier(name: str) -> str:
    """Return the 1-8 character upper-case ASCII identifier for a supplier name.

    Raises:
        ValueError: if nothing identifying remains after normalisation (e.g. an empty
            name, only punctuation, or ``"Magyar"`` alone). An empty identifier would
            yield a malformed registry number, so the caller must flag the invoice
            instead of filing it.
    """
    cleaned = _strip_accents(name)
    cleaned = _LEADING_MAGYAR.sub("", cleaned, count=1)
    cleaned = _NON_ALNUM.sub("", cleaned.upper())
    if not cleaned:
        raise ValueError(f"supplier name {name!r} normalises to an empty identifier")
    return cleaned[:SUPPLIER_ID_LENGTH]
