"""Task 19 / USR-005-02: Gmail draft reply on a TIG mismatch.

Fakes only, no network: a ``FakeGmail`` for the step's own contract, and the real
``GmailClient`` over a MagicMock ``googleapiclient`` service to prove the draft is created
(never sent) on the invoice's thread and is detected on re-evaluation.
"""

from __future__ import annotations

import base64
import email
import json
from decimal import Decimal
from email import policy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients.gmail_client import (
    GmailClient,
    GmailReplyError,
    GmailResponseError,
    Thread,
    ThreadMessage,
)
from intake.config import LabelNames
from intake.models import (
    ComparisonResult,
    Discrepancy,
    DiscrepancyField,
    InvoiceExtraction,
    LineItem,
)
from intake.normalization.amounts import format_hungarian_amount
from intake.reconciliation.draft_reply import (
    DRAFT_LABEL,
    DraftReplyResult,
    DraftReplyStatus,
    compose_discrepancy_body,
    draft_reply_on_mismatch,
    find_existing_draft,
)
from intake.reconciliation.tig_matcher import TigDocument

ANSWER_KEY = Path(__file__).resolve().parents[2] / "fixtures" / "tig_pairs" / "answer_key.json"
D = Decimal
NBSP = " "
CONTRACTOR = "C Kft. <szamla@c-kft.example>"
THREAD_ID = "t-1"


# --- builders ---------------------------------------------------------------------------------


def _hu(value: str) -> str:
    return format_hungarian_amount(D(value))


def _line(description: str, quantity: str, unit_price: str) -> LineItem:
    return LineItem(description, D(quantity), D(unit_price), D(quantity) * D(unit_price))


def _invoice(lines: int = 2, number: str | None = "INV-C-2026-02") -> InvoiceExtraction:
    return InvoiceExtraction(
        supplier="C Kft.",
        invoice_number=number,
        currency="EUR",
        net="1 073,00 EUR",
        line_items=tuple(_line(f"Tétel {i + 1}", "1", "10") for i in range(lines)),
    )


def _tig(lines: int = 2, number: str | None = "TIG-2026-09-KEK-C") -> TigDocument:
    return TigDocument(
        message_id="m-tig",
        filename="TIG-2026-09-KEK-C.pdf",
        lines=tuple(_line(f"Tétel {i + 1}", "1", "10") for i in range(lines)),
        total_net=D("989.00"),
        currency="EUR",
        number=number,
    )


UNIT_PRICE_LINE_3 = Discrepancy(
    DiscrepancyField.UNIT_PRICE,
    invoice_value=D("16.50"),
    tig_value=D("14.50"),
    line_index=2,
    line_description="Grafikai tervezés – Kék projekt, 2026. szeptember",
)
TOTAL_NET = Discrepancy(DiscrepancyField.TOTAL_NET, D("1073.00"), D("989.00"))
MISMATCH = ComparisonResult.mismatch([UNIT_PRICE_LINE_3, TOTAL_NET])


def _thread_message(
    message_id: str,
    *labels: str,
    sender: str = CONTRACTOR,
    internal_date: int = 0,
) -> ThreadMessage:
    return ThreadMessage(
        message_id=message_id,
        thread_id=THREAD_ID,
        sender=sender,
        subject="Számla INV-C-2026-02",
        rfc822_message_id=f"<{message_id}@mail.example>",
        references=None,
        internal_date=internal_date,
        label_names=frozenset(labels),
    )


