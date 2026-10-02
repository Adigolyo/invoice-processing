"""Task 10 / USR-001-02: invoice email route classification (direct vs. TIG vs. ambiguous)."""

from typing import Any

import pytest

from intake.config import Config
from intake.models import FlagReason, Route, RouteDecision
from intake.routing import classify as classify_from_package
from intake.routing.classifier import classify


def _config(**overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "invoice_keywords": ["számla", "invoice"],
        "attachment_mime_allowlist": ["application/pdf", "image/jpeg", "image/png"],
        "contractor_identifiers": ["dev@contractor.example", "buildco.example"],
        "tig_subject_indicators": ["TIG", "teljesítésigazolás", "PRJ-"],
        "currency_map": {"Ft": "HUF", "€": "EUR", "$": "USD"},
        "label_processed": "Kibit/Processed",
        "label_pending": "Kibit/Pending",
        "label_needs_review": "Kibit/NeedsReview",
        "label_awaiting_tig": "Kibit/AwaitingTIG",
        "sequence_start": 1,
        "sequence_width": 3,
        "rounding_tolerance": "0.01",
        "drive_root_folder_id": "folder-123",
    }
    raw.update(overrides)
    return Config.from_mapping(raw)


@pytest.fixture
def config() -> Config:
    return _config()


def test_classify_is_exported_from_routing_package() -> None:
    assert classify_from_package is classify


# --- AC1: recognised contractor sender -> TIG ------------------------------


@pytest.mark.parametrize(
    "sender",
    [
        "dev@contractor.example",
        "Dev Team <dev@contractor.example>",
        "DEV@Contractor.EXAMPLE",
        '"Contractor, Dev" <dev@contractor.example>',
        "  dev@contractor.example  ",
    ],
)
def test_contractor_address_routes_to_tig(config: Config, sender: str) -> None:
    decision = classify(sender, "Számla 2024/05", config)

    assert decision.route is Route.TIG
    assert decision.matched_on == "sender:dev@contractor.example"


@pytest.mark.parametrize("sender", ["billing@buildco.example", "Acc <ACCOUNTS@BuildCo.Example>"])
def test_contractor_domain_identifier_matches_any_address_at_that_domain(
    config: Config, sender: str
) -> None:
    decision = classify(sender, "Invoice", config)

    assert decision.route is Route.TIG
    assert decision.matched_on == "sender:buildco.example"


def test_domain_identifier_with_leading_at_sign_matches_domain() -> None:
    config = _config(contractor_identifiers=["@buildco.example"])

    decision = classify("anyone@buildco.example", "Invoice", config)

    assert decision.route is Route.TIG
    assert decision.matched_on == "sender:@buildco.example"


@pytest.mark.parametrize(
    "subject", ["Invoice", "", "   ", "Supplier invoice, nothing to do with TIG?"]
)
def test_contractor_sender_wins_regardless_of_subject(config: Config, subject: str) -> None:
    decision = classify("dev@contractor.example", subject, config)

    assert decision.route is Route.TIG
    assert not decision.is_ambiguous


# --- AC2: recognised non-contractor sender -> DIRECT -----------------------


@pytest.mark.parametrize(
    "sender",
    [
        "billing@supplier.example",
        "Supplier Kft <szamla@supplier.hu>",
        # Same domain as a contractor *address* identifier, but not that address.
        "other@contractor.example",
        # Subdomains and look-alike domains are not the configured domain.
        "x@mail.buildco.example",
        "x@notbuildco.example",
        "x@buildco.example.evil",
        # The identifier appearing only in the display name is not a sender match.
        '"dev@contractor.example" <scam@phish.example>',
    ],
)
def test_non_contractor_sender_routes_direct(config: Config, sender: str) -> None:
    decision = classify(sender, "Számla 2024/05", config)

    assert decision.route is Route.DIRECT
    assert decision.matched_on == "sender"


def test_contractor_identifier_in_malformed_header_never_routes_tig(config: Config) -> None:
    # An unquoted "@" in the display name makes the header malformed: the sender is
    # inconclusive, so the subject decides; the display name is never a contractor match.
    decision = classify("dev@contractor.example <scam@phish.example>", "Számla", config)

    assert decision.route is Route.DIRECT
    assert decision.matched_on == "subject"


def test_non_contractor_sender_routes_direct_even_with_tig_indicator_in_subject(
    config: Config,
) -> None:
    # The subject is only a secondary signal for an *inconclusive* sender (AC3);
    # a recognised non-contractor sender is decided by the sender alone (AC2).
    decision = classify("billing@supplier.example", "TIG 2024/05 számla", config)

    assert decision.route is Route.DIRECT
    assert decision.matched_on == "sender"


