"""Task 8 / USR-002-04: performance date selection (domestic/foreign rule)."""

from datetime import date
from itertools import permutations

import pytest

from intake.models import FlagReason, InvoiceExtraction
from intake.normalization import (
    NormalizationFailure,
    NormalizedDates,
    Origin,
    ledger_performance_date,
    normalize_dates,
    select_performance_date,
)
from intake.normalization import performance_date as module

FOREIGN = [Origin.US, Origin.OTHER_FOREIGN]

ISSUE = date(2024, 3, 10)
DUE = date(2024, 4, 9)
SUPPLY = date(2024, 3, 5)
STATED = date(2024, 2, 28)


def _failure(field: str) -> NormalizationFailure:
    return NormalizationFailure("unrecognised date format 'xx'", field=field)


def _assert_flagged(result: object) -> NormalizationFailure:
    assert isinstance(result, NormalizationFailure), result
    assert result.reason is FlagReason.INCOMPLETE_DATA
    assert result.field == "performance_date"
    assert result.detail
    return result


def test_public_api_is_reexported() -> None:
    assert select_performance_date is module.select_performance_date
    assert ledger_performance_date is module.ledger_performance_date


# --- AC1: domestic uses the stated performance date ---------------------------


def test_domestic_uses_stated_date_not_issue_due_or_supply() -> None:
    dates = NormalizedDates(
        issue_date=date(2024, 1, 1),
        due_date=date(2024, 1, 2),
        supply_date=date(2024, 1, 3),
        performance_date=STATED,
    )
    assert select_performance_date(dates, Origin.DOMESTIC) == STATED


def test_domestic_stated_date_wins_even_when_later_than_the_other_dates() -> None:
    later = date(2024, 5, 31)
    dates = NormalizedDates(
        issue_date=ISSUE, due_date=DUE, supply_date=SUPPLY, performance_date=later
    )
    assert select_performance_date(dates, Origin.DOMESTIC) == later


def test_domestic_with_only_a_stated_date_resolves() -> None:
    assert (
        select_performance_date(NormalizedDates(performance_date=STATED), Origin.DOMESTIC) == STATED
    )


def test_domestic_ignores_unparseable_other_dates() -> None:
    dates = NormalizedDates(
        issue_date=_failure("issue_date"),
        due_date=_failure("due_date"),
        supply_date=None,
        performance_date=STATED,
    )
    assert select_performance_date(dates, Origin.DOMESTIC) == STATED


# --- AC4: domestic without a stated date is flagged, never foreign-ruled ------


def test_domestic_without_stated_date_is_flagged_not_defaulted_to_foreign_rule() -> None:
    dates = NormalizedDates(issue_date=ISSUE, due_date=DUE, supply_date=SUPPLY)
    failure = _assert_flagged(select_performance_date(dates, Origin.DOMESTIC))
    assert "domestic" in failure.detail


def test_domestic_with_nothing_extracted_is_flagged() -> None:
    _assert_flagged(select_performance_date(NormalizedDates(), Origin.DOMESTIC))


def test_domestic_with_unparseable_stated_date_is_flagged_with_its_cause() -> None:
    dates = NormalizedDates(
        issue_date=ISSUE,
        due_date=DUE,
        supply_date=SUPPLY,
        performance_date=NormalizationFailure("'2024/13/45' is not a valid calendar date"),
    )
    failure = _assert_flagged(select_performance_date(dates, Origin.DOMESTIC))
    assert "2024/13/45" in failure.detail


# --- AC2: foreign uses the earliest of issue, due and supply ------------------


@pytest.mark.parametrize("origin", FOREIGN)
@pytest.mark.parametrize(
    "order", list(permutations([date(2024, 3, 1), date(2024, 3, 2), date(2024, 3, 3)]))
)
def test_foreign_picks_earliest_of_three_regardless_of_which_field_holds_it(
    origin: Origin, order: tuple[date, date, date]
) -> None:
    issue, due, supply = order
    dates = NormalizedDates(issue_date=issue, due_date=due, supply_date=supply)
    assert select_performance_date(dates, origin) == date(2024, 3, 1)


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_ignores_a_stated_performance_date(origin: Origin) -> None:
    dates = NormalizedDates(
        issue_date=ISSUE, due_date=DUE, supply_date=SUPPLY, performance_date=date(2024, 1, 1)
    )
    assert select_performance_date(dates, origin) == SUPPLY


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_with_equal_candidates_selects_that_date(origin: Origin) -> None:
    same = date(2024, 6, 30)
    dates = NormalizedDates(issue_date=same, due_date=same, supply_date=same)
    assert select_performance_date(dates, origin) == same


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_compares_across_year_boundaries(origin: Origin) -> None:
    dates = NormalizedDates(
        issue_date=date(2024, 1, 2), due_date=date(2024, 1, 31), supply_date=date(2023, 12, 31)
    )
    assert select_performance_date(dates, origin) == date(2023, 12, 31)


# --- AC3: foreign with missing candidates (2 and 1 present) -------------------


@pytest.mark.parametrize("origin", FOREIGN)
@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (NormalizedDates(issue_date=ISSUE, due_date=DUE), ISSUE),  # QA: supply missing
        (NormalizedDates(issue_date=ISSUE, supply_date=SUPPLY), SUPPLY),
        (NormalizedDates(due_date=DUE, supply_date=SUPPLY), SUPPLY),
        (NormalizedDates(issue_date=ISSUE), ISSUE),
        (NormalizedDates(due_date=DUE), DUE),
        (NormalizedDates(supply_date=SUPPLY), SUPPLY),
    ],
)
def test_foreign_with_missing_candidates_uses_earliest_remaining(
    origin: Origin, dates: NormalizedDates, expected: date
) -> None:
    assert select_performance_date(dates, origin) == expected


