"""
Action items, and the Action Agent that dispatches them.

WHAT AN ACTION IS
-----------------
Something a person has to do that this system cannot do for them, stated
with what it is blocking. Today there is one source: the data-completeness
gate (netgravity/ingestion/completeness.py), run over the committed upload
by `services/completeness_adapter.py`. A required gap means the analysis is
running without a field it needs from a named site; an optional gap means
the analysis ran, and a field would have unlocked more of it.

The envelope is deliberately wider than that one source. Governance already
produces two more kinds — an APPROVAL_REQUIRED card and a HUMAN_ONLY
"please investigate" card, both of which the Action Agent already emails
(orchestrator `_notify_action_agent`). They are not listed here yet because
this endpoint is project-scoped and those are execution-scoped; when that
join exists they become two more `kind`s in the same list, and the frontend
does not change.

WHAT THE AGENT IS, AND IS NOT
-----------------------------
A dispatcher. Every word it sends traces back to something already computed:
the completeness registry's own field labels and the named entities missing
them. It never composes a claim about the network, never runs a scenario,
and never decides that something is worth sending — it is told.

Nothing is sent without a person pressing send. There is no background
dispatcher on this path: the emails the *pipeline* fires (missing data on an
ingestion session, a governance card) are a separate, already-built trigger;
this endpoint is the human-initiated one, from the insight detail screen.

STUB MODE IS REPORTED, NOT HIDDEN
---------------------------------
With no NETGRAVITY_SMTP_HOST / NETGRAVITY_EMAIL_API_KEY configured — the
default, and the state this repository ships in — `EmailSender` logs the
message and returns a labelled stub result. This endpoint passes that label
straight through as `delivery: "stubbed"`, and the UI says so on the button
that was just pressed. A stub reported as a send is the one outcome that
would make this feature worse than not having it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.security import require_auth
from app.backend.services.dataset_store import dataset_store
from app.backend.services.errors import ValidationError
from app.backend.services.project_registry import project_registry

logger = logging.getLogger(__name__)

#: Deliberately permissive. This is a sanity check against an obvious typo,
#: not an attempt to implement RFC 5322 — the address is confirmed by
#: whether the mail arrives, and a regex that rejects a valid address is
#: worse than one that accepts an invalid one.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _storage():
    from netgravity.ingestion.config import IngestionConfig
    from netgravity.ingestion.storage import get_storage
    return get_storage(IngestionConfig())


def _valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def _group_gaps(gaps: List[Dict[str, Any]], severity: str) -> List[Dict[str, Any]]:
    """
    One action per missing FIELD, not per affected row.

    A workbook with no fixed-cost column produces one gap per distribution
    centre — fifteen of them on the Canadian dataset. Fifteen cards saying
    the same sentence with a different site name is not fifteen decisions;
    it is one, and the person acting on it will ask their counterpart for
    one column. The sites are carried inside so the email can name every
    one of them, which is what makes the request actionable.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for gap in gaps:
        label = str(gap.get("display_label") or gap.get("canonical_key") or "")
        if not label:
            continue
        entry = grouped.setdefault(label, {
            "id": f"act_{severity}_{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}",
            "kind": "MISSING_DATA",
            "severity": severity.upper(),
            "display_label": label,
            "canonical_key": gap.get("canonical_key") or "",
            "unit": gap.get("unit") or "",
            "what_it_unlocks": gap.get("what_it_unlocks") or "",
            "entity_type": gap.get("entity_type") or "",
            "entities": [],
        })
        name = str(gap.get("entity_name") or "").strip()
        if name and name not in entry["entities"]:
            entry["entities"].append(name)
    return list(grouped.values())


def _plural(entity_type: str) -> str:
    """
    "Candidate DC" -> "Candidate DCs", not "candidate dcs".

    The registry's entity types carry an acronym ("Candidate DC", "Demand
    Zone"), and lowercasing the whole phrase to fit it into a sentence
    turned DC into dc. The type is a proper noun in this product's own
    vocabulary — the screens say "Candidate DC" everywhere else — so its
    case is preserved and only the plural is added.
    """
    label = (entity_type or "").strip()
    if not label:
        return "records"
    if label.endswith("s"):
        return label
    return label + "s"