class FakeGmail:
    """Serves one thread; ``create_draft_reply`` adds a DRAFT message to it, like Gmail."""

    def __init__(
        self,
        *messages: ThreadMessage,
        create_error: Exception | None = None,
        thread_error: Exception | None = None,
    ) -> None:
        self.messages = list(messages) or [_thread_message("m-inv", "INBOX", "UNREAD")]
        self.create_error = create_error
        self.thread_error = thread_error
        self.calls: list[str] = []
        self.drafts: list[tuple[str, str]] = []

    def get_thread(self, thread_id: str) -> Thread:
        self.calls.append("get_thread")
        if self.thread_error is not None:
            raise self.thread_error
        assert thread_id == THREAD_ID
        return Thread(thread_id=thread_id, messages=tuple(self.messages))

    def create_draft_reply(self, thread_id: str, body: str) -> str:
        self.calls.append("create_draft_reply")
        if self.create_error is not None:
            raise self.create_error
        self.drafts.append((thread_id, body))
        draft_id = f"r-{len(self.drafts)}"
        self.messages.append(
            _thread_message(f"m-draft-{len(self.drafts)}", DRAFT_LABEL, sender="me@kibit.example")
        )
        return draft_id

    # Only the orchestrator may label / mark read; nothing here may send.
    def apply_label(self, *_: Any) -> None:
        raise AssertionError("draft_reply must not call apply_label")

    def mark_read(self, *_: Any) -> None:
        raise AssertionError("draft_reply must not call mark_read")

    def send(self, *_: Any) -> None:
        raise AssertionError("draft_reply must never send")


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _run(gmail: FakeGmail, comparison: ComparisonResult = MISMATCH) -> DraftReplyResult:
    return draft_reply_on_mismatch(
        gmail, THREAD_ID, comparison, invoice=_invoice(3), tig=_tig(3), currency_iso="EUR"
    )


def _body(*discrepancies: Discrepancy, **kwargs: Any) -> str:
    kwargs.setdefault("currency", "EUR")
    kwargs.setdefault("invoice_number", "INV-C-2026-02")
    kwargs.setdefault("tig_number", "TIG-2026-09-KEK-C")
    return compose_discrepancy_body(discrepancies, **kwargs)


def _line_of(body: str, needle: str) -> str:
    matches = [line for line in body.splitlines() if needle in line]
    assert len(matches) == 1, (needle, body)
    return matches[0]


# --- AC2: composing the body ---------------------------------------------------------------


def test_ac2_unit_price_discrepancy_names_line_field_and_both_values() -> None:
    body = _body(UNIT_PRICE_LINE_3)
    line = _line_of(body, "3. tétel")
    assert "Grafikai tervezés – Kék projekt, 2026. szeptember" in line
    assert "egységár" in line
    assert "a számlán 16,50 EUR" in line
    assert "a teljesítésigazoláson 14,50 EUR" in line
    assert line.index("16,50") < line.index("14,50")  # invoice value first, then TIG


def test_ac2_line_position_is_one_based() -> None:
    first = Discrepancy(DiscrepancyField.QUANTITY, D("12"), D("11"), 0, "Fejlesztés")
    assert "1. tétel (Fejlesztés)" in _body(first)
    assert "0. tétel" not in _body(first)


def test_ac2_quantity_discrepancy_has_no_currency_and_no_trailing_zeros() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, D("12.00"), D("0.40"), 0, "Óradíj")
    line = _line_of(_body(discrepancy), "1. tétel")
    assert "mennyiség" in line
    assert "12,00" not in line and "0,40" not in line
    assert "a számlán 12, a teljesítésigazoláson 0,4" in line
    assert "EUR" not in line


def test_quantity_thousands_use_hungarian_grouping() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, D("1430"), D("1.3E+3"), 0, "Szórólap")
    line = _line_of(_body(discrepancy), "1. tétel")
    assert f"1{NBSP}430" in line
    assert f"1{NBSP}300" in line


def test_ac2_total_net_discrepancy_keeps_amount_precision_and_currency() -> None:
    line = _line_of(_body(TOTAL_NET), "Nettó végösszeg")
    assert f"a számlán 1{NBSP}073,00 EUR" in line
    assert "a teljesítésigazoláson 989,00 EUR" in line
    assert "tétel" not in line


