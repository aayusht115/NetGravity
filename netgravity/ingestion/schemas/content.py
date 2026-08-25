"""
NetGravity — Content Types
===========================
WHAT a record set contains, and therefore WHERE its rows are allowed to go.

WHY THE ROUTING LIVES HERE
--------------------------
Ingestion used to decide destination by folder: anything under distributors/
went to the staging zone, anything else fed the optimiser. That is the wrong
basis for the decision, because where a file sat says nothing reliable about
what was inside it. A distributor can send a facility list; a client can send
shipment history.

So destination is a property of the CONTENT TYPE, resolved from the data
itself by ai/classifier.py — not of the path, the sender, or the filename.

The distinction that must never blur:

    NETWORK   facilities, markets, products, demand, lanes.
              These become the CanonicalNetwork the MILP solves against.
              Wrong data here produces wrong optimisation results, so this
              destination carries the strict confirmation bar.

    STAGING   shipment/despatch logs, historical volume.
              Transactional history. It is forecasting input (Layer 3), NOT
              network structure. Loading it into the network would silently
              alter the Digital Twin, which is why it is kept separate even
              though it arrives through the same pipeline.

    HOLD      anything unclassified. Held for a human to label rather than
              guessed at. Same principle as the guardrail UNKNOWN bucket.
"""

from __future__ import annotations

from enum import Enum

DEST_NETWORK = "network"
DEST_STAGING = "staging"
DEST_HOLD = "hold"


class ContentType(str, Enum):
    """What a record set is."""

    FACILITY = "FACILITY"                    # DCs, plants, warehouses
    MARKET = "MARKET"                        # demand zones / destinations
    PRODUCT = "PRODUCT"                      # SKU master
    DEMAND = "DEMAND"                        # demand per market/product
    LANE = "LANE"                            # transport lanes and rates
    SHIPMENT_LOG = "SHIPMENT_LOG"            # transactional despatch history
    MARKET_SIGNAL = "MARKET_SIGNAL"          # dated external/market intelligence
    HISTORICAL_VOLUME = "HISTORICAL_VOLUME"  # volume time series
    UNKNOWN = "UNKNOWN"                      # could not be determined

    @property
    def destination(self) -> str:
        if self in _NETWORK_TYPES:
            return DEST_NETWORK
        if self in _STAGING_TYPES:
            return DEST_STAGING
        return DEST_HOLD

    @property
    def feeds_optimizer(self) -> bool:
        """True when this content reaches the MILP — the strict-bar cases."""
        return self.destination == DEST_NETWORK

    @classmethod
    def parse(cls, value: object) -> "ContentType":
        """Tolerant lookup. Anything unrecognised becomes UNKNOWN, never an error."""
        try:
            return cls(str(value).strip().upper())
        except (ValueError, AttributeError):
            return cls.UNKNOWN


_NETWORK_TYPES = {
    ContentType.FACILITY, ContentType.MARKET, ContentType.PRODUCT,
    ContentType.DEMAND, ContentType.LANE,
}
_STAGING_TYPES = {
    ContentType.SHIPMENT_LOG, ContentType.HISTORICAL_VOLUME,
    # MARKET_SIGNAL is staging, and that placement is the whole point.
    # A fuel-price story or a port notice is CONTEXT: it shifts an
    # assumption and explains a result. Routing it to NETWORK would let a
    # headline become a number the MILP treats as fact, which the
    # architecture forbids — only the deterministic engines produce
    # numbers. Held here, a signal enriches a forecast and supports a
    # root-cause narrative without ever silently editing a rate.
    ContentType.MARKET_SIGNAL,
}


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field          # noqa: E402
from typing import Dict, List, Optional           # noqa: E402


@dataclass
class ContentClassification:
    """
    What one record set was judged to be, and how much to trust that.

    Carries BOTH opinions — the model's and the deterministic rule scorer's —
    rather than collapsing them to a single answer, because the disagreement
    is itself the signal that a human should look.
    """

    content_type: ContentType = ContentType.UNKNOWN
    confidence: float = 0.0
    reasoning: str = ""
    proposed_by: str = "rules"                       # "rules" | "<provider>:<model>"

    #: The deterministic alias-overlap opinion, always computed.
    rule_type: ContentType = ContentType.UNKNOWN
    rule_score: float = 0.0
    rule_scores: Dict[str, float] = field(default_factory=dict)

    needs_review: bool = True
    review_reasons: List[str] = field(default_factory=list)
    ai_note: str = ""

    @property
    def rules_agree(self) -> bool:
        return (self.rule_type != ContentType.UNKNOWN
                and self.rule_type == self.content_type)

    @property
    def destination(self) -> str:
        return self.content_type.destination

    @property
    def summary(self) -> str:
        agreement = ("rules agree" if self.rules_agree
                     else f"rules said {self.rule_type.value}")
        return (f"{self.content_type.value} at {self.confidence:.0%} "
                f"via {self.proposed_by} ({agreement})")
