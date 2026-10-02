"""Task 6 / USR-002-02: date normalisation to the ledger's YYYY.MM.DD format."""

from datetime import date

import pytest

from intake.models import FlagReason, InvoiceExtraction, StageResult
from intake.normalization import NormalizationFailure as FailureFromPackage
from intake.normalization import (
    NormalizedDates,
    format_ledger_date,
    normalize_date,
    normalize_dates,
)
from intake.normalization.country_of_origin import Origin
from intake.normalization.result import NormalizationFailure

ALL_ORIGINS = list(Origin)


def _assert_flagged(result: object) -> NormalizationFailure:
    assert isinstance(result, NormalizationFailure), result
    assert result.reason is FlagReason.INCOMPLETE_DATA
    assert result.detail
    return result


# --- NormalizationFailure (shared with Tasks 7/8) ----------------------------


def test_failure_defaults_to_incomplete_data_and_is_reexported() -> None:
    failure = NormalizationFailure("cannot parse")
    assert failure.reason is FlagReason.INCOMPLETE_DATA
    assert failure.detail == "cannot parse"
    assert failure.field is None
    assert FailureFromPackage is NormalizationFailure
    hash(failure)


def test_failure_converts_to_an_incomplete_stage_result() -> None:
    result: StageResult[date] = NormalizationFailure("bad date", field="due_date").to_stage_result()
    assert not result.is_ok
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail == "due_date: bad date"


def test_failure_requires_a_detail() -> None:
    with pytest.raises(ValueError, match="detail"):
        NormalizationFailure("  ")


# --- ledger formatter ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2024, 3, 5), "2024.03.05"),
        (date(2024, 12, 31), "2024.12.31"),
        (date(1999, 1, 1), "1999.01.01"),
    ],
)
def test_format_ledger_date_is_zero_padded_yyyy_mm_dd(value: date, expected: str) -> None:
    assert format_ledger_date(value) == expected


# --- AC1: Hungarian / domestic formats ----------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2024.03.05.",
        "2024.03.05",
        "2024. 03. 05.",
        "2024.3.5.",
        "2024-03-05",
        "2024/03/05",
        "2024. március 5.",
        "2024. Március 5.",
        "2024. marcius 5.",
        "2024. márc. 5.",
        "2024 március 05",
        "  2024.03.05.  ",
    ],
)
def test_ac1_domestic_formats_normalise_to_correct_day(raw: str) -> None:
    result = normalize_date(raw, Origin.DOMESTIC)
    assert result == date(2024, 3, 5)
    assert isinstance(result, date)
    assert format_ledger_date(result) == "2024.03.05"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024. január 1.", date(2024, 1, 1)),
        ("2024. jan. 31.", date(2024, 1, 31)),
        ("2024. február 29.", date(2024, 2, 29)),
        ("2024. febr. 1.", date(2024, 2, 1)),
        ("2024. április 2.", date(2024, 4, 2)),
        ("2024. ápr. 2.", date(2024, 4, 2)),
        ("2024. május 3.", date(2024, 5, 3)),
        ("2024. máj. 3.", date(2024, 5, 3)),
        ("2024. június 4.", date(2024, 6, 4)),
        ("2024. július 5.", date(2024, 7, 5)),
        ("2024. augusztus 6.", date(2024, 8, 6)),
        ("2024. szeptember 7.", date(2024, 9, 7)),
        ("2024. szept. 7.", date(2024, 9, 7)),
        ("2024. október 8.", date(2024, 10, 8)),
        ("2024. november 9.", date(2024, 11, 9)),
        ("2024. december 10.", date(2024, 12, 10)),
    ],
)
def test_ac1_every_hungarian_month_name(raw: str, expected: date) -> None:
    assert normalize_date(raw, Origin.DOMESTIC) == expected


def test_ac1_domestic_day_first_numeric_is_not_hungarian_format_and_is_flagged() -> None:
    """Hungarian dates are year-first; a DD/MM-vs-MM/DD pattern on a domestic invoice
    matches no domestic rule, so it is flagged rather than guessed."""
    _assert_flagged(normalize_date("05/03/2024", Origin.DOMESTIC))


# --- AC2: US MM/DD/YYYY --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("03/14/2024", date(2024, 3, 14)),
        ("3/14/2024", date(2024, 3, 14)),
        ("01/02/2024", date(2024, 1, 2)),
        ("12-31-2024", date(2024, 12, 31)),
        ("March 14, 2024", date(2024, 3, 14)),
        ("Mar 14 2024", date(2024, 3, 14)),
        ("Sept. 3, 2024", date(2024, 9, 3)),
        ("2024-03-14", date(2024, 3, 14)),
    ],
)
def test_ac2_us_dates_normalise_without_transposition(raw: str, expected: date) -> None:
    assert normalize_date(raw, Origin.US) == expected


