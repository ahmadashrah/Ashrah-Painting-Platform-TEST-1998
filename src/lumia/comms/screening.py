"""Content screening — what turns the approval levels into a mechanism.

The operating spec lists the things that must never leave the building
without a human: prices, change-order language, contractual commitments,
completion guarantees, admissions of fault, disputes, legal and insurance
matters, safety incidents, discipline and termination, refunds and
credits, serious complaints, and anything uncertain or contradictory.

A prompt instruction alone cannot enforce that — the model writes the
message and would also be the one deciding whether its own message is
risky. So the message body is screened here, in code, at send time, and a
hit escalates the send to Level 3 no matter what the agent believed it was
doing.

Screening is deliberately biased toward false positives. The cost of a
needless approval prompt is a few seconds of a manager's time; the cost of
a missed one is a price, a promise or an admission of liability sent to a
general contractor in Ashrah's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..domain.projects import RecipientRole, is_external

#: Where a category applies. "all" screens every message; "external" screens
#: only messages leaving the company — asking a crew lead to confirm which
#: coat went on is uncertain by design, and gating it would break the
#: clarification workflow the spec asks for.
ALL = "all"
EXTERNAL = "external"


#: Words that turn a hit into a routine all-clear rather than an event.
#: Only applied where the spec's intent is obviously about an occurrence —
#: "no injuries today" is a normal daily-log line, whereas "we are not
#: responsible" is still a liability statement and must never be excused.
NEGATIONS = ("no", "not", "zero", "nil", "without", "any")


@dataclass(frozen=True)
class Category:
    name: str
    scope: str
    guidance: str
    patterns: tuple[str, ...]
    #: Whether a preceding negation clears the hit.
    negatable: bool = False


CATEGORIES: tuple[Category, ...] = (
    Category(
        "pricing",
        ALL,
        "Price and cost information requires management approval before it is sent to anyone.",
        (
            r"\$\s?\d",
            r"\b\d+(?:\.\d{2})?\s?(?:dollars|cad|usd)\b",
            r"\bpric(?:e|es|ing)\b",
            r"\bcosts?\b",
            r"\bquot(?:e|es|ed|ation)\b",
            r"\binvoic(?:e|es|ing)\b",
            r"\bbillable\b",
            r"\bextra charge\b",
            r"\bsurcharge\b",
            r"\bper square (?:foot|metre|meter)\b",
            r"\bhourly rate\b",
        ),
    ),
    Category(
        "change_order",
        ALL,
        "Change-order language is contractual. Draft it, then get management approval.",
        (
            r"\bchange orders?\b",
            r"\bco\s?#\s?\d",
            r"\bextra work\b",
            r"\badditional scope\b",
            r"\bscope change\b",
            r"\bvariation order\b",
        ),
    ),
    Category(
        "contractual_commitment",
        ALL,
        "This reads as a contractual commitment on Ashrah's behalf.",
        (
            r"\bwe (?:guarantee|warrant|commit|undertake)\b",
            r"\bwe will (?:cover|absorb|pay|assume)\b",
            r"\bwarrant(?:y|ies)\b",
            r"\bbinding\b",
            r"\bwe agree to\b",
            r"\bcontractual(?:ly)?\b",
            r"\bas per the contract\b",
        ),
    ),
    Category(
        "completion_guarantee",
        ALL,
        "A completion claim needs verification and approval — a photo of partial work never proves it.",
        (
            r"\b100\s?%\s*(?:complete|completed|done|finished)\b",
            r"\bfully (?:complete|completed|finished)\b",
            r"\ball work is (?:complete|completed|done|finished)\b",
            r"\bentire(?:ly)? (?:complete|completed|finished)\b",
            r"\bguarantee[ds]? complet",
            r"\bsigned off\b",
        ),
    ),
    Category(
        "schedule_commitment",
        EXTERNAL,
        "A firm date given to a client can carry financial consequences. Hedge it or get approval.",
        (
            r"\bwe will (?:be )?(?:done|finished|complete[d]?)\s+(?:by|on)\b",
            r"\bno later than\b",
            r"\bguaranteed? (?:by|completion|date)\b",
            r"\bcompletion date\b",
            r"\bliquidated damages\b",
            r"\bon time, ?guaranteed\b",
        ),
    ),
    Category(
        "fault_admission",
        ALL,
        "An admission of fault or liability must never be sent without management approval.",
        (
            r"\bour (?:fault|mistake|error|negligence)\b",
            r"\bwe (?:damaged|caused|ruined|broke)\b",
            # Denying liability is as much a legal statement as admitting it.
            r"\bwe (?:are|were) (?:not )?(?:responsible|liable|at fault|to blame)\b",
            r"\bwe admit\b",
            r"\bliab(?:le|ility)\b",
            r"\bwe apologi[sz]e for the damage\b",
        ),
    ),
    Category(
        "dispute",
        ALL,
        "Dispute-related communication is escalation territory, not routine correspondence.",
        (
            r"\bdisputes?\b",
            r"\bback ?charges?\b",
            r"\bdeduction from\b",
            r"\bbreach of\b",
            r"\bdelay claim\b",
            r"\bnotice of (?:default|delay|claim)\b",
            r"\bwithhold(?:ing)? payment\b",
        ),
    ),
    Category(
        "legal_or_insurance",
        ALL,
        "Legal, insurance and regulatory matters go to management, never straight out.",
        (
            r"\blawyers?\b",
            r"\battorneys?\b",
            r"\blegal (?:action|counsel|advice|notice)\b",
            r"\blitigat(?:e|ion|ing)\b",
            r"\binsuran(?:ce|ce claim)\b",
            r"\bwcb\b",
            r"\bwsib\b",
            r"\boh&s\b",
            r"\bsubpoena\b",
        ),
    ),
    Category(
        "safety_incident",
        EXTERNAL,
        "A safety incident follows the escalation process. It is never an ordinary daily-log item.",
        (
            r"\binjur(?:y|ies|ed)\b",
            r"\baccidents?\b",
            r"\bambulance\b",
            r"\bhospital\b",
            r"\bnear ?miss\b",
            r"\bfell from\b",
            r"\bfall (?:from|off) (?:the )?(?:ladder|scaffold|lift)\b",
            r"\bemergency (?:services|response)\b",
            r"\bincident report\b",
        ),
        negatable=True,
    ),
    Category(
        "discipline",
        ALL,
        "Employee disciplinary content requires management approval.",
        (
            r"\bdisciplinar(?:y|ily)\b",
            r"\bwritten warning\b",
            r"\bwrite[- ]up\b",
            r"\bmisconduct\b",
            r"\bsuspend(?:ed|ing|sion)?\b",
            r"\bperformance issue\b",
        ),
    ),
    Category(
        "termination",
        ALL,
        "Termination notices require management approval without exception.",
        (
            r"\bterminat(?:e|ed|ing|ion)\b",
            r"\bdismiss(?:al|ed|ing)\b",
            r"\byou(?:'re| are) fired\b",
            r"\blay(?:ing)? ?off\b",
            r"\blaid off\b",
            r"\blet (?:you|him|her|them) go\b",
        ),
    ),
    Category(
        "refund_or_credit",
        ALL,
        "Refunds, credits and compensation are financial commitments.",
        (
            r"\brefunds?\b",
            r"\bcredit (?:note|memo|back|the)\b",
            r"\bcompensat(?:e|ion|ing)\b",
            r"\breimburse(?:ment)?\b",
            r"\bfree of charge\b",
            r"\bat no (?:cost|charge)\b",
            r"\bno additional cost\b",
        ),
    ),
    Category(
        "serious_complaint",
        ALL,
        "Messages involving serious complaints or hostility need a human before they go out.",
        (
            r"\bunacceptable\b",
            r"\bformal complaint\b",
            r"\bvery disappointed\b",
            r"\bthreat(?:en|ening|ened)\b",
            r"\bescalat(?:e|ing) (?:this )?to (?:your|the) (?:manager|head office|owner)\b",
            r"\bwalk off the (?:job|site)\b",
        ),
    ),
    Category(
        "uncertainty",
        EXTERNAL,
        "Uncertain or contradictory statements must be resolved before a client sees them.",
        (
            r"\bnot sure\b",
            r"\bi think\b",
            r"\bwe think\b",
            r"\bmaybe\b",
            r"\bunclear\b",
            r"\bunverified\b",
            r"\bassum(?:e|ed|ing|ption)\b",
            r"\btbd\b",
            r"\bto be confirmed\b",
            r"\bpossibly\b",
            r"\bcan(?:'|no)?t confirm\b",
        ),
    ),
)

#: Internal information that must not reach an external recipient. These are
#: not "risky phrasings" — they are disclosures.
CONFIDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bmargins?\b", "profit margin"),
    (r"\bmark[- ]?ups?\b", "markup"),
    (r"\bprofit(?:ability)?\b", "profit"),
    (r"\bpayroll\b", "payroll"),
    (r"\bwages?\b", "wages"),
    (r"\bwhat we pay\b", "labour cost"),
    (r"\bour cost\b", "internal cost"),
    (r"\binternal note\b", "internal note"),
    (r"\bdo not (?:tell|share with) the (?:client|gc|customer)\b", "internal aside"),
    (r"\bbid strategy\b", "bid strategy"),
    (r"\bcompetitors?\b", "competitor information"),
    (r"\bsupplier discount\b", "supplier pricing"),
    (r"\baccess code\b", "site access code"),
    (r"\balarm code\b", "alarm code"),
    (r"\bkey ?code\b", "key code"),
    (r"\bpasswords?\b", "credentials"),
    (r"\bsin\b", "personal identifier"),
)

#: Tone and professionalism problems. These come back as warnings for the
#: agent to fix rather than as approval gates — the spec treats them as
#: writing standards, not as risk.
STYLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:stupid|idiot|idiots|lazy|useless|clueless)\b", "insulting language"),
    (r"\bscrewed up\b", "unprofessional phrasing"),
    (r"\b(?:disaster|nightmare|mess)\b", "emotional wording"),
    (r"\bthey (?:never|always)\b", "blaming another party"),
    (r"\bnot our (?:problem|job)\b", "dismissive phrasing"),
    (r"\bdamn|\bhell\b", "slang"),
    (r"\bas per my (?:last|previous) (?:email|message)\b", "passive-aggressive phrasing"),
    (r"\bwe (?:are so|are extremely|deeply) sorry\b", "excessive apology"),
    (r"\bjust (?:following up|checking in)\b", "empty follow-up"),
    # The spec forbids revealing that a report was rewritten or translated.
    (r"\btranslated from\b", "discloses translation of the field report"),
    (r"\b(?:broken|poor) english\b", "disparages the field report"),
    (r"\bthe worker wrote\b", "discloses the raw field submission"),
)


@dataclass
class Finding:
    category: str
    scope: str
    trigger: str
    excerpt: str
    guidance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "trigger": self.trigger,
            "excerpt": self.excerpt,
            "guidance": self.guidance,
        }


@dataclass
class ScreenResult:
    recipient_role: str
    external: bool
    findings: list[Finding] = field(default_factory=list)
    disclosures: list[Finding] = field(default_factory=list)
    style_warnings: list[Finding] = field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        seen = [f.category for f in self.findings] + [f.category for f in self.disclosures]
        return sorted(set(seen))

    @property
    def requires_approval(self) -> bool:
        return bool(self.findings or self.disclosures)

    def reason(self) -> str:
        """One line naming why this message cannot auto-send."""
        if not self.requires_approval:
            return ""
        parts = []
        for finding in self.findings + self.disclosures:
            parts.append(f"{finding.category} ({finding.trigger!r})")
        joined = "; ".join(dict.fromkeys(parts))
        return f"Message content requires human approval — {joined}."

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipient_role": self.recipient_role,
            "external_recipient": self.external,
            "requires_approval": self.requires_approval,
            "categories": self.categories,
            "findings": [f.to_dict() for f in self.findings],
            "confidential_disclosures": [f.to_dict() for f in self.disclosures],
            "style_warnings": [f.to_dict() for f in self.style_warnings],
            "guidance": (
                "This message cannot be sent automatically. Keep the draft, tell the "
                "user it is queued for approval, and do not restate it as sent."
                if self.requires_approval
                else "No approval triggers found. Style warnings, if any, should still be fixed."
            ),
        }


def _excerpt(text: str, match: re.Match[str], width: int = 60) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def _negated(text: str, match: re.Match[str]) -> bool:
    """True when the hit is preceded by a negation, e.g. 'no injuries today'."""
    lead = text[max(0, match.start() - 24):match.start()].lower()
    words = re.findall(r"[a-z]+", lead)
    return bool(words) and words[-1] in NEGATIONS


def _scan(text: str, category: Category) -> tuple[str, str] | None:
    for pattern in category.patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if category.negatable and _negated(text, match):
                continue
            return match.group(0), _excerpt(text, match)
    return None


def screen(
    text: str,
    *,
    recipient_role: RecipientRole | str = RecipientRole.CLIENT,
    subject: str = "",
) -> ScreenResult:
    """Screen a message for anything that requires a human before sending."""
    haystack = f"{subject}\n{text}".strip()
    external = is_external(recipient_role)
    role = recipient_role.value if isinstance(recipient_role, RecipientRole) else str(recipient_role)
    result = ScreenResult(recipient_role=role, external=external)

    for category in CATEGORIES:
        if category.scope == EXTERNAL and not external:
            continue
        hit = _scan(haystack, category)
        if hit:
            trigger, excerpt = hit
            result.findings.append(
                Finding(
                    category=category.name,
                    scope=category.scope,
                    trigger=trigger,
                    excerpt=excerpt,
                    guidance=category.guidance,
                )
            )

    if external:
        for pattern, label in CONFIDENTIAL_PATTERNS:
            match = re.search(pattern, haystack, flags=re.IGNORECASE)
            if match:
                result.disclosures.append(
                    Finding(
                        category="confidential_disclosure",
                        scope=EXTERNAL,
                        trigger=match.group(0),
                        excerpt=_excerpt(haystack, match),
                        guidance=f"{label} must not be disclosed to an external recipient.",
                    )
                )

    for pattern, label in STYLE_PATTERNS:
        match = re.search(pattern, haystack, flags=re.IGNORECASE)
        if match:
            result.style_warnings.append(
                Finding(
                    category="style",
                    scope=ALL,
                    trigger=match.group(0),
                    excerpt=_excerpt(haystack, match),
                    guidance=f"Rewrite: {label}.",
                )
            )

    return result


def screen_record(record: dict[str, Any]) -> ScreenResult:
    """Screen a stored communication draft.

    Send-time classification reads the *stored* body rather than whatever
    arguments the model passes to the send tool, so a draft cannot be
    screened clean and then sent as something else.
    """
    return screen(
        str(record.get("body", "")),
        recipient_role=str(record.get("recipient_role", RecipientRole.CLIENT.value)),
        subject=str(record.get("subject", "")),
    )