# --- AC3: inconclusive sender -> subject indicator disambiguates -----------


@pytest.mark.parametrize(
    ("subject", "indicator"),
    [
        ("TIG 2024/05 - számla", "TIG"),
        ("Re: tig attached", "TIG"),
        ("Számla a teljesítésigazolás alapján", "teljesítésigazolás"),
        ("SZÁMLA - TELJESÍTÉSIGAZOLÁS", "teljesítésigazolás"),
        ("Invoice for PRJ-0042", "PRJ-"),
    ],
)
@pytest.mark.parametrize("sender", ["", "   ", "not-an-address", "Name Only <>", "a@b@c"])
def test_inconclusive_sender_with_tig_subject_indicator_routes_tig(
    config: Config, sender: str, subject: str, indicator: str
) -> None:
    decision = classify(sender, subject, config)

    assert decision.route is Route.TIG
    assert decision.matched_on == f"subject:{indicator}"


def test_subject_indicator_matching_is_unicode_normalisation_insensitive(
    config: Config,
) -> None:
    decomposed = "teljesítésigazolás"  # NFD form of the indicator
    decision = classify("", f"Számla - {decomposed}", config)

    assert decision.route is Route.TIG


@pytest.mark.parametrize(
    "subject",
    [
        "Számla 2024/05",
        "Invoice for contiguous services",  # "tig" only inside a word
        "Prestige invoice",  # "tig" only inside a word
    ],
)
def test_inconclusive_sender_without_indicator_routes_direct(config: Config, subject: str) -> None:
    decision = classify("not-an-address", subject, config)

    assert decision.route is Route.DIRECT
    assert decision.matched_on == "subject"


# --- AC4: neither signal confident -> AMBIGUOUS, never guessed -------------


@pytest.mark.parametrize("sender", ["", "   ", "not-an-address", None])
@pytest.mark.parametrize("subject", ["", "   \t", None])
def test_inconclusive_sender_and_empty_subject_is_ambiguous(
    config: Config, sender: str | None, subject: str | None
) -> None:
    decision = classify(sender, subject, config)

    assert decision == RouteDecision(route=Route.AMBIGUOUS, matched_on=None)
    assert decision.is_ambiguous
    assert decision.flag_reason is FlagReason.AMBIGUOUS_ROUTE


def test_recognised_non_contractor_sender_with_empty_subject_still_routes_direct(
    config: Config,
) -> None:
    decision = classify("billing@supplier.example", "", config)

    assert decision.route is Route.DIRECT


# --- Config-driven, not hardcoded ------------------------------------------


def test_contractor_list_comes_from_config() -> None:
    sender = "billing@supplier.example"
    assert classify(sender, "Invoice", _config()).route is Route.DIRECT

    reconfigured = _config(contractor_identifiers=["billing@supplier.example"])
    assert classify(sender, "Invoice", reconfigured).route is Route.TIG


def test_subject_indicators_come_from_config() -> None:
    subject = "Invoice - project ref KB-7"
    assert classify("", subject, _config()).route is Route.DIRECT

    reconfigured = _config(tig_subject_indicators=["project ref"])
    decision = classify("", subject, reconfigured)
    assert decision.route is Route.TIG
    assert decision.matched_on == "subject:project ref"


def test_first_configured_identifier_wins_when_several_match() -> None:
    config = _config(contractor_identifiers=["contractor.example", "dev@contractor.example"])

    decision = classify("dev@contractor.example", "", config)

    assert decision.matched_on == "sender:contractor.example"


# --- AC6: deterministic; pure (AC5 support: no hidden state) ---------------


@pytest.mark.parametrize(
    ("sender", "subject"),
    [
        ("dev@contractor.example", "Invoice"),
        ("billing@supplier.example", "TIG"),
        ("", "TIG 05"),
        ("", "Számla"),
        ("", ""),
    ],
)
def test_classification_is_deterministic(config: Config, sender: str, subject: str) -> None:
    first = classify(sender, subject, config)

    results = {classify(sender, subject, config) for _ in range(100)}

    assert results == {first}


def test_classification_does_not_depend_on_call_history(config: Config) -> None:
    baseline = classify("", "Számla", config)
    classify("dev@contractor.example", "TIG", config)
    classify("", "", config)

    assert classify("", "Számla", config) == baseline


def test_classify_returns_route_decision(config: Config) -> None:
    assert isinstance(classify("dev@contractor.example", "x", config), RouteDecision)