def test_huf_amounts_are_rendered_with_hungarian_grouping() -> None:
    discrepancy = Discrepancy(DiscrepancyField.UNIT_PRICE, D("95000"), D("83000"), 0, "Hosting")
    line = _line_of(_body(discrepancy, currency="HUF"), "1. tétel")
    assert f"95{NBSP}000 HUF" in line
    assert f"83{NBSP}000 HUF" in line


def test_every_discrepancy_is_listed_in_order() -> None:
    body = _body(UNIT_PRICE_LINE_3, TOTAL_NET)
    assert body.index("3. tétel") < body.index("Nettó végösszeg")
    assert len([line for line in body.splitlines() if line.startswith("- ")]) == 2


def test_body_references_the_invoice_and_tig_numbers() -> None:
    body = _body(TOTAL_NET)
    assert "INV-C-2026-02 számú számlát" in body
    assert "TIG-2026-09-KEK-C számú teljesítésigazolással" in body


def test_missing_document_numbers_are_omitted_not_invented() -> None:
    body = _body(TOTAL_NET, invoice_number=None, tig_number="  ")
    assert "A számlát összevetettük a teljesítésigazolással" in body
    assert "számú" not in body
    assert "None" not in body


def test_unreadable_invoice_value_is_stated_explicitly() -> None:
    discrepancy = Discrepancy(DiscrepancyField.UNIT_PRICE, None, D("14.50"), 1, "Grafika")
    line = _line_of(_body(discrepancy, invoice_line_count=2, tig_line_count=2), "2. tétel")
    assert "a számlán nem olvasható" in line
    assert "a teljesítésigazoláson 14,50 EUR" in line
    assert "None" not in line


def test_unreadable_invoice_total_is_stated_explicitly() -> None:
    discrepancy = Discrepancy(DiscrepancyField.TOTAL_NET, None, D("989.00"))
    line = _line_of(_body(discrepancy), "Nettó végösszeg")
    assert "a számlán nem olvasható" in line


def test_extra_invoice_line_missing_from_tig_is_stated_explicitly() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, D("2"), None, 2, "Extra díj")
    line = _line_of(_body(discrepancy, invoice_line_count=3, tig_line_count=2), "3. tétel")
    assert "a számlán 2" in line
    assert "a teljesítésigazoláson nem szerepel (hiányzó tétel)" in line


def test_tig_line_missing_from_invoice_is_stated_explicitly() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, None, D("5"), 1, "Tesztelés")
    line = _line_of(_body(discrepancy, invoice_line_count=1, tig_line_count=2), "2. tétel")
    assert "a számlán nem szerepel (hiányzó tétel)" in line
    assert "a teljesítésigazoláson 5" in line


def test_line_without_description_is_named_by_position_only() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, D("2"), D("1"), 0, None)
    line = _line_of(_body(discrepancy), "1. tétel")
    assert "1. tétel, mennyiség" in line


def test_multiline_description_is_flattened_to_one_line() -> None:
    discrepancy = Discrepancy(DiscrepancyField.QUANTITY, D("2"), D("1"), 0, " Sor\n  egy\tkettő ")
    assert "1. tétel (Sor egy kettő)" in _body(discrepancy)


def test_amount_without_known_currency_is_rendered_without_a_unit() -> None:
    line = _line_of(_body(TOTAL_NET, currency=None), "Nettó végösszeg")
    assert line.endswith("a teljesítésigazoláson 989,00")


def test_body_is_plain_factual_text_without_persuasive_language() -> None:
    body = _body(UNIT_PRICE_LINE_3, TOTAL_NET).casefold()
    for phrase in ("sürgős", "haladéktalanul", "azonnal", "kötbér", "felszólít", "jogi", "!!"):
        assert phrase not in body
    assert "<" not in body and "*" not in body  # plain text, no HTML/markdown


def test_empty_discrepancies_are_rejected() -> None:
    with pytest.raises(ValueError):
        compose_discrepancy_body([], currency="EUR")


def test_composition_is_deterministic() -> None:
    assert _body(UNIT_PRICE_LINE_3, TOTAL_NET) == _body(UNIT_PRICE_LINE_3, TOTAL_NET)


