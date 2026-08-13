"""
NetGravity — Production-Grade Supply Chain Network Optimization Engine
======================================================================
Version: 1.0.0

Mathematical framework:
  Multi-echelon, multi-product, capacitated network design MILP
  Grounded in Chopra & Meindl, SCM 5th Ed., Chapter 5

Solver: PuLP + HiGHS (open-source, production-grade)

Architecture: Clean separation of concerns
  schemas/     → typed data contracts
  network/     → network assembly
  costs/       → transport cost abstraction
  inventory/   → modular safety-stock / inventory
  carbon/      → CO₂ calculation
  service/     → SLA / service-level constraints
  cog/         → Weiszfeld Center-of-Gravity (screening only)
  validation/  → pre-solve data validation
  optimization/ → core MILP engine + solver interface
  metrics/     → KPI derivation
  scenarios/   → scenario engine
  sensitivity/ → sensitivity sweep engine
  resilience/  → disruption analysis
  assumptions/ → explicit assumption registry
  config/      → default parameters
  tests/       → comprehensive test suite

DO NOT: Use LLM logic inside optimization
DO NOT: Hard-code data, cities, or scenarios
DO:     Separate facts, assumptions, parameters, decisions, outputs
"""

__version__ = "1.0.0"
__author__  = "NetGravity Engineering"