def test_qa_us_scenario_03_14_2024() -> None:
    result = normalize_date("03/14/2024", Origin.US)
    assert isinstance(result, date)
    assert format_ledger_date(result) == "2024.03.14"


def test_ac2_day_first_value_on_us_invoice_is_flagged_not_swapped() -> None:
    """14/03/2024 is impossible as MM/DD; it is flagged, never silently re-read as DD/MM."""
    _assert_flagged(normalize_date("14/03/2024", Origin.US))


# --- AC3: other foreign DD/MM/YYYY ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14/03/2024", date(2024, 3, 14)),
        ("14.03.2024", date(2024, 3, 14)),
        ("14.03.2024.", date(2024, 3, 14)),
        ("14-03-2024", date(2024, 3, 14)),
        ("1/2/2024", date(2024, 2, 1)),
        ("01/02/2024", date(2024, 2, 1)),
        ("14 March 2024", date(2024, 3, 14)),
        ("14th March 2024", date(2024, 3, 14)),
        ("3 Sep 2024", date(2024, 9, 3)),
        ("2024-03-14", date(2024, 3, 14)),
    ],
)
def test_ac3_other_foreign_dates_normalise_without_transposition(raw: str, expected: date) -> None:
    assert normalize_date(raw, Origin.OTHER_FOREIGN) == expected


def test_qa_other_foreign_scenario_14_03_2024() -> None:
    result = normalize_date("14/03/2024", Origin.OTHER_FOREIGN)
    assert isinstance(result, date)
    assert format_ledger_date(result) == "2024.03.14"


def test_ac3_month_first_value_on_other_foreign_invoice_is_flagged() -> None:
    _assert_flagged(normalize_date("03/14/2024", Origin.OTHER_FOREIGN))


# --- AC4: origin selects the rule for ambiguous patterns -----------------------


def test_ac4_same_ambiguous_pattern_resolves_by_origin() -> None:
    assert normalize_date("01/02/2024", Origin.US) == date(2024, 1, 2)
    assert normalize_date("01/02/2024", Origin.OTHER_FOREIGN) == date(2024, 2, 1)


# --- AC5: unparseable / ambiguous dates are flagged -----------------------------


def test_ac5_qa_ambiguous_pattern_with_unknown_origin_is_flagged() -> None:
    failure = _assert_flagged(normalize_date("01/02/2024", Origin.UNKNOWN))
    assert "origin" in failure.detail


@pytest.mark.parametrize("raw", ["14/03/2024", "03/14/2024", "13.13.2024"])
def test_ac5_day_month_order_formats_need_a_known_origin(raw: str) -> None:
    """Even when only one reading is valid, the day/month order rule is chosen by
    origin; with no origin, nothing is inferred from the digits."""
    _assert_flagged(normalize_date(raw, Origin.UNKNOWN))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024.03.05.", date(2024, 3, 5)),
        ("2024-03-05", date(2024, 3, 5)),
        ("2024. március 5.", date(2024, 3, 5)),
        ("March 5, 2024", date(2024, 3, 5)),
        ("5 March 2024", date(2024, 3, 5)),
    ],
)
def test_ac5_unambiguous_formats_do_not_need_origin(raw: str, expected: date) -> None:
    """Year-first and month-name dates have exactly one reading under every rule."""
    assert normalize_date(raw, Origin.UNKNOWN) == expected


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "n/a",
        "next Tuesday",
        "2024.02.30.",  # not a calendar day
        "2023. február 29.",  # not a leap year
        "2024.13.01",  # month 13
        "2024.00.10",
        "2024. foo 5.",  # unknown month name
        "03/14/24",  # two-digit year: century would be a guess
        "2024.03",  # incomplete
        "2024.03.05.12",  # trailing junk
        "2024-03/05",  # mixed separators
        "0024.03.05",  # implausible year
        "2024.03.05 25:00",  # not a time of day
        "2024.03.05 10:61",
        "2024.03.05 10",  # hour without minutes
        "2024.03.05 10:00 ma",  # trailing junk after the time
    ],
)
def test_ac5_unparseable_values_are_flagged_never_defaulted(
    raw: str | None, origin: Origin
) -> None:
    _assert_flagged(normalize_date(raw, origin))


@pytest.mark.parametrize("origin", [Origin.US, Origin.OTHER_FOREIGN])
def test_ac5_invalid_calendar_day_in_day_first_pattern_is_flagged(origin: Origin) -> None:
    _assert_flagged(normalize_date("31/31/2024", origin))


def test_ac5_failure_detail_names_the_raw_value() -> None:
    failure = _assert_flagged(normalize_date("2024.02.30.", Origin.DOMESTIC))
    assert "2024.02.30." in failure.detail


def test_normalisation_is_deterministic() -> None:
    results = {normalize_date("01/02/2024", Origin.US) for _ in range(50)}
    assert results == {date(2024, 1, 2)}


# --- AC6: every date field normalised independently ----------------------------