# --- AC2 against the fixture answer key ----------------------------------------------------


def _mismatch_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["pairs"]
    return [p for p in pairs if p["expected_outcome"] == "mismatch"]


def test_answer_key_has_seven_mismatch_pairs() -> None:
    assert len(_mismatch_pairs()) == 7


@pytest.mark.parametrize("pair", _mismatch_pairs(), ids=lambda p: p["pair"])
def test_body_states_every_answer_key_discrepancy(pair: dict[str, Any]) -> None:
    invoice, tig = pair["invoice"], pair["tig"]
    discrepancies = [
        Discrepancy(
            DiscrepancyField(d["field"]),
            D(d["invoice_value"]),
            D(d["tig_value"]),
            d["line_index"],
            None if d["line_index"] is None else invoice["lines"][d["line_index"]]["description"],
        )
        for d in pair["discrepancies"]
    ]
    body = compose_discrepancy_body(
        discrepancies,
        currency=invoice["currency"],
        invoice_number=invoice["invoice_number"],
        tig_number=tig["number"],
        invoice_line_count=len(invoice["lines"]),
        tig_line_count=len(tig["lines"]),
    )
    assert invoice["invoice_number"] in body
    assert tig["number"] in body
    for d, discrepancy in zip(pair["discrepancies"], discrepancies, strict=True):
        if d["line_index"] is None:
            line = _line_of(body, "Nettó végösszeg")
        else:
            line = _line_of(body, f"{d['line_index'] + 1}. tétel")
            assert discrepancy.line_description in line
        if d["field"] == "quantity":
            assert "mennyiség" in line
            invoice_text = _hu(d["invoice_value"])
            tig_text = _hu(d["tig_value"])
        else:
            assert ("egységár" if d["field"] == "unit_price" else "Nettó végösszeg") in line
            invoice_text = f"{_hu(d['invoice_value'])} {invoice['currency']}"
            tig_text = f"{_hu(d['tig_value'])} {invoice['currency']}"
        assert f"a számlán {invoice_text}" in line
        assert f"a teljesítésigazoláson {tig_text}" in line


# --- AC1 / AC4: drafting only on a mismatch ------------------------------------------------


def test_ac4_match_creates_no_draft_and_touches_no_gmail_api() -> None:
    gmail = FakeGmail()
    result = _run(gmail, ComparisonResult.match())
    assert result.status is DraftReplyStatus.NOT_NEEDED
    assert gmail.calls == []
    assert not result.mismatch_outstanding
    assert not result.draft_missing


def test_ac1_mismatch_creates_one_draft_on_the_thread_with_the_composed_body() -> None:
    gmail = FakeGmail()
    result = _run(gmail)
    assert result == DraftReplyResult(DraftReplyStatus.CREATED, draft_id="r-1")
    assert result.mismatch_outstanding and not result.draft_missing
    expected = compose_discrepancy_body(
        MISMATCH.discrepancies,
        currency="EUR",
        invoice_number="INV-C-2026-02",
        tig_number="TIG-2026-09-KEK-C",
        invoice_line_count=3,
        tig_line_count=3,
    )
    assert gmail.drafts == [(THREAD_ID, expected)]


def test_currency_falls_back_to_the_tig_currency_when_unresolved() -> None:
    gmail = FakeGmail()
    draft_reply_on_mismatch(
        gmail, THREAD_ID, MISMATCH, invoice=_invoice(3), tig=_tig(3), currency_iso=None
    )
    ((_, body),) = gmail.drafts
    assert "989,00 EUR" in body


# --- AC5: exactly one draft per invoice ----------------------------------------------------


def test_ac5_re_evaluating_a_drafted_mismatch_creates_no_second_draft() -> None:
    gmail = FakeGmail()
    first = _run(gmail)
    second = _run(gmail)
    assert first.status is DraftReplyStatus.CREATED
    assert second == DraftReplyResult(
        DraftReplyStatus.ALREADY_DRAFTED, existing_draft_message_id="m-draft-1"
    )
    assert len(gmail.drafts) == 1
    assert second.mismatch_outstanding and not second.draft_missing


