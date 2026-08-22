"""
Phase 3.1 — NLU evaluation.

A labelled dataset and a harness for measuring how well natural language is
turned into a `ConversationalIntent`, offline and against the real gateway.

This package is deliberately NOT a test package. It is a measurement
instrument: `dataset.py` states what the right answer is, `harness.py` records
what the system actually said, and the assertions in
`tests/integration/test_nlu_evaluation.py` pin only the thresholds we are
prepared to defend. Keeping the two apart means a regression shows up as a
number that moved, not merely as a red test.
"""

from netgravity.tests.nlu_eval.dataset import (
    CASES,
    Category,
    EvalCase,
    cases_by_category,
    cases_for_intent,
)

__all__ = ["CASES", "Category", "EvalCase", "cases_by_category", "cases_for_intent"]