def test_ac6_every_date_field_is_normalised_with_the_same_rule_set() -> None:
    extraction = InvoiceExtraction(
        issue_date="2024.03.01.",
        due_date="2024. március 31.",
        supply_date="2024-03-05",
        performance_date="2024. 03. 05.",
    )
    dates = normalize_dates(extraction, Origin.DOMESTIC)
    assert dates == NormalizedDates(
        issue_date=date(2024, 3, 1),
        due_date=date(2024, 3, 31),
        supply_date=date(2024, 3, 5),
        performance_date=date(2024, 3, 5),
    )
    assert dates.failures == ()


def test_ac6_one_bad_field_does_not_block_the_others() -> None:
    extraction = InvoiceExtraction(
        issue_date="03/01/2024",
        due_date="garbage",
        supply_date="March 5, 2024",
        performance_date=None,
    )
    dates = normalize_dates(extraction, Origin.US)
    assert dates.issue_date == date(2024, 3, 1)
    assert dates.supply_date == date(2024, 3, 5)
    # Not extracted: not a failure; the issue date stands in for it.
    assert dates.performance_date == date(2024, 3, 1)
    assert isinstance(dates.due_date, NormalizationFailure)
    assert dates.due_date.field == "due_date"
    assert dates.failures == (dates.due_date,)


def test_ac6_unknown_origin_flags_only_the_ambiguous_fields() -> None:
    extraction = InvoiceExtraction(issue_date="2024.03.01.", due_date="01/04/2024")
    dates = normalize_dates(extraction, Origin.UNKNOWN)
    assert dates.issue_date == date(2024, 3, 1)
    assert isinstance(dates.due_date, NormalizationFailure)
    assert dates.due_date.field == "due_date"
    assert dates.supply_date is None


def test_ac6_all_failures_are_reported_in_field_order() -> None:
    extraction = InvoiceExtraction(
        issue_date="x", due_date="y", supply_date="z", performance_date="w"
    )
    failures = normalize_dates(extraction, Origin.OTHER_FOREIGN).failures
    assert [failure.field for failure in failures] == [
        "issue_date",
        "due_date",
        "supply_date",
        "performance_date",
    ]


def test_normalized_dates_formatted_view_for_the_ledger() -> None:
    dates = NormalizedDates(
        issue_date=date(2024, 3, 1),
        due_date=NormalizationFailure("bad", field="due_date"),
        supply_date=None,
        performance_date=date(2024, 3, 5),
    )
    assert dates.formatted() == {
        "issue_date": "2024.03.01",
        "due_date": None,
        "supply_date": None,
        "performance_date": "2024.03.05",
    }


# --- A trailing time of day is ignored ------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026.09.23 08:03:15",
        "2026.09.23. 08:03",
        "2026. 09. 23. 8:03",
        "2026-09-23T08:03:15",
        "2026. szeptember 23. 08:03:15",
    ],
)
def test_a_trailing_time_of_day_is_ignored(raw: str) -> None:
    assert normalize_date(raw, Origin.DOMESTIC) == date(2026, 9, 23)


def test_a_trailing_time_keeps_the_origin_rule_for_day_month_order() -> None:
    assert normalize_date("23/09/2026 08:03", Origin.OTHER_FOREIGN) == date(2026, 9, 23)
    _assert_flagged(normalize_date("23/09/2026 08:03", Origin.DOMESTIC))


# --- Missing due/performance date falls back to the issue date ---------------------
# Receipts paid on the spot print neither (decision of the project owner, 2026-10-03).


def test_missing_due_and_performance_date_fall_back_to_the_issue_date() -> None:
    dates = normalize_dates(InvoiceExtraction(issue_date="2026.09.23 08:03:15"), Origin.DOMESTIC)
    assert dates == NormalizedDates(
        issue_date=date(2026, 9, 23),
        due_date=date(2026, 9, 23),
        performance_date=date(2026, 9, 23),
    )


def test_printed_dates_win_over_the_issue_date_fallback() -> None:
    extraction = InvoiceExtraction(
        issue_date="2026.09.23", due_date="2026.10.23", performance_date="2026.09.20"
    )
    dates = normalize_dates(extraction, Origin.DOMESTIC)
    assert (dates.due_date, dates.performance_date) == (date(2026, 10, 23), date(2026, 9, 20))


def test_an_unparseable_printed_due_date_is_flagged_not_replaced() -> None:
    extraction = InvoiceExtraction(issue_date="2026.09.23", due_date="garbage")
    dates = normalize_dates(extraction, Origin.DOMESTIC)
    assert isinstance(dates.due_date, NormalizationFailure)
    assert dates.due_date.field == "due_date"


@pytest.mark.parametrize("issue", [None, "garbage"])
def test_no_fallback_without_a_usable_issue_date(issue: str | None) -> None:
    dates = normalize_dates(InvoiceExtraction(issue_date=issue), Origin.DOMESTIC)
    assert dates.due_date is None
    assert dates.performance_date is None
