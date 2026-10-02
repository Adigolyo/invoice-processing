"""Task 2: shared domain models consumed by every pipeline stage."""

import dataclasses
from decimal import Decimal

import pytest

from intake.models import (
    Attachment,
    Candidate,
    ComparisonOutcome,
    ComparisonResult,
    Discrepancy,
    DiscrepancyField,
    FlagReason,
    InvoiceExtraction,
    LineItem,
    Route,
    RouteDecision,
    StageResult,
    StageStatus,
)

# --- enums -----------------------------------------------------------------


def test_flag_reason_covers_exactly_the_three_design_values() -> None:
    assert {member.name for member in FlagReason} == {
        "AMBIGUOUS_ROUTE",
        "INCOMPLETE_DATA",
        "MISSING_TIG",
    }


def test_route_covers_direct_tig_ambiguous() -> None:
    assert {member.name for member in Route} == {"DIRECT", "TIG", "AMBIGUOUS"}


def test_stage_status_covers_ok_incomplete_error() -> None:
    assert {member.value for member in StageStatus} == {"ok", "incomplete", "error"}


def test_comparison_outcome_covers_match_and_mismatch() -> None:
    assert {member.name for member in ComparisonOutcome} == {"MATCH", "MISMATCH"}


def test_discrepancy_field_covers_quantity_unit_price_total_net() -> None:
    assert {member.name for member in DiscrepancyField} == {
        "QUANTITY",
        "UNIT_PRICE",
        "TOTAL_NET",
    }


# --- Candidate -------------------------------------------------------------


def test_candidate_construction_and_equality() -> None:
    attachment = Attachment(filename="szamla.pdf", mime_type="application/pdf", attachment_id="a1")
    first = Candidate(
        message_id="m1",
        thread_id="t1",
        sender="billing@acme.example",
        subject="Számla 2024/05",
        attachments=(attachment,),
    )
    second = Candidate(
        message_id="m1",
        thread_id="t1",
        sender="billing@acme.example",
        subject="Számla 2024/05",
        attachments=(Attachment("szamla.pdf", "application/pdf", "a1"),),
    )
    assert first == second
    assert first.label_names == frozenset()


def test_candidate_defaults_to_no_attachments() -> None:
    candidate = Candidate(message_id="m1", thread_id="t1", sender="s", subject="x")
    assert candidate.attachments == ()


