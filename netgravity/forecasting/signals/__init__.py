"""
External signals as forecasting features.

The forecasting pathway only. Event likelihood belongs to the RF pathway and
never passes through here — see `enrichment.py` for the full statement of the
boundary and what was removed to keep it.
"""

from netgravity.forecasting.signals.enrichment import BucketRule, SignalEnricher

__all__ = ["SignalEnricher", "BucketRule"]
