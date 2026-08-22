"""
External-signal guardrail tests.

These encode the policy the team agreed in review, so a later change to
thresholds.yaml that breaks an agreed rule fails loudly rather than silently.
"""

from __future__ import annotations

from datetime import date

from netgravity.ingestion.guardrails import evaluate, load_policy
from netgravity.ingestion.schemas.signal import (
    MarketIntelligenceSignal,
    ScenarioUse,
    SignalBucket,
    SignalConfidence,
)

KNOWN = {"DC_DELHI", "DC_GUWAHATI", "PLT_PUNE", "MKT_KOLKATA"}


def _signal(**kw) -> MarketIntelligenceSignal:
    base = dict(
        signal_id="SIG-TEST", title="Test signal",
        published_date="2026-08-01", effective_date="2026-08-15",
        confidence=SignalConfidence.HIGH,
    )
    base.update(kw)
    return MarketIntelligenceSignal(**base)


def test_competitor_news_is_excluded_by_default():
    """Team review: competitor news is low signal-to-noise. Logged, never used."""
    s = _signal(bucket=SignalBucket.COMPETITOR,
                title="Rival opens a new distribution centre")
    v = evaluate(s, known_entity_ids=KNOWN)
    assert v.passed is False
    assert "excluded by default" in v.reason


def test_carrier_signal_naming_our_node_passes():
    s = _signal(bucket=SignalBucket.CARRIER,
                title="Carrier cuts fleet capacity on the Delhi corridor",
                affected_entities=["DC_DELHI"])
    assert evaluate(s, known_entity_ids=KNOWN).passed is True


def test_supplier_signal_naming_our_plant_passes():
    s = _signal(bucket=SignalBucket.SUPPLIER,
                title="Supplier halts production",
                affected_entities=["PLT_PUNE"])
    assert evaluate(s, known_entity_ids=KNOWN).passed is True


def test_immaterial_macro_move_is_filtered():
    """A 0.9% index move must not reach the optimizer."""
    s = _signal(bucket=SignalBucket.MACRO,
                title="Wholesale price index up slightly",
                magnitude="+0.9%", confidence=SignalConfidence.MEDIUM)
    v = evaluate(s, known_entity_ids=KNOWN)
    assert v.passed is False
    assert "materiality" in v.reason


def test_material_macro_move_passes_without_naming_a_node():
    """
    A pan-India fuel move affects every lane but can name no single facility,
    so it can never earn the entity bonus. Clearing materiality must be enough
    on its own — otherwise a major fuel shock scores below a trivial local one.
    """
    s = _signal(bucket=SignalBucket.MACRO,
                title="Diesel prices expected to rise",
                magnitude="+8% fuel cost", confidence=SignalConfidence.MEDIUM,
                affected_entities=[])
    v = evaluate(s, known_entity_ids=KNOWN)
    assert v.passed is True, v.reason
    assert "materiality bar" in v.reason


def test_expired_weather_signal_is_filtered():
    """A months-old cyclone must not remain an active assumption."""
    s = _signal(bucket=SignalBucket.WEATHER, title="Cyclone warning",
                effective_date="2026-01-01", affected_entities=["MKT_KOLKATA"])
    v = evaluate(s, known_entity_ids=KNOWN, today=date(2026, 8, 19))
    assert v.passed is False
    assert "expired" in v.reason


def test_recent_weather_signal_passes():
    s = _signal(bucket=SignalBucket.WEATHER, title="Cyclone warning",
                effective_date="2026-08-16", affected_entities=["MKT_KOLKATA"])
    v = evaluate(s, known_entity_ids=KNOWN, today=date(2026, 8, 19))
    assert v.passed is True


def test_unclassified_signal_is_held_back():
    """Anything we cannot classify is withheld, not assumed relevant."""
    s = _signal(title="Something entirely unrelated happened",
                confidence=SignalConfidence.LOW)
    assert evaluate(s, known_entity_ids=KNOWN).passed is False


def test_bucket_is_inferred_from_text_when_not_supplied():
    s = _signal(title="Major carrier reduces trucking capacity")
    assert evaluate(s, known_entity_ids=KNOWN).bucket == SignalBucket.CARRIER


def test_filtered_signals_are_never_silently_dropped():
    """Auditability: a filtered signal keeps a verdict explaining the decision."""
    from netgravity.ingestion.guardrails import apply

    signals = [
        _signal(signal_id="S1", bucket=SignalBucket.COMPETITOR, title="Rival news"),
        _signal(signal_id="S2", bucket=SignalBucket.CARRIER,
                title="Carrier capacity cut", affected_entities=["DC_DELHI"]),
    ]
    out = apply(signals, known_entity_ids=KNOWN)
    assert len(out) == 2, "filtered signals must be retained for audit"
    filtered = [s for s in out if not s.passed_guardrail]
    assert filtered and filtered[0].verdict.reason
    assert filtered[0].scenario_use == ScenarioUse.LOGGED_ONLY


def test_policy_loads_with_all_buckets_defined():
    policy = load_policy()
    for bucket in (SignalBucket.CARRIER, SignalBucket.SUPPLIER,
                   SignalBucket.CUSTOMER, SignalBucket.MACRO,
                   SignalBucket.WEATHER, SignalBucket.COMPETITOR):
        assert bucket in policy.buckets, f"{bucket} missing from guardrail policy"
