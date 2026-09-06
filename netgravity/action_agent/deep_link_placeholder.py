"""
NetGravity — Deep-Link Placeholder Pages
============================================
PATCH POINT — replace this whole module once the frontend has real routes.

Every email the Action Agent sends contains a link — the missing-data
resume link, or the recommendation/investigate deep link. Those links need
somewhere real to land TODAY (so the email flow can be tested end to end
with an actual click), but the actual screens (ingestion review console,
insight/card detail) live in the frontend, which isn't wired to this
backend yet (see the handoff notes on app/frontend/js/*.js being fully
mocked).

Rather than send a link to nothing, these two routes are a working stand-in
that confirms the click landed, shows which session/card it resolved to,
and says plainly that a real screen goes here next.

TO REPLACE: once the frontend is ready, either:
  (a) point NETGRAVITY_APP_BASE_URL at the frontend's own origin and let it
      own these paths directly (this blueprint is then simply not
      registered / removed), or
  (b) keep this blueprint mounted here and have it redirect
      (flask.redirect) to the frontend's route instead of rendering HTML.

Nothing in netgravity/action_agent/triggers.py needs to change either way —
it only ever builds the URL string, never assumes what serves it.
"""

from __future__ import annotations

from flask import Blueprint

from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService, SessionNotFound
from netgravity.ingestion.storage import get_storage


def _page(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title} — NetGravity (placeholder)</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px;
         margin: 64px auto; padding: 0 24px; color: #181b22; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .04em;
           text-transform: uppercase; background: #e7ecfd; color: #3452e1;
           padding: 3px 9px; border-radius: 999px; margin-bottom: 16px; }}
  code {{ background: #eeece4; padding: 2px 6px; border-radius: 4px; }}
  .note {{ margin-top: 28px; padding: 14px 16px; background: #fbe7e1; border: 1px solid #e7b9ab;
          border-radius: 6px; font-size: 13px; color: #c6432a; }}
</style></head>
<body>
  <span class="badge">Placeholder — not the real screen</span>
  <h1>{title}</h1>
  {body_html}
  <div class="note">This page exists so the Action Agent's email link has somewhere
  real to land before the frontend is wired up. Replace
  <code>netgravity/action_agent/deep_link_placeholder.py</code> with an actual
  redirect (or remove it) once the frontend owns this route.</div>
</body></html>"""


def create_deep_link_placeholder_blueprint() -> Blueprint:
    bp = Blueprint("deep_link_placeholder", __name__)
    service = IngestionService(IngestionConfig())
    storage = get_storage(IngestionConfig())

    @bp.route("/ingestion/<run_id>/review", methods=["GET"])
    def ingestion_review(run_id: str):
        try:
            session = service.get(run_id)
        except SessionNotFound:
            return _page("Session not found", f"<p>No ingestion session <code>{run_id}</code> "
                        f"exists in this backend's data root.</p>"), 404

        missing_required = (session.report or {}).get("missing_required") or []
        missing_optional = (session.report or {}).get("missing_optional") or []
        rows = "".join(
            f"<li><b>{m.get('entity_type', '')}: {m.get('entity_name', '')}</b> — "
            f"{m.get('display_label', '')}</li>"
            for m in missing_required
        ) or "<li>None</li>"
        opt_rows = "".join(
            f"<li>{m.get('display_label', '')}</li>" for m in missing_optional
        ) or "<li>None</li>"

        body = f"""
        <p>Ingestion session <code>{session.run_id}</code> — status
        <b>{session.status}</b>, revision {session.revision}.</p>
        <h3>Missing required fields</h3><ul>{rows}</ul>
        <h3>Missing optional fields</h3><ul>{opt_rows}</ul>
        <p>The real ingestion review console (drag-and-drop a corrected file,
        answer column-mapping questions) renders here once the frontend is wired
        to <code>/api/ingestions/{session.run_id}</code>.</p>
        """
        return _page("Ingestion review", body)

    @bp.route("/insights/<card_id>", methods=["GET"])
    def insight_card(card_id: str):
        from netgravity.action_agent.dispatch_log import DispatchLogStore

        matches = [r for r in DispatchLogStore(storage).list_all() if r.reference_id == card_id]
        if not matches:
            body = (f"<p>No dispatch record references <code>{card_id}</code> in this "
                    f"backend's data root — it may have been created by a different run, "
                    f"or the id in the link is wrong.</p>")
            return _page("Card not found", body), 404

        rows = "".join(
            f"<li><b>{r.trigger_type}</b> — sent {r.sent_at} to {', '.join(r.recipients)} "
            f"— <i>{r.subject}</i></li>" for r in matches
        )
        body = f"""
        <p>Card / execution <code>{card_id}</code>.</p>
        <h3>Dispatch history for this id</h3><ul>{rows}</ul>
        <p>The real insight detail screen (headline, KPI evidence, Approve / Edit /
        Reject) renders here once the frontend is wired to the orchestrator's
        <code>GET /orchestrator/executions/{card_id}</code>.</p>
        """
        return _page("Insight card", body)

    return bp
