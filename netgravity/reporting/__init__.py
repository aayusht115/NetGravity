"""
NetGravity — Derivation reports
================================
"How was this reached?" as a document somebody can take away.

A finding on screen has a headline, the figures it cites and one paragraph of
prose. That is the right amount for a screen and the wrong amount for the
conversation that follows it: a planner taking a capacity finding to a
steering committee is asked which figures it rests on, what they were
compared against, what the model assumed and what it could not establish —
and none of that fits on a card.

WHAT THIS IS
------------
A neutral document builder. `DerivationReport` is a plain description of a
conclusion and how it was reached; `build_derivation_docx()` renders one as a
.docx. Neither knows anything about insights, forecasts or the optimiser.

WHY IT IS NOT IN THE INSIGHTS API
---------------------------------
The same question is asked of a demand forecast — which series, which method,
which history, what the model could not see — and of a scenario comparison.
Putting the writer beside the insight endpoint would mean a second copy of it
the first time forecasting needed one, and two documents that formatted the
same facts differently.

WHAT IT WILL NOT DO
-------------------
It does not compute, round, restate or infer anything. Every figure it prints
arrives formatted, from whoever built the report, and is written out verbatim
— the same rule the screens follow (§9). A document that recomputed a figure
would be a second, unverified engine whose output carries a company letterhead
and gets forwarded.
"""

from netgravity.reporting.derivation import (
    DerivationReport,
    DerivationStep,
    Figure,
    build_derivation_docx,
)
from netgravity.reporting.narration import Narration, narrate

__all__ = [
    "DerivationReport",
    "DerivationStep",
    "Figure",
    "Narration",
    "build_derivation_docx",
    "narrate",
]
