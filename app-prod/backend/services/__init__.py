"""
NetGravity — Application Services
=================================
The application layer that sits between HTTP and the orchestrator.

These modules own *application* concerns only — who is asking, which project
they are asking about, and which network snapshot that project is bound to.
They own no business calculation. Every number they return originates in an
engine reached through the orchestrator, and every one that cannot be produced
is reported as an explicit status rather than a plausible substitute.
"""
