"""The finding record, and the honesty rules that govern its wording.

Spec 3.6 and section 6.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SEVERITY_ORDER = ("info", "low", "medium", "high")
SEVERITIES = frozenset(SEVERITY_ORDER)

# Checks 1 and 2 are UNIVERSAL: they are claims about all paths, so a sample can
# never establish them. Checks 3, 4, 5 and 7 are EXISTENTIAL: they are proved by
# exhibiting one path, so a sample can only under-report them and any finding it
# does emit is real. Checks 6, 8 and 9 are structural and are unaffected either
# way (spec 3.6).
UNIVERSAL_CHECKS = frozenset({"unreachable_step", "inert_rule"})

CONFIDENCE_EXHAUSTIVE = "exhaustive"
CONFIDENCE_SAMPLED = "not_observed_in_sample"

# The wording a sampled walk may never use. A "never" claim from a sample is the
# failure mode that would cost this tool its credibility, so it is enforced
# rather than left to whoever writes the next detail string.
NEVER_WORDINGS = (
    "can never",
    "never appear",
    "never appears",
    "never fire",
    "never fires",
    "no path",
    "cannot ever",
    "will never",
)


@dataclass
class Finding:
    check: str
    severity: str
    confidence: str
    title: str
    detail: str
    step_ids: List[str] = field(default_factory=list)
    rule_ids: List[str] = field(default_factory=list)
    witness: Optional[Dict[str, str]] = None
    recommended_action: str = "review"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "confidence": self.confidence,
            "title": self.title,
            "detail": self.detail,
            "step_ids": list(self.step_ids),
            "rule_ids": list(self.rule_ids),
            "witness": self.witness,
            "recommended_action": self.recommended_action,
        }


def meets_min_severity(severity: str, minimum: str) -> bool:
    if minimum not in SEVERITIES:
        minimum = "medium"
    return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(minimum)


def contains_never_wording(text: str) -> bool:
    """True when a string makes a universal claim.

    Used to enforce the honesty requirement mechanically: a truncated component
    may not emit one, whatever the author of a detail string intended.
    """
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in NEVER_WORDINGS)
