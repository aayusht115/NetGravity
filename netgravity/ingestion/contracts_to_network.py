"""
NetGravity — Contractual site commitments, applied to the network
================================================================
Turns the `FacilityCommitment` clauses read out of a contract into the
`FacilityRecord` fields the MILP already enforces.

The gap this closes
-------------------
Constraint C5c in `optimization/milp.py` pins `y_i = 1` for any facility whose
`contract_status` is ACTIVE and which does not permit early closure. Validation
check V-015 names the conflict when a scenario tries to close such a facility.
The Digital Twin reports `contract_status` per site, and
`metrics/contracts.py` puts it in the contract summary.

None of that had ever fired, because **nothing set the fields**. No ingestion
path, no API, no scenario override: `contract_status` defaulted to NONE on every
facility of every network. The enforcement was structurally present and
permanently inert, so a planner could be shown a recommendation to close a site
the client was contractually unable to close — and the one part of the system
whose job was to object had no way to know.

What this does and does not decide
----------------------------------
It translates. It does not judge:

* A commitment is applied only where the document stated enough to act on —
  `is_stated_enough_to_apply`.
* `allows_early_closure` is treated as prohibiting closure ONLY when the
  document said so. Silence stays silence: an unstated term must not pin a
  facility open, because that would block a closure the client is free to make,
  on the strength of a clause nobody wrote.
* An exit penalty becomes `closure_cost`, which the objective already charges
  once on an open → closed transition. It is never invented from rent times
  remaining term.
* A commitment naming a site this network does not contain is REPORTED, not
  dropped and not force-matched. The likeliest cause is that the client's
  facility ids and their contracts use different names for the same building,
  and that is a thing for a person to resolve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from netgravity.ingestion.schemas.contract import ContractRule, FacilityCommitment
from netgravity.schemas.network import CanonicalNetwork, ContractStatus

logger = logging.getLogger(__name__)


@dataclass
class CommitmentApplication:
    """
    What applying a set of commitments did.

    `network` is always a network — this never refuses to produce one, because a
    contract that cannot be matched is a data-quality finding rather than a
    reason to stop optimising. What it must never do is stay quiet about it.
    """
    network: CanonicalNetwork

    #: facility_id -> the commitment that bound it.
    applied: Dict[str, FacilityCommitment] = field(default_factory=dict)
    #: Facilities pinned open because a contract forbids early closure.
    pinned_open: List[str] = field(default_factory=list)
    #: Facilities that gained a stated exit penalty.
    priced_exit: List[str] = field(default_factory=list)
    #: Commitments naming a site this network does not contain.
    unmatched: List[str] = field(default_factory=list)
    #: Commitments the document left too vague to act on.
    understated: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)

    @property
    def n_applied(self) -> int:
        return len(self.applied)

    @property
    def changed_anything(self) -> bool:
        return bool(self.applied)


def _match(commitment: FacilityCommitment,
           by_id: Dict[str, Any], by_name: Dict[str, Any]) -> Optional[str]:
    """
    The facility a commitment binds, or None.

    Matched on the id the extraction supplied, then on an exact
    case-insensitive facility name. Deliberately nothing fuzzier: a commitment
    that pins a site open is the wrong place for a near-miss match, because the
    cost of binding the wrong building is a plan that cannot be executed.
    """
    stated = (commitment.facility_id or "").strip()
    if stated and stated in by_id:
        return stated
    label = (commitment.facility_label or "").strip().lower()
    if label and label in by_name:
        return by_name[label]
    # An id that arrived in the label field, or vice versa.
    if label and label.upper() in by_id:
        return label.upper()
    if stated and stated.lower() in by_name:
        return by_name[stated.lower()]
    return None


def apply_commitments(
    network: CanonicalNetwork,
    commitments: Sequence[FacilityCommitment],
) -> CommitmentApplication:
    """
    Stamp contractual commitments onto a network's facilities.

    Returns a `CommitmentApplication` describing exactly what changed, what did
    not match, and what the documents left unstated.
    """
    result = CommitmentApplication(network=network)
    if not commitments:
        return result

    by_id = {f.id: f for f in network.facilities}
    by_name = {(f.name or "").strip().lower(): f.id
               for f in network.facilities if (f.name or "").strip()}

    updates: Dict[str, Dict[str, Any]] = {}

    for commitment in commitments:
        label = (commitment.facility_label or commitment.facility_id
                 or "an unnamed site")
        facility_id = _match(commitment, by_id, by_name)

        if facility_id is None:
            result.unmatched.append(label)
            continue
        if not commitment.is_stated_enough_to_apply and not commitment.facility_label:
            result.understated.append(label)
            continue

        change: Dict[str, Any] = updates.setdefault(facility_id, {})

        # Status. EXPIRED is stated as clearly as ACTIVE — a contract that has
        # ended is a fact worth recording, because it is what makes a closure
        # permissible.
        if commitment.is_active is True:
            change["contract_status"] = ContractStatus.ACTIVE
        elif commitment.is_active is False:
            change["contract_status"] = ContractStatus.EXPIRED

        # Early closure. Only ever set from a stated value.
        if commitment.allows_early_closure is not None:
            change["contract_allows_early_closure"] = commitment.allows_early_closure

        # A stated exit penalty is the closure cost the objective charges.
        if commitment.early_exit_penalty is not None:
            change["closure_cost"] = float(commitment.early_exit_penalty)
            result.priced_exit.append(facility_id)

        if not change:
            result.understated.append(label)
            continue

        result.applied[facility_id] = commitment
        if commitment.prohibits_closure:
            result.pinned_open.append(facility_id)

    if not updates:
        if result.unmatched:
            result.warnings.append(
                f"{len(result.unmatched)} contractual site commitment(s) name a "
                f"facility this network does not contain, so none was applied: "
                f"{', '.join(result.unmatched[:6])}"
                f"{'…' if len(result.unmatched) > 6 else ''}. The likeliest cause "
                f"is that the network and the contracts name the same buildings "
                f"differently; nothing has been force-matched."
            )
        return result

    facilities = [
        f.model_copy(update=updates[f.id]) if f.id in updates else f
        for f in network.facilities
    ]
    # A new data version, because the network the solver sees is now different:
    # a pinned facility changes the feasible set, and an exit penalty changes
    # the objective. Sharing the observed version would let a snapshot store
    # return the un-stamped network for this one.
    updated = network.model_copy(update={"facilities": facilities,
                                         "data_version": None})
    result.network = updated.model_copy(
        update={"data_version": updated.compute_data_version()})

    if result.pinned_open:
        result.assumptions.append(
            f"{len(result.pinned_open)} facility(ies) are held OPEN by an active "
            f"contract that does not permit early closure: "
            f"{', '.join(result.pinned_open)}. Any scenario that closes one is "
            f"reported INFEASIBLE rather than costed, because it is not a "
            f"decision the client can take without renegotiating."
        )
    if result.priced_exit:
        result.assumptions.append(
            f"{len(result.priced_exit)} facility(ies) carry a stated early-exit "
            f"penalty, charged once when the model closes them: "
            f"{', '.join(result.priced_exit)}."
        )
    if result.understated:
        result.warnings.append(
            f"{len(result.understated)} commitment clause(s) named a site but "
            f"stated no term this model can act on, so nothing was applied for "
            f"them: {', '.join(result.understated[:6])}. Silence is not read as "
            f"a lock-in — an unstated term would otherwise block a closure the "
            f"client is free to make."
        )
    if result.unmatched:
        result.warnings.append(
            f"{len(result.unmatched)} commitment(s) name a facility this network "
            f"does not contain: {', '.join(result.unmatched[:6])}. Nothing has "
            f"been force-matched."
        )

    logger.info(
        "ingestion.contracts.applied network_id=%s applied=%d pinned=%d "
        "priced=%d unmatched=%d",
        network.network_id, len(result.applied), len(result.pinned_open),
        len(result.priced_exit), len(result.unmatched),
    )
    return result


def commitments_from_rules(rules: Iterable[ContractRule]) -> List[FacilityCommitment]:
    """Every site commitment across a set of extracted contracts."""
    out: List[FacilityCommitment] = []
    for rule in rules or []:
        out.extend(getattr(rule, "facility_commitments", []) or [])
    return out


def apply_contract_rules(
    network: CanonicalNetwork,
    rules: Iterable[ContractRule],
) -> CommitmentApplication:
    """Convenience: extract the commitments from contracts and apply them."""
    return apply_commitments(network, commitments_from_rules(rules))


__all__ = [
    "CommitmentApplication",
    "apply_commitments",
    "apply_contract_rules",
    "commitments_from_rules",
]