def _describe(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    The sentence the feed and the detail page show.

    Written from the registry's own label and the entity names the gate
    found — never a generated phrase, and never a figure. There is no number
    to state here: "how much is this costing you" is not something any
    engine in this system computes for a missing column.
    """
    names = action["entities"]
    count = len(names)
    if count == 0:
        where = "this upload"
    elif count == 1:
        where = names[0]
    else:
        where = f"{count} {_plural(action['entity_type'])}"

    if action["severity"] == "REQUIRED":
        title = f"{action['display_label']} is missing for {where}"
        subtitle = ("The analysis is running without a field it needs from "
                    f"{'this site' if count == 1 else 'these sites'}.")
    else:
        title = f"{action['display_label']} was not provided"
        subtitle = action["what_it_unlocks"] or "Optional — the analysis ran without it."
        if subtitle and not subtitle[0].isupper():
            subtitle = subtitle[0].upper() + subtitle[1:]
    return {**action, "title": title, "subtitle": subtitle,
            # Sent rather than derived in the browser: one pluraliser, on
            # the side that owns the entity vocabulary.
            "entity_type_plural": _plural(action["entity_type"])}


def _actions_for_project(project_id: str) -> List[Dict[str, Any]]:
    committed = dataset_store.committed(project_id) or {}
    completeness = committed.get("completeness") or {}
    actions = [
        *_group_gaps(completeness.get("missing_required") or [], "required"),
        *_group_gaps(completeness.get("missing_optional") or [], "optional"),
    ]
    return [_describe(a) for a in actions]


def _draft_email(action: Dict[str, Any], project_name: str) -> Dict[str, str]:
    """
    The message, composed from the gap and nothing else.

    Every line is either the registry's own words or a name the gate read
    out of the client's file. No model is called here and none should be:
    the whole content of this email is a list of fields and the sites they
    are missing from, which is a thing to state, not a thing to phrase.
    """
    label = action["display_label"]
    names = action["entities"]
    if names:
        listed = "\n".join(f"  - {name}" for name in names)
        heading = (action["entity_type"] or "record") if len(names) == 1 \
            else _plural(action["entity_type"])
        where = f"\n\nAffected {heading}:\n{listed}"
    else:
        where = ""

    if action["severity"] == "REQUIRED":
        subject = f"[NetGravity] Data needed for {project_name}: {label}"
        body = (
            "Hi,\n\n"
            f"We are analysing the {project_name} network and one required field is "
            f"not in the data we have:\n\n"
            f"  {label}\n"
            f"{where}\n\n"
            "Could you send this across? A column in the existing workbook is "
            "fine — it does not need to be a new file.\n\n"
            "Thanks,\n"
            "— sent from NetGravity"
        )
    else:
        subject = f"[NetGravity] Optional data for {project_name}: {label}"
        body = (
            "Hi,\n\n"
            f"We have run the {project_name} network analysis with the data we have. "
            f"One optional field was not included:\n\n"
            f"  {label}\n\n"
            f"{action['what_it_unlocks'] or 'It would let us go further.'}"
            f"{where}\n\n"
            "No action needed if it is not available.\n\n"
            "Thanks,\n"
            "— sent from NetGravity"
        )
    return {"subject": subject, "body": body}


def create_actions_blueprint(url_prefix: str = "/api/actions") -> Blueprint:
    bp = Blueprint("actions", __name__, url_prefix=url_prefix)

    def _project(project_id: str):
        if not project_id:
            raise ValidationError("A project_id is required.")
        return project_registry.get(project_id, user_id=g.current_user.user_id)

    def _recipient_store(project_id: str):
        """
        The address book for ONE project.

        Scoped because these are a client's own contacts. The store was
        deployment-wide, and an address added while working on one network
        was offered, pre-ticked, on the next one — including on a project
        belonging to a different account.
        """
        from netgravity.action_agent.recipients import NotificationRecipientStore
        return NotificationRecipientStore(_storage(), scope=project_id)

    @bp.route("", methods=["GET"])
    @require_auth
    def list_actions():
        """Everything outstanding for this project, and who it can go to."""
        project_id = str(request.args.get("project_id") or "").strip()
        project = _project(project_id)

        from netgravity.action_agent.config import load_config
        from netgravity.action_agent.dispatch_log import DispatchLogStore

        actions = _actions_for_project(project_id)

        # What has already gone out, so a card can say "asked for on
        # Tuesday" instead of inviting the same email a second time.
        sent_by_ref: Dict[str, Dict[str, Any]] = {}
        for record in DispatchLogStore(_storage()).list_all():
            if record.trigger_type != "manual_request":
                continue
            existing = sent_by_ref.get(record.reference_id)
            if existing is None or record.sent_at > existing["sent_at"]:
                sent_by_ref[record.reference_id] = record.as_dict()

        for action in actions:
            action["draft"] = _draft_email(action, project.name)
            action["last_sent"] = sent_by_ref.get(f"{project_id}:{action['id']}")

        return jsonify({
            "project_id": project_id,
            "actions": actions,
            "recipients": [r.as_dict() for r in _recipient_store(project_id).list()],
            # The UI states this on the send button. A stub reported as a
            # send is the one outcome that makes this worse than nothing.
            "email_mode": "stub" if load_config().stub_mode else "live",
        }), 200

    @bp.route("/recipients", methods=["GET"])
    @require_auth
    def list_recipients():
        project_id = str(request.args.get("project_id") or "").strip()
        _project(project_id)
        store = _recipient_store(project_id)
        return jsonify({"recipients": [r.as_dict() for r in store.list()]}), 200

    @bp.route("/recipients", methods=["POST"])
    @require_auth
    def add_recipient():
        body = request.get_json(silent=True) or {}
        project_id = str(body.get("project_id") or "").strip()
        _project(project_id)
        email = str(body.get("email") or "").strip()
        if not _valid_email(email):
            raise ValidationError(f"'{email}' does not look like an email address.")
        store = _recipient_store(project_id)
        store.add(email, label=str(body.get("label") or "").strip())
        return jsonify({"recipients": [r.as_dict() for r in store.list()]}), 201

    @bp.route("/recipients", methods=["DELETE"])
    @require_auth
    def remove_recipient():
        project_id = str(request.args.get("project_id") or "").strip()
        _project(project_id)
        email = str(request.args.get("email") or "").strip()
        if not email:
            raise ValidationError("An email is required.")
        store = _recipient_store(project_id)
        store.remove(email)
        return jsonify({"recipients": [r.as_dict() for r in store.list()]}), 200

    @bp.route("/<action_id>/dispatch", methods=["POST"])
    @require_auth
    def dispatch(action_id: str):
        """
        Send one action's request. Pressed by a person, never by a timer.

        The subject and body are taken from the request, because the screen
        lets them be edited before sending — but they are seeded by
        `_draft_email` from the gap itself, so an unedited send says exactly
        what the gate found.
        """
        body = request.get_json(silent=True) or {}
        project_id = str(body.get("project_id") or "").strip()
        project = _project(project_id)

        action = next((a for a in _actions_for_project(project_id)
                       if a["id"] == action_id), None)
        if action is None:
            raise ValidationError(
                f"'{action_id}' is not an open action on this project.",
                context={"project_id": project_id},
            )

        recipients = [str(e).strip() for e in (body.get("to") or []) if str(e).strip()]
        if not recipients:
            raise ValidationError("At least one recipient is required.")
        invalid = [e for e in recipients if not _valid_email(e)]
        if invalid:
            raise ValidationError(
                f"These do not look like email addresses: {', '.join(invalid)}")

        draft = _draft_email(action, project.name)
        subject = str(body.get("subject") or draft["subject"]).strip()
        message = str(body.get("body") or draft["body"])

        from netgravity.action_agent.dispatch_log import DispatchLogStore, DispatchRecord
        from netgravity.action_agent.email_sender import get_sender

        result = get_sender().send(to=recipients, subject=subject, body=message)

        # Addresses typed in at send time join the standing list, which is
        # the "learning" the recipients store is for — the second time you
        # ask this person for something, they are already offered.
        if body.get("remember", True):
            store = _recipient_store(project_id)
            for email in recipients:
                store.add(email)

        record = DispatchRecord(
            trigger_type="manual_request",
            reference_id=f"{project_id}:{action_id}",
            recipients=recipients,
            subject=subject,
            result="stubbed" if result.stubbed else ("failed" if result.failed else "sent"),
        )
        DispatchLogStore(_storage()).record(record)
        logger.info("actions.dispatched project_id=%s action=%s result=%s recipients=%d",
                    project_id, action_id, record.result, len(recipients))

        return jsonify({
            "dispatch": record.as_dict(),
            # Reported exactly as the sender reported it. "stubbed" means no
            # message left this machine.
            "delivery": record.result,
            "notes": result.notes,
            "recipients": [r.as_dict() for r in _recipient_store(project_id).list()],
        }), 200

    @bp.route("/dispatches", methods=["GET"])
    @require_auth
    def dispatches():
        """The audit trail: what this project has sent, to whom, and when."""
        project_id = str(request.args.get("project_id") or "").strip()
        _project(project_id)
        from netgravity.action_agent.dispatch_log import DispatchLogStore
        records = [
            r.as_dict() for r in DispatchLogStore(_storage()).list_all()
            if r.reference_id.startswith(f"{project_id}:")
        ]
        records.sort(key=lambda r: r["sent_at"], reverse=True)
        return jsonify({"project_id": project_id, "dispatches": records}), 200

    return bp