def test_ac5_draft_left_by_an_interrupted_earlier_run_is_detected() -> None:
    gmail = FakeGmail(
        _thread_message("m-tig", "SENT", sender="pm@kibit.example"),
        _thread_message("m-inv", "INBOX", "UNREAD"),
        _thread_message("m-old-draft", DRAFT_LABEL, sender="me@kibit.example"),
    )
    result = _run(gmail)
    assert result.status is DraftReplyStatus.ALREADY_DRAFTED
    assert result.existing_draft_message_id == "m-old-draft"
    assert "create_draft_reply" not in gmail.calls


def test_sent_messages_are_not_mistaken_for_a_draft() -> None:
    thread = Thread(
        THREAD_ID,
        (
            _thread_message("m-tig", "SENT", sender="pm@kibit.example"),
            _thread_message("m-inv", "INBOX"),
        ),
    )
    assert find_existing_draft(thread) is None


def test_find_existing_draft_returns_the_latest_draft() -> None:
    thread = Thread(
        THREAD_ID,
        (
            _thread_message("m-inv", "INBOX"),
            _thread_message("d-1", DRAFT_LABEL),
            _thread_message("d-2", DRAFT_LABEL),
        ),
    )
    found = find_existing_draft(thread)
    assert found is not None and found.message_id == "d-2"


# --- AC3: never sent, never labelled ---------------------------------------------------------


def test_ac3_step_only_creates_a_draft_and_never_labels_marks_read_or_sends() -> None:
    gmail = FakeGmail()
    _run(gmail)
    _run(gmail)
    assert set(gmail.calls) <= {"get_thread", "create_draft_reply"}


# --- AC6: failures are surfaced, the mismatch stays outstanding --------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _http_error(503),
        GmailReplyError("no Message-ID"),
        GmailResponseError("drafts.create response is missing 'id'"),
        TimeoutError("timed out"),
    ],
    ids=["http-503", "reply-error", "response-error", "timeout"],
)
def test_ac6_draft_creation_failure_keeps_the_mismatch_outstanding(error: Exception) -> None:
    gmail = FakeGmail(create_error=error)
    result = _run(gmail)
    assert result.status is DraftReplyStatus.FAILED
    assert result.mismatch_outstanding  # orchestrator still labels Pending
    assert result.draft_missing
    assert result.error is not None and type(error).__name__ in result.error
    assert result.draft_id is None


def test_ac6_thread_read_failure_does_not_risk_a_duplicate_draft() -> None:
    gmail = FakeGmail(thread_error=_http_error(500))
    result = _run(gmail)
    assert result.status is DraftReplyStatus.FAILED
    assert result.mismatch_outstanding and result.draft_missing
    assert "create_draft_reply" not in gmail.calls


def test_ac6_failed_draft_is_created_when_retried() -> None:
    gmail = FakeGmail(create_error=_http_error(503))
    assert _run(gmail).status is DraftReplyStatus.FAILED
    gmail.create_error = None
    assert _run(gmail).status is DraftReplyStatus.CREATED
    assert len(gmail.drafts) == 1


def test_programming_errors_are_not_swallowed() -> None:
    gmail = FakeGmail(create_error=TypeError("bug"))
    with pytest.raises(TypeError):
        _run(gmail)


