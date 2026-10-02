"""The shared ``YYMM`` monthly Drive folder naming convention.

The monthly folder is named exactly like the registry number's ``[YYMM]`` prefix
(two-digit year, zero-padded month). Sequence derivation (USR-003-02) and filing
(USR-003-04) must agree on it byte for byte, so both import it from here rather than
formatting the name themselves (technical design, "Dependency Analysis").
"""

import re
from datetime import date

YYMM_PATTERN = re.compile(r"[0-9]{2}(?:0[1-9]|1[0-2])")
"""A valid ``YYMM`` value (ASCII digits only); use with ``fullmatch``."""


def yymm_folder_name(value: date) -> str:
    """Return the ``YYMM`` folder name for a date (a ``datetime``'s time is ignored).

    Raises:
        TypeError: if ``value`` is not a ``date``/``datetime``.
    """
    if not isinstance(value, date):
        raise TypeError(f"expected a date, got {type(value).__name__}")
    return f"{value.year % 100:02d}{value.month:02d}"


def validate_yymm(yymm: str) -> str:
    """Return ``yymm`` unchanged if it is a valid ``YYMM`` value.

    Raises:
        ValueError: if it is not exactly two ASCII year digits and a month ``01``-``12``.
    """
    if not isinstance(yymm, str) or not YYMM_PATTERN.fullmatch(yymm):
        raise ValueError(f"month must be YYMM (e.g. '2405'), got {yymm!r}")
    return yymm


__all__ = ["YYMM_PATTERN", "validate_yymm", "yymm_folder_name"]
