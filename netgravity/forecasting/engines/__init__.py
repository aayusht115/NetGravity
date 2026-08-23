"""
Forecasting engines.

Each engine turns an ordered sequence of observed quantities into forecast
points and nothing else — no status, no provenance, no notion of a market. The
service owns all of that; see `base.py` for why.
"""

from netgravity.forecasting.engines.base import BaseForecaster, EngineOutput
from netgravity.forecasting.engines.cold_start import ColdStartForecaster
from netgravity.forecasting.engines.ets import ETSForecaster
from netgravity.forecasting.engines.intermittent import IntermittentForecaster
from netgravity.forecasting.engines.quantile import QuantileForecaster
from netgravity.forecasting.engines.selector import EngineSelector

__all__ = [
    "BaseForecaster",
    "EngineOutput",
    "ColdStartForecaster",
    "ETSForecaster",
    "IntermittentForecaster",
    "QuantileForecaster",
    "EngineSelector",
]