def test_candidate_is_immutable() -> None:
    candidate = Candidate(message_id="m1", thread_id="t1", sender="s", subject="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.subject = "changed"  # type: ignore[misc]


def test_candidate_coerces_list_attachments_to_tuple_for_hashability() -> None:
    candidate = Candidate(
        message_id="m1",
        thread_id="t1",
        sender="s",
        subject="x",
        attachments=[Attachment("a.pdf", "application/pdf")],  # type: ignore[arg-type]
        label_names={"Kibit/AwaitingTIG"},  # type: ignore[arg-type]
    )
    assert isinstance(candidate.attachments, tuple)
    assert candidate.label_names == frozenset({"Kibit/AwaitingTIG"})
    hash(candidate)


# --- LineItem / InvoiceExtraction ------------------------------------------


def test_line_item_uses_decimal_for_money_and_quantity() -> None:
    item = LineItem(
        description="Consulting",
        quantity=Decimal("10"),
        unit_price=Decimal("12500.50"),
        net=Decimal("125005.00"),
    )
    assert item.unit_price == Decimal("12500.50")
    assert item == LineItem("Consulting", Decimal("10"), Decimal("12500.50"), Decimal("125005.00"))


def test_line_item_rejects_float_money() -> None:
    with pytest.raises(TypeError, match="unit_price"):
        LineItem(description="x", quantity=Decimal("1"), unit_price=0.1, net=None)  # type: ignore[arg-type]


def test_line_item_allows_missing_values_as_none() -> None:
    item = LineItem(description=None, quantity=None, unit_price=None, net=None)
    assert item.net is None


def test_invoice_extraction_defaults_every_field_to_none_never_fabricated() -> None:
    extraction = InvoiceExtraction()
    for field in dataclasses.fields(InvoiceExtraction):
        if field.name == "line_items":
            continue
        assert getattr(extraction, field.name) is None, field.name
    assert extraction.line_items == ()


def test_invoice_extraction_field_list_matches_design() -> None:
    names = [field.name for field in dataclasses.fields(InvoiceExtraction)]
    assert names == [
        "supplier",
        "invoice_number",
        "currency",
        "net",
        "gross",
        "due_date",
        "issue_date",
        "supply_date",
        "performance_date",
        "line_items",
        "supplier_country",
    ]


def test_invoice_extraction_keeps_raw_printed_values_and_equality() -> None:
    items = (LineItem("Hosting", Decimal("1"), Decimal("1234.56"), Decimal("1234.56")),)
    first = InvoiceExtraction(
        supplier="Kővári Kft",
        invoice_number="INV-001",
        currency="Ft",
        net="1.234,56",
        gross="1 567,89",
        due_date="2024.05.31",
        issue_date="2024.05.01",
        supply_date=None,
        performance_date="2024.05.15",
        line_items=items,
    )
    second = dataclasses.replace(first)
    assert first == second
    assert first.net == "1.234,56"
    assert first.line_items[0].net == Decimal("1234.56")


def test_invoice_extraction_coerces_line_items_to_tuple() -> None:
    extraction = InvoiceExtraction(line_items=[LineItem(None, None, None, None)])  # type: ignore[arg-type]
    assert isinstance(extraction.line_items, tuple)
    hash(extraction)


# --- RouteDecision ---------------------------------------------------------


def test_route_decision_ambiguous_maps_to_ambiguous_route_flag() -> None:
    decision = RouteDecision(route=Route.AMBIGUOUS)
    assert decision.is_ambiguous
    assert decision.flag_reason is FlagReason.AMBIGUOUS_ROUTE


@pytest.mark.parametrize("route", [Route.DIRECT, Route.TIG])
def test_route_decision_confident_route_has_no_flag(route: Route) -> None:
    decision = RouteDecision(route=route, matched_on="sender")
    assert not decision.is_ambiguous
    assert decision.flag_reason is None
    assert decision == RouteDecision(route=route, matched_on="sender")


# --- ComparisonResult ------------------------------------------------------


def _quantity_discrepancy() -> Discrepancy:
    return Discrepancy(
        field=DiscrepancyField.QUANTITY,
        invoice_value=Decimal("12"),
        tig_value=Decimal("10"),
        line_index=1,
        line_description="Development hours",
    )


def test_comparison_result_match_has_no_discrepancies() -> None:
    result = ComparisonResult.match()
    assert result.outcome is ComparisonOutcome.MATCH
    assert result.discrepancies == ()
    assert not result.is_mismatch


def test_comparison_result_mismatch_carries_attributed_discrepancies() -> None:
    discrepancy = _quantity_discrepancy()
    result = ComparisonResult.mismatch([discrepancy])
    assert result.is_mismatch
    assert result.discrepancies == (discrepancy,)
    assert result.discrepancies[0].line_index == 1
    assert result == ComparisonResult(ComparisonOutcome.MISMATCH, (discrepancy,))


def test_total_net_discrepancy_has_no_line_index() -> None:
    discrepancy = Discrepancy(
        field=DiscrepancyField.TOTAL_NET,
        invoice_value=Decimal("100.02"),
        tig_value=Decimal("100.00"),
    )
    assert discrepancy.line_index is None
    assert discrepancy.line_description is None


def test_discrepancy_rejects_float_values() -> None:
    with pytest.raises(TypeError, match="invoice_value"):
        Discrepancy(field=DiscrepancyField.TOTAL_NET, invoice_value=1.0, tig_value=None)  # type: ignore[arg-type]


def test_mismatch_without_discrepancies_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one discrepancy"):
        ComparisonResult(outcome=ComparisonOutcome.MISMATCH)


def test_match_with_discrepancies_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not carry discrepancies"):
        ComparisonResult(outcome=ComparisonOutcome.MATCH, discrepancies=(_quantity_discrepancy(),))


# --- StageResult -----------------------------------------------------------


def test_stage_result_ok_carries_value() -> None:
    result = StageResult.ok(42)
    assert result.status is StageStatus.OK
    assert result.value == 42
    assert result.is_ok
    assert result.flag_reason is None
    assert result.error is None


def test_stage_result_incomplete_carries_flag_reason() -> None:
    result: StageResult[int] = StageResult.incomplete(FlagReason.INCOMPLETE_DATA, "due_date")
    assert result.status is StageStatus.INCOMPLETE
    assert result.flag_reason is FlagReason.INCOMPLETE_DATA
    assert result.detail == "due_date"
    assert result.value is None
    assert not result.is_ok


def test_stage_result_error_carries_message() -> None:
    result: StageResult[int] = StageResult.failed("Drive API 503")
    assert result.status is StageStatus.ERROR
    assert result.error == "Drive API 503"
    assert not result.is_ok


def test_stage_result_equality() -> None:
    assert StageResult.ok("x") == StageResult.ok("x")
    assert StageResult.ok("x") != StageResult.ok("y")


def test_incomplete_stage_result_requires_flag_reason() -> None:
    with pytest.raises(ValueError, match="flag_reason"):
        StageResult(status=StageStatus.INCOMPLETE)


def test_error_stage_result_requires_message() -> None:
    with pytest.raises(ValueError, match="error message"):
        StageResult(status=StageStatus.ERROR)


def test_ok_stage_result_rejects_flag_or_error() -> None:
    with pytest.raises(ValueError, match="ok"):
        StageResult(status=StageStatus.OK, value=1, flag_reason=FlagReason.MISSING_TIG)
    with pytest.raises(ValueError, match="ok"):
        StageResult(status=StageStatus.OK, value=1, error="boom")