# --- AC5: foreign with no usable candidate is flagged -------------------------


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_with_all_candidates_missing_is_flagged(origin: Origin) -> None:
    failure = _assert_flagged(select_performance_date(NormalizedDates(), origin))
    assert "issue" in failure.detail and "due" in failure.detail and "supply" in failure.detail


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_with_only_a_stated_performance_date_is_flagged(origin: Origin) -> None:
    _assert_flagged(select_performance_date(NormalizedDates(performance_date=STATED), origin))


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_with_all_candidates_unparseable_is_flagged(origin: Origin) -> None:
    dates = NormalizedDates(
        issue_date=_failure("issue_date"),
        due_date=_failure("due_date"),
        supply_date=_failure("supply_date"),
    )
    _assert_flagged(select_performance_date(dates, origin))


# --- Unparseable candidate among usable ones: flag, don't guess ---------------
# The unreadable date might be the earliest; ignoring it could pick a later date.


@pytest.mark.parametrize("origin", FOREIGN)
@pytest.mark.parametrize("bad_field", ["issue_date", "due_date", "supply_date"])
def test_foreign_with_an_unparseable_candidate_is_flagged_not_ignored(
    origin: Origin, bad_field: str
) -> None:
    values: dict[str, date | NormalizationFailure] = {
        "issue_date": ISSUE,
        "due_date": DUE,
        "supply_date": SUPPLY,
    }
    values[bad_field] = _failure(bad_field)
    failure = _assert_flagged(select_performance_date(NormalizedDates(**values), origin))
    assert bad_field in failure.detail


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_with_one_unparseable_and_others_missing_is_flagged(origin: Origin) -> None:
    dates = NormalizedDates(issue_date=ISSUE, supply_date=_failure("supply_date"))
    _assert_flagged(select_performance_date(dates, origin))


# --- Unknown origin: the rule cannot be chosen, so flag -----------------------


@pytest.mark.parametrize(
    "dates",
    [
        NormalizedDates(
            issue_date=ISSUE, due_date=DUE, supply_date=SUPPLY, performance_date=STATED
        ),
        NormalizedDates(performance_date=STATED),
        NormalizedDates(issue_date=ISSUE, due_date=DUE, supply_date=SUPPLY),
        NormalizedDates(),
    ],
)
def test_unknown_origin_is_flagged_whatever_dates_exist(dates: NormalizedDates) -> None:
    failure = _assert_flagged(select_performance_date(dates, Origin.UNKNOWN))
    assert "origin" in failure.detail


# --- AC6: emitted in YYYY.MM.DD ------------------------------------------------


@pytest.mark.parametrize(
    ("dates", "origin", "expected"),
    [
        (NormalizedDates(performance_date=date(2024, 2, 5)), Origin.DOMESTIC, "2024.02.05"),
        (
            NormalizedDates(issue_date=date(2024, 3, 9), due_date=date(2024, 4, 8)),
            Origin.US,
            "2024.03.09",
        ),
        (NormalizedDates(supply_date=date(2023, 12, 1)), Origin.OTHER_FOREIGN, "2023.12.01"),
    ],
)
def test_ledger_performance_date_is_yyyy_mm_dd(
    dates: NormalizedDates, origin: Origin, expected: str
) -> None:
    assert ledger_performance_date(dates, origin) == expected


@pytest.mark.parametrize("origin", list(Origin))
def test_ledger_performance_date_passes_failures_through(origin: Origin) -> None:
    _assert_flagged(ledger_performance_date(NormalizedDates(), origin))


# --- End-to-end through normalize_dates (shared origin, USR-002-02) -----------


def test_us_invoice_from_rawInvoiceExtraction() -> None:
    extraction = InvoiceExtraction(
        issue_date="03/14/2024", due_date="04/13/2024", supply_date="03/01/2024"
    )
    dates = normalize_dates(extraction, Origin.US)
    assert ledger_performance_date(dates, Origin.US) == "2024.03.01"


def test_other_foreign_invoice_from_rawInvoiceExtraction() -> None:
    extraction = InvoiceExtraction(issue_date="14/03/2024", due_date="13/04/2024")
    dates = normalize_dates(extraction, Origin.OTHER_FOREIGN)
    assert ledger_performance_date(dates, Origin.OTHER_FOREIGN) == "2024.03.14"


def test_domestic_invoice_from_rawInvoiceExtraction() -> None:
    extraction = InvoiceExtraction(
        issue_date="2024. 03. 10.", due_date="2024.04.09.", performance_date="2024. február 28."
    )
    dates = normalize_dates(extraction, Origin.DOMESTIC)
    assert ledger_performance_date(dates, Origin.DOMESTIC) == "2024.02.28"


def test_domestic_invoice_with_foreign_style_stated_date_is_flagged() -> None:
    extraction = InvoiceExtraction(issue_date="2024.03.10", performance_date="02/28/2024")
    dates = normalize_dates(extraction, Origin.DOMESTIC)
    _assert_flagged(select_performance_date(dates, Origin.DOMESTIC))


def test_domestic_receipt_without_stated_date_uses_the_issue_date() -> None:
    # Fallback applied by normalize_dates (project owner decision, 2026-10-03).
    dates = normalize_dates(InvoiceExtraction(issue_date="2026.09.30 11:44:06"), Origin.DOMESTIC)
    assert ledger_performance_date(dates, Origin.DOMESTIC) == "2026.09.30"
