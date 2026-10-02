"""Thin wrapper over the Sheets v4 API for the ledger spreadsheet.

One spreadsheet holds two tabs (technical design, "Data Layer"):

- ``Config``: two columns, ``key | value``, with a header row. Each data row is one of
  ``intake.config.REQUIRED_KEYS``; lists are comma-separated strings and ``currency_map``
  is ``"Ft=HUF, €=EUR"``. Blank rows are skipped, unknown keys ignored, duplicate keys
  rejected. See ``docs/config-tab.md`` for the full layout with an example per key.
- ``Ledger``: header row A-H exactly ``LEDGER_HEADER``; one row per booked invoice.

The ``googleapiclient`` Sheets resource is injected through the constructor.

Error semantics: Sheets API failures propagate unchanged as
``googleapiclient.errors.HttpError`` (a failed dedupe read therefore blocks booking rather
than looking like "no duplicates"). A Ledger header that is not exactly A-H
``LEDGER_HEADER`` raises ``LedgerHeaderError`` before anything is read or appended; a bad
Config tab raises ``intake.config.ConfigError``; a malformed response raises
``SheetsResponseError``.

Only ``intake/pipeline/orchestrator.py`` may call ``append_row``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from intake.config import Config, ConfigError

logger = logging.getLogger(__name__)

SheetsResource = Any
"""The untyped ``googleapiclient`` Sheets v4 resource."""

LEDGER_TAB = "Ledger"
CONFIG_TAB = "Config"
LEDGER_HEADER: tuple[str, ...] = (
    "INV_ID_int",
    "Provider",
    "INV_ID_ext",
    "INV_type",
    "Currency",
    "Net",
    "Gross",
    "Due date",
)
CONFIG_HEADER: tuple[str, str] = ("key", "value")
LEDGER_DATE_FORMAT = "%Y.%m.%d"

_ISO_4217 = re.compile(r"^[A-Z]{3}$")


class LedgerHeaderError(RuntimeError):
    """The Ledger tab's header row is not exactly the expected A-H columns."""


