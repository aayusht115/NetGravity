"""
NetGravity — Action Agent
===========================
A dispatcher, not a second decision-maker. Everything this package sends
traces back to something the orchestrator/MILP already computed, or a
data-completeness check that is purely rule-based. It never runs its own
scenario and never originates a recommendation.

See netgravity/action_agent/triggers.py for the five entry points.
"""
