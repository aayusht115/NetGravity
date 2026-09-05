"""
NetGravity — Optimisation priorities
======================================
What "optimise my network" can actually be optimised FOR, and how to ask.

THE RULE THIS MODULE EXISTS TO KEEP: never offer a choice the solver cannot
act on. A question that asks a planner to pick between cost, service and
resilience sounds natural and is the obvious thing to write — and "service"
is not buildable today. `ObjectiveMode.COST_SERVICE` is declared in
netgravity/schemas/network.py and never referenced by the MILP;
`V1_SUPPORTED_OBJECTIVE_MODES` in that same file says so in the code itself.
Offering it would ask someone to choose something that silently changes
nothing, which is worse than not asking at all.

(SLA *is* enforced — `enforce_sla` filters lanes by transit time, and it is on
by default. What does not exist is service as an objective to trade cost
against. So there is nothing to offer, not because service is ignored, but
because it is not a dial.)

Each lever declares its own availability and its own solver overrides, so
this is a registry rather than an if-chain: if COST_SERVICE is genuinely
implemented later, it is one more entry here and nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from netgravity.schemas.network import (
    ObjectiveMode,
    SourcingPolicy,
    V1_SUPPORTED_OBJECTIVE_MODES,
)


@dataclass(frozen=True)
class OptimisationLever:
    """One thing the solver can genuinely be pointed at."""

    id: str
    #: Shown to the user. Plain language, no engine vocabulary.
    label: str
    #: One line on what choosing it does.
    description: str
    #: Phrases that mean the user has ALREADY stated this priority, so the
    #: question must not be asked. Matched as substrings of the lowered text.
    phrases: Tuple[str, ...]
    #: Where in the solver this is real. Quoted in tests, so a lever that
    #: stops being backed by code is visible rather than merely wrong.
    evidence: str
    #: What to change on OptimizationConfig. Takes the network's own config so
    #: a lever can derive a value from it rather than invent one.
    overrides: Callable[[Any], Dict[str, Any]] = field(default=lambda cfg: {})
    #: Whether this lever can bite on THIS network and config. A lever with no
    #: basis in the data is not offered — see `carbon` below.
    available: Callable[[Any, Any], bool] = field(default=lambda net, cfg: True)
    #: Whether a USER is ever offered this choice.
    #:
    #: False keeps the solver capability and removes the question. The
    #: distinction matters: `SourcingPolicy.DUAL` is really enforced by the
    #: MILP, so the lever is not fiction — it is simply not a decision we put
    #: to a planner. `config_overrides_for()` still returns its overrides, so
    #: a caller that knows what it is asking for can still ask.
    offered: bool = True


def _carbon_weight(cfg: Any) -> float:
    """
    The weight to put on CO₂ in the objective.

    Never invented. `carbon_weight` is the direct statement of it;
    `carbon_price` (currency per kg CO₂) is the same quantity expressed as
    money and is what an uploaded dataset is far more likely to carry. With
    neither, there is no basis for a number, and this lever is not offered.
    """
    return float(getattr(cfg, "carbon_weight", 0.0) or 0.0) \
        or float(getattr(cfg, "carbon_price", 0.0) or 0.0)


def _carbon_available(network: Any, cfg: Any) -> bool:
    # A cap is the other implemented route (COST_CARBON applies in any mode).
    return bool(_carbon_weight(cfg)) or getattr(cfg, "carbon_cap_kg", None) is not None


def _carbon_overrides(cfg: Any) -> Dict[str, Any]:
    return {
        "objective_mode": ObjectiveMode.WEIGHTED_COST_CARBON,
        "carbon_weight": _carbon_weight(cfg),
    }


def _resilience_available(network: Any, cfg: Any) -> bool:
    """
    Dual sourcing needs two sources to choose between.

    On a network with one supplying facility the constraint is not a
    trade-off, it is an infeasibility — so the choice is not offered.
    """
    facilities = getattr(network, "facilities", None) or []
    supplying = [
        f for f in facilities
        if str(getattr(getattr(f, "role", None), "value", getattr(f, "role", "")))
        not in ("MARKET", "CUSTOMER")
    ]
    return len(supplying) >= 2


#: The levers, in the order a question offers them. Cost first: it is the
#: default objective and the answer most planners want.
LEVERS: List[OptimisationLever] = [
    OptimisationLever(
        id="cost",
        label="Lowest total cost",
        description="The cheapest network that still serves demand.",
        phrases=("cheapest", "lowest cost", "minimise cost", "minimize cost",
                 "reduce cost", "cut cost", "save money", "cost down",
                 "least expensive", "for cost"),
        evidence="the MILP's baseline objective (milp.py 'TotalCost')",
        overrides=lambda cfg: {"objective_mode": ObjectiveMode.COST_MIN},
    ),
    OptimisationLever(
        id="carbon",
        label="Lowest carbon",
        description="Weighs CO₂ alongside cost, so a cleaner plan can win.",
        phrases=("carbon", "co2", "co₂", "emission", "emissions", "greenhouse",
                 "sustainab", "green", "net zero", "footprint"),
        evidence="milp.py carbon_objective_term under WEIGHTED_COST_CARBON, "
                 "and the carbon cap constraint C_carbon_cap",
        overrides=_carbon_overrides,
        available=_carbon_available,
    ),
    OptimisationLever(
        id="resilience",
        label="Most resilient",
        description="Requires two sources per market, so losing one is survivable.",
        # NOT OFFERED. The solver always solves for cost, and this is not a
        # choice a user is asked to make.
        #
        # The constraint itself is real and stays — `SourcingPolicy.DUAL` is
        # enforced at milp.py 364/827-828/862-869 — so nothing is lost from
        # the engine. What is removed is the question, and with it the claim:
        # "most resilient" promises a general property that dual sourcing does
        # not deliver, and offering it invited the model to say the system
        # optimises for resilience when it optimises for cost under a sourcing
        # constraint.
        offered=False,
        phrases=("resilien", "robust", "dual sourc", "dual-sourc", "second source",
                 "backup", "redundan", "single point of failure", "risk"),
        evidence="milp.py SourcingPolicy.DUAL constraints (C_dual_*)",
        overrides=lambda cfg: {"sourcing_policy": SourcingPolicy.DUAL},
        available=_resilience_available,
    ),
]

class _NoConfig:
    """
    Stands in for an OptimizationConfig that states nothing.

    Every `overrides` callable must survive being handed one, because that is
    what a lever gets when it is asked what it would do to a bare default.
    """

    def __getattr__(self, name: str) -> None:
        return None


def declared_objective_modes() -> Dict[str, str]:
    """Which ObjectiveMode each lever selects, if it selects one."""
    out: Dict[str, str] = {}
    for lever in LEVERS:
        mode = lever.overrides(_NoConfig()).get("objective_mode")
        if mode is not None:
            out[lever.id] = mode.value
    return out


# The guard this module exists for, run at import: a lever that selects an
# objective mode the V1 MILP does not implement is a question we must not ask,
# and it should fail loudly here rather than quietly on a user's screen.
for _lever_id, _mode_value in declared_objective_modes().items():
    assert _mode_value in V1_SUPPORTED_OBJECTIVE_MODES, (
        f"lever {_lever_id!r} selects {_mode_value}, which the V1 MILP does "
        f"not implement — see V1_SUPPORTED_OBJECTIVE_MODES in "
        f"netgravity/schemas/network.py")
del _lever_id, _mode_value


def by_id(lever_id: str) -> Optional[OptimisationLever]:
    return next((l for l in LEVERS if l.id == lever_id), None)


def available_levers(network: Any, config: Any) -> List[OptimisationLever]:
    """
    The levers a user may be OFFERED for this network.

    Two filters, and they mean different things: `offered` is a product
    decision about what we ask, `available` is a fact about whether the
    answer could change anything on this data.
    """
    return [l for l in LEVERS if l.offered and l.available(network, config)]


#: Below this many offered levers there is no question worth asking, and the
#: default objective — cost — simply runs.
#:
#: One option is not a choice. Without this the question degenerated into
#: "what should I optimise for: lowest total cost?", which is an interruption
#: wearing a question mark.
MIN_LEVERS_TO_ASK = 2


def worth_asking(network: Any, config: Any) -> bool:
    """Whether there is a real choice to put to the user on this network."""
    return len(available_levers(network, config)) >= MIN_LEVERS_TO_ASK


def stated_priority(text: str) -> Optional[str]:
    """
    Which priority the user has already named, if any.

    Substring matching, deliberately: this decides only whether to ASK, and
    the cost of a false positive is a question not asked, not a wrong answer
    given. A lever the text names is a priority the user has stated.
    """
    lowered = f" {(text or '').lower()} "
    for lever in LEVERS:
        if any(phrase in lowered for phrase in lever.phrases):
            return lever.id
    return None


def clarification_options(levers: List[OptimisationLever]) -> List[Dict[str, str]]:
    """ClarificationRequest.options, from the levers themselves."""
    return [{"id": l.id, "label": l.label, "description": l.description}
            for l in levers]


def clarification_question(levers: List[OptimisationLever]) -> str:
    """
    One question, naming only what can be acted on.

    Built from the levers so it can never list an option the registry does
    not contain — the failure mode this module exists to prevent.
    """
    labels = [l.label.lower() for l in levers]
    if len(labels) == 1:
        choices = labels[0]
    else:
        choices = f"{', '.join(labels[:-1])}, or {labels[-1]}"
    return (f"Before I run this — what should I optimise for: {choices}? "
            f"They give different networks, so it changes the answer.")


def config_overrides_for(lever_id: str, config: Any) -> Dict[str, Any]:
    """What to change on OptimizationConfig for a chosen priority."""
    lever = by_id(lever_id)
    return lever.overrides(config) if lever else {}


# ---------------------------------------------------------------------------
# Answering the question
# ---------------------------------------------------------------------------


def apply_priority_answer(intent: Any, option_id: str) -> Any:
    """
    Apply a chosen priority to the request that was asked about.

    Returns a CLEAR intent carrying the priority, so the request the user
    originally made proceeds — with the lever they picked reaching the
    solver's config (see registry.py `_config_for_priority`).

    An option that is not a real lever leaves the intent untouched, so the
    question is asked again rather than a request running under a priority
    nobody offered.
    """
    if by_id(option_id) is None:
        return intent
    return intent.model_copy(update={
        "optimisation_priority": option_id,
        "clarity": type(intent.clarity).CLEAR,
        "ambiguity": type(intent.ambiguity).NONE,
        "clarification": None,
        "rationale": f"Resumed with the {option_id} priority the user chose.",
    })
