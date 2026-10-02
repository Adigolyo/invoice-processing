"""Invoice email route classification: direct vs. TIG vs. ambiguous (USR-001-02).

``classify`` is a pure, deterministic function of ``(sender, subject, config)``: it reads
nothing but its arguments, keeps no state and has no side effects, so the same inputs
always yield the same ``RouteDecision`` (AC6). Recording that a candidate has already been
classified, so it is never routed twice (AC5), is the orchestrator's job, not this
module's.

Decision order:

1. **Sender is a recognised contractor -> TIG** (AC1). The sender is the primary signal
   and wins regardless of subject wording.
2. **Sender is a recognised non-contractor -> DIRECT** (AC2). The subject is not consulted.
3. **Sender is inconclusive** -> the subject disambiguates (AC3): a configured TIG subject
   indicator -> TIG, otherwise DIRECT.
4. **Sender inconclusive and the subject carries no content at all -> AMBIGUOUS** (AC4):
   neither signal can support a decision, so no route is guessed.

Interpretation of "inconclusive" (the specification does not define it, and the
``Config`` schema has no list of known suppliers or shared mailboxes): a sender is
*recognised* whenever it yields a single well-formed email address, and *inconclusive*
only when no such address can be extracted (missing/blank ``From``, malformed address,
empty angle brackets). A well-formed address that does not match the contractor list is
a recognised non-contractor, which is AC2's literal wording ("a sender not matching the
contractor identifier list"). An inconclusive sender with a non-blank subject that lacks
any indicator is DIRECT, which is AC3's literal "otherwise ... direct"; AC4's AMBIGUOUS is
therefore reserved for an inconclusive sender combined with a blank subject.

Matching rules:

- Contractor identifiers (``Config.contractor_identifiers``) are compared
  case-insensitively against the *address* only (never the display name). An identifier
  containing a local part (``dev@contractor.example``) must equal the whole address; a
  bare domain (``contractor.example``) or ``@contractor.example`` matches any address at
  exactly that domain (subdomains are not included). The first configured identifier
  that matches is reported.
- TIG subject indicators (``Config.tig_subject_indicators``) are matched as whole terms,
  case-insensitively and independent of Unicode normalisation form, so ``TIG`` matches
  ``"TIG 2024/05"`` but not ``"contiguous"``. The first configured indicator found is
  reported.
"""

from __future__ import annotations

import re
import unicodedata
from email.utils import parseaddr

from intake.config import Config
from intake.models import Route, RouteDecision

MATCHED_ON_SENDER = "sender"
MATCHED_ON_SUBJECT = "subject"


def classify(sender: str | None, subject: str | None, config: Config) -> RouteDecision:
    """Classify one candidate email into the direct, TIG or ambiguous route.

    ``matched_on`` records which signal decided the route:
    ``"sender:<identifier>"`` (contractor match, TIG), ``"sender"`` (recognised
    non-contractor, DIRECT), ``"subject:<indicator>"`` (inconclusive sender, TIG),
    ``"subject"`` (inconclusive sender, no indicator, DIRECT), or ``None`` (AMBIGUOUS).
    """
    address = _sender_address(sender)
    if address is not None:
        identifier = _matching_contractor(address, config.contractor_identifiers)
        if identifier is not None:
            return RouteDecision(Route.TIG, matched_on=f"{MATCHED_ON_SENDER}:{identifier}")
        return RouteDecision(Route.DIRECT, matched_on=MATCHED_ON_SENDER)

    normalised_subject = _normalise(subject or "").strip()
    if not normalised_subject:
        return RouteDecision(Route.AMBIGUOUS)

    indicator = _matching_indicator(normalised_subject, config.tig_subject_indicators)
    if indicator is not None:
        return RouteDecision(Route.TIG, matched_on=f"{MATCHED_ON_SUBJECT}:{indicator}")
    return RouteDecision(Route.DIRECT, matched_on=MATCHED_ON_SUBJECT)


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _sender_address(sender: str | None) -> str | None:
    """The sender's single well-formed email address, lower-cased, or ``None``."""
    if sender is None or not sender.strip():
        return None
    _, address = parseaddr(sender)
    address = address.strip()
    local, at, domain = address.partition("@")
    if not at or not local or not domain or "@" in domain:
        return None
    return _normalise(address)


def _matching_contractor(address: str, identifiers: tuple[str, ...]) -> str | None:
    domain = address.rpartition("@")[2]
    for identifier in identifiers:
        wanted = _normalise(identifier.strip())
        local, at, wanted_domain = wanted.rpartition("@")
        if at and local:
            if address == wanted:
                return identifier
        elif domain == (wanted_domain if at else wanted):
            return identifier
    return None


def _matching_indicator(subject: str, indicators: tuple[str, ...]) -> str | None:
    for indicator in indicators:
        term = _normalise(indicator.strip())
        if term and _term_pattern(term).search(subject):
            return indicator
    return None


def _term_pattern(term: str) -> re.Pattern[str]:
    # Only require a word boundary on a side where the term itself ends in a word
    # character, so indicators like "PRJ-" still match "PRJ-0042".
    prefix = r"(?<!\w)" if re.match(r"\w", term[0]) else ""
    suffix = r"(?!\w)" if re.match(r"\w", term[-1]) else ""
    return re.compile(prefix + re.escape(term) + suffix)


__all__ = ["classify"]