# --- result contract -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "valid"),
    [
        ({"status": DraftReplyStatus.NOT_NEEDED}, True),
        ({"status": DraftReplyStatus.NOT_NEEDED, "draft_id": "r-1"}, False),
        ({"status": DraftReplyStatus.CREATED}, False),
        ({"status": DraftReplyStatus.CREATED, "draft_id": "r-1"}, True),
        ({"status": DraftReplyStatus.CREATED, "draft_id": "r-1", "error": "x"}, False),
        ({"status": DraftReplyStatus.ALREADY_DRAFTED}, False),
        ({"status": DraftReplyStatus.ALREADY_DRAFTED, "existing_draft_message_id": "m"}, True),
        ({"status": DraftReplyStatus.FAILED}, False),
        ({"status": DraftReplyStatus.FAILED, "error": "boom"}, True),
        ({"status": DraftReplyStatus.FAILED, "error": "boom", "draft_id": "r-1"}, False),
    ],
)
def test_result_fields_are_consistent_with_the_status(kwargs: dict[str, Any], valid: bool) -> None:
    if valid:
        DraftReplyResult(**kwargs)
    else:
        with pytest.raises(ValueError):
            DraftReplyResult(**kwargs)


# --- with the real GmailClient (MagicMock googleapiclient service) -----------------------------


LABELS = LabelNames(
    processed="Kibit/Processed",
    pending="Kibit/Pending",
    needs_review="Kibit/NeedsReview",
    awaiting_tig="Kibit/AwaitingTIG",
)


def _api_message(message_id: str, label_ids: list[str], sender: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": THREAD_ID,
        "labelIds": label_ids,
        "internalDate": "1717000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": "Számla INV-C-2026-02"},
                {"name": "Message-ID", "value": f"<{message_id}@mail.example>"},
            ],
            "parts": [],
        },
    }


def _request(result: Any) -> MagicMock:
    request = MagicMock(name="request")
    request.execute.return_value = result
    return request


class FakeGmailService:
    """Enough of ``build("gmail", "v1")``: drafts.create adds a DRAFT message to the thread."""

    def __init__(self) -> None:
        self.service = MagicMock(name="gmail")
        users = self.service.users.return_value
        self.drafts = users.drafts.return_value
        self.messages = users.messages.return_value
        users.labels.return_value.list.return_value = _request(
            {"labels": [{"id": i, "name": i} for i in ("INBOX", "UNREAD", "SENT", "DRAFT")]}
        )
        self.thread = [
            _api_message("m-tig", ["SENT"], "PM <pm@kibit.example>"),
            _api_message("m-inv", ["INBOX", "UNREAD"], CONTRACTOR),
        ]
        users.threads.return_value.get.side_effect = lambda **_: _request(
            {"id": THREAD_ID, "messages": list(self.thread)}
        )
        self.drafts.create.side_effect = self._create

    def _create(self, **kwargs: Any) -> MagicMock:
        self.thread.append(_api_message("m-draft", ["DRAFT"], "Kibit <me@kibit.example>"))
        return _request({"id": "r-123", "message": {"id": "m-draft"}})


def test_real_client_creates_a_threaded_unsent_draft_to_the_contractor_exactly_once() -> None:
    fake = FakeGmailService()
    client = GmailClient(fake.service, LABELS)

    first = draft_reply_on_mismatch(
        client, THREAD_ID, MISMATCH, invoice=_invoice(3), tig=_tig(3), currency_iso="EUR"
    )
    second = draft_reply_on_mismatch(
        client, THREAD_ID, MISMATCH, invoice=_invoice(3), tig=_tig(3), currency_iso="EUR"
    )

    assert first == DraftReplyResult(DraftReplyStatus.CREATED, draft_id="r-123")
    assert second.status is DraftReplyStatus.ALREADY_DRAFTED
    assert fake.drafts.create.call_count == 1
    fake.drafts.send.assert_not_called()
    fake.messages.send.assert_not_called()
    fake.messages.modify.assert_not_called()

    body = fake.drafts.create.call_args.kwargs["body"]
    assert body["message"]["threadId"] == THREAD_ID
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    sent = email.message_from_bytes(raw, policy=policy.default)
    assert sent["To"] == CONTRACTOR
    assert sent["In-Reply-To"] == "<m-inv@mail.example>"
    text = sent.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
    assert "3. tétel" in text and "Nettó végösszeg" in text
