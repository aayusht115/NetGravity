# Ingestion clarification workflow

This workflow separates an immediate AI-assisted draft from the confirmed
canonical network used by deterministic engines.

## User flow

1. Upload CSV, TSV, XLSX/XLSM, PDF, text, or Markdown files.
2. Inspect the provisional network, contracts, data-quality findings, and
   unfamiliar fields.
3. Resolve blocking mappings or ask AI to investigate one field.
4. Confirm the resulting structured interpretation.
5. Finalize only after blocking questions are settled.

AI investigation responses are not rendered directly. The backend requires a
small JSON object, validates its recommended value against the content-type
schema, and caps the displayed recommendation, reason, and question at 8, 18,
and 12 words respectively (35 words total).

Free text is evidence, not a write command. It produces a proposal that must be
confirmed through the review endpoint. Unresolved and supplementary fields
never enter `CanonicalNetwork` or the MILP.

## Field dispositions

- `CANONICAL`: approved field eligible for the canonical network.
- `SUPPLEMENTARY`: preserved for consultant context, excluded from engines.
- `UNRESOLVED`: preserved and visible, meaning not settled.
- `PROPOSED_NEW`: recorded for governed schema review.
- `IGNORED`: deliberately excluded while the raw upload remains intact.

## HTTP surface

- `POST /api/ingestions`
- `GET /api/ingestions/{run_id}`
- `GET /api/ingestions/{run_id}/draft`
- `GET /api/ingestions/{run_id}/reviews`
- `POST /api/ingestions/{run_id}/reviews/analyse` with `item_id` in JSON
- `POST /api/ingestions/{run_id}/reviews`
- `POST /api/ingestions/{run_id}/finalize`

Review writes use an expected session revision. Stale submissions receive HTTP
409 rather than overwriting a newer decision.

## Trust boundaries

- The model cannot create canonical fields.
- Model confidence is supporting evidence, not authority.
- Machine confirmations do not generalize to other senders.
- The structured field memory/catalogue is authoritative; semantic retrieval
  can be added later only as another source of suggestions.
- A failed live model call marks the UI ingestion session failed; fallback stub
  data is never finalized as a trusted upload.