class SheetsResponseError(RuntimeError):
    """The Sheets API answered, but not with the shape this client relies on."""


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One ledger row, columns A-H in ``LEDGER_HEADER`` order (USR-004-01).

    Values are already normalised: ``net``/``gross`` rounded per currency (USR-004-02),
    ``currency`` an ISO 4217 code, ``due_date`` a calendar date written as ``YYYY.MM.DD``.
    """

    inv_id_int: str
    provider: str
    inv_id_ext: str
    inv_type: str
    currency: str
    net: Decimal
    gross: Decimal
    due_date: date

    def __post_init__(self) -> None:
        for name in ("inv_id_int", "provider", "inv_id_ext", "inv_type", "currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LedgerRow.{name} must be a non-blank string")
        if not _ISO_4217.fullmatch(self.currency):
            raise ValueError(f"LedgerRow.currency must be an ISO 4217 code, got {self.currency!r}")
        for name in ("net", "gross"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"LedgerRow.{name} must be a Decimal, got {type(value).__name__}")
            if not value.is_finite():
                raise ValueError(f"LedgerRow.{name} must be finite, got {value}")
        if not isinstance(self.due_date, date):
            raise TypeError(
                f"LedgerRow.due_date must be a date, got {type(self.due_date).__name__}"
            )

    def to_values(self) -> list[str | int | float]:
        """Cell values for columns A-H (amounts as numbers, the rest as text)."""
        return [
            self.inv_id_int,
            self.provider,
            self.inv_id_ext,
            self.inv_type,
            self.currency,
            _amount_cell(self.net),
            _amount_cell(self.gross),
            self.due_date.strftime(LEDGER_DATE_FORMAT),
        ]


def _amount_cell(amount: Decimal) -> int | float:
    # Sheets stores numbers as doubles; whole amounts (e.g. rounded HUF) are sent as ints.
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _a1(tab: str, cells: str) -> str:
    return "'" + tab.replace("'", "''") + "'!" + cells


class SheetsClient:
    """Sheets operations: Config loading, dedupe keys and ledger appends."""

    def __init__(
        self,
        service: SheetsResource,
        spreadsheet_id: str,
        ledger_tab: str = LEDGER_TAB,
        config_tab: str = CONFIG_TAB,
    ) -> None:
        if not spreadsheet_id.strip():
            raise ValueError("spreadsheet_id is required")
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._ledger_tab = ledger_tab
        self._config_tab = config_tab

    def load_config(self) -> Config:
        """Read the Config tab (``key | value`` rows) into a validated ``Config``."""
        rows = self._get_values(_a1(self._config_tab, "A:B"))
        if not rows or tuple(_cell_text(c).lower() for c in rows[0][:2]) != CONFIG_HEADER:
            raise ConfigError(
                f"{self._config_tab} tab must start with a header row 'key | value', "
                f"got {rows[0] if rows else 'an empty tab'}"
            )
        raw: dict[str, Any] = {}
        for row in rows[1:]:
            key = _cell_text(row[0]) if row else ""
            if not key:
                continue
            if key in raw:
                raise ConfigError(f"{self._config_tab} tab defines {key!r} more than once")
            raw[key] = row[1] if len(row) > 1 else None
        config = Config.from_mapping(raw)
        logger.info("loaded config", extra={"config_keys": len(raw)})
        return config

    def verify_ledger_header(self) -> None:
        """Fail loudly unless the Ledger header row is exactly ``LEDGER_HEADER`` (A-H)."""
        rows = self._get_values(_a1(self._ledger_tab, "A1:H1"))
        self._check_header(rows[0] if rows else [])

    def load_existing_keys(self) -> set[tuple[str, str]]:
        """``{(INV_ID_ext, Provider)}`` of every ledger row, read live (never cached).

        The header is verified in the same read. Rows missing either value are skipped.
        """
        rows = self._get_values(_a1(self._ledger_tab, "A1:H"))
        self._check_header(rows[0] if rows else [])
        keys: set[tuple[str, str]] = set()
        for row in rows[1:]:
            provider = _cell_text(row[1]) if len(row) > 1 else ""
            ext_id = _cell_text(row[2]) if len(row) > 2 else ""
            if provider and ext_id:
                keys.add((ext_id, provider))
        return keys

    def append_row(self, row: LedgerRow) -> None:
        """Append exactly one row to the Ledger tab after re-verifying its header."""
        self.verify_ledger_header()
        request = (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=_a1(self._ledger_tab, "A:H"),
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row.to_values()]},
            )
        )
        response = self._execute(request)
        updates = response.get("updates")
        if not isinstance(updates, dict) or updates.get("updatedRows") != 1:
            raise SheetsResponseError(f"ledger append did not confirm one written row: {updates}")

    def _check_header(self, actual: list[Any]) -> None:
        cells = tuple(_cell_text(c) for c in actual[: len(LEDGER_HEADER)])
        if cells != LEDGER_HEADER:
            raise LedgerHeaderError(
                f"{self._ledger_tab} tab header must be exactly {', '.join(LEDGER_HEADER)} "
                f"(columns A-H), found {list(cells)}; refusing to read or write the ledger"
            )

    def _get_values(self, a1_range: str) -> list[list[Any]]:
        request = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=a1_range,
                valueRenderOption="UNFORMATTED_VALUE",
            )
        )
        values = self._execute(request).get("values", [])
        if not isinstance(values, list) or not all(isinstance(r, list) for r in values):
            raise SheetsResponseError(f"malformed 'values' in response for {a1_range}")
        return values

    @staticmethod
    def _execute(request: Any) -> dict[str, Any]:
        result = request.execute()
        if not isinstance(result, dict):
            raise SheetsResponseError(
                f"unexpected Sheets API response type: {type(result).__name__}"
            )
        return result


__all__ = [
    "CONFIG_HEADER",
    "LEDGER_HEADER",
    "LedgerHeaderError",
    "LedgerRow",
    "SheetsClient",
    "SheetsResponseError",
]
