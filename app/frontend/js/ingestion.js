/**
 * Netgravity — Data Ingestion Flow
 * =================================
 * Owns everything between Create Project and the app shell:
 *
 *   create project → UPLOAD DATA → (data-loading pop-up)
 *                  → EXCEL/PDF ingestion, one screen per uploaded file
 *                  → (network-loading pop-up) → app (Home)
 *
 * Excel/CSV files are queued before PDFs (matching the walkthrough the
 * product spec gave), each rendered by its own template.
 *
 * The Excel/CSV path is REAL: every column, sample value, row count, mapped
 * field and quality issue on the review screen comes from
 * `/api/ingestions/preview/upload-and-parse`, which parses the actual file. If
 * that call fails the screen says so and refuses to continue, rather than
 * showing a plausible mapping for a file nobody read.
 *
 * The PDF path is still demo content — see CONTRACT_VENDOR below, which says
 * so on the screen itself.
 */

import { DATA_QUALITY, CONTRACT_DEMO, loadNetworkData } from './data.js';
import { getActiveProjectId, setActiveProject } from './integration/project-context.js';
import { hydrateFromBackend } from './integration/hydrate.js';
import {
  beginAnalysisLoading, endAnalysisLoading, reportAnalysisStage,
} from './analysis-loading.js';
import { ingestionService } from './integration/services/ingestion-service.js';
import {
  bindWorkspaceTopbar, recordActivity, showInfoPanel, workspaceTopbarHtml,
} from './workspace-chrome.js';
import {
  buildTemplateWorkbook, saveBlob, templateFilename,
} from './template-download.js';

/* The "mapped to" dropdown's options.
   Served by the parser (`schemaFields`) so the list is exactly the set of
   fields this build reads, produced by the same table that decided each row's
   suggestion. The screen used to ship its own nine-item list — 'Customer ID',
   'Distribution Centre', 'Demand Market', … — and a `<select>` whose value is
   absent from its options falls back to the FIRST option, so every row of a
   real workbook rendered as "Customer ID" regardless of what the server said.
   The fallback below is used only before the first parse returns. */
const FALLBACK_SCHEMA_FIELDS = ['Not used by the model'];

function schemaFields() {
  return (flow.schemaFields && flow.schemaFields.length)
    ? flow.schemaFields : FALLBACK_SCHEMA_FIELDS;
}

// ─── S4: Contract Intelligence (PDF review) ───────────────────
// Sourced from CONTRACT_DEMO (data.js) — a fully-authored per-vendor
// extraction (rate, surcharge, confidence per term) that existed but was
// never wired into the PDF review screen; that screen previously showed
// unrelated hardcoded numbers (₹12/kg, 6.5%, "Apr 2024 – Mar 2026") that
// matched no data source at all. Every uploaded PDF shows the same vendor
// (vendorA), matching this flow's existing convention of reusing identical
// mock content per file type regardless of the actual uploaded file (see
// the Excel/CSV mapping stats in buildQueue()).
const CONTRACT_VENDOR = CONTRACT_DEMO.vendorA;

function contractStatus(effectiveDateStr) {
  const eff = new Date(effectiveDateStr);
  return new Date() >= eff
    ? { label: 'Active', tone: 'green' }
    : { label: 'Upcoming', tone: 'amber' };
}

function reviewTermId(term) {
  return term.field.toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

// Low-confidence/review-required terms (P0 #5) are derived directly from
// each term's own confidence value — never a separate hand-maintained
// list that can drift out of sync with what the data actually says.
function getReviewTerms(vendor) {
  return vendor.extractedTerms.filter(t => t.confidence !== 'HIGH');
}


// Rows are counted by the parser, per file. This used to be
// `6000 + hash(name) % 9000` — a number derived from the FILE NAME, shown
// under the label "Rows analyzed". The 927-row sample workbook reported 12,655.
function rowsAnalyzed(file) {
  const summary = (flow.fileSummaries || {})[file.name];
  return summary && typeof summary.rows === 'number' ? summary.rows : null;
}
function sheetsOf(file) {
  const summary = (flow.fileSummaries || {})[file.name];
  return (summary && summary.sheets) || [];
}

/* File size, in the unit that makes it legible.
   `(bytes / 1024 / 1024).toFixed(1)` renders the 33 KB sample workbook as
   "0.0 MB", which reads as "nothing was uploaded". */
function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/* ─── Icons ──────────────────────────────────────────────────── */
const I = {
  chevronRight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>`,
  chevronLeft: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18"/></svg>`,
  arrowRight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`,
  uploadCloud: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 16.5a4 4 0 0 0-1-7.87 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 6 17"/><polyline points="9 13 12 10 15 13"/><line x1="12" y1="10" x2="12" y2="19"/></svg>`,
  paperclip: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`,
  check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
  checkCircle: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><polyline points="8.5 12 11 14.5 15.5 9.5"/></svg>`,
  warning: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 17h.01"/></svg>`,
  ban: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9.5"/><line x1="5.5" y1="5.5" x2="18.5" y2="18.5"/></svg>`,
  columns: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/></svg>`,
  shuffle: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>`,
  fingerprint: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 0 0-9 9c0 2 .5 3 1 4"/><path d="M12 3a9 9 0 0 1 9 9c0 3-1 5-1 5"/><path d="M12 7a5 5 0 0 0-5 5c0 3 1 4 1 6"/><path d="M12 7a5 5 0 0 1 5 5c0 1.5-.3 2.5-.7 3.5"/><path d="M12 11a1 1 0 0 0-1 1c0 3 1 5 2 7"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  filter: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>`,
  sparkle: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.6L19.4 9.4l-5.6 1.8L12 17l-1.8-5.8L4.6 9.4l5.6-1.8L12 2z"/><path d="M19 15l.8 2.4L22.2 18.2l-2.4.8L19 21.4l-.8-2.4L15.8 18.2l2.4-.8L19 15z" opacity="0.7"/></svg>`,
  waveform: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="2 12 6 12 9 4 13 20 16 12 22 12"/></svg>`,
  agent: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="2.5"/><path d="M12 8V4"/><circle cx="12" cy="3" r="1.3" fill="currentColor" stroke="none"/><line x1="9" y1="14" x2="9" y2="14.5"/><line x1="15" y1="14" x2="15" y2="14.5"/><path d="M2 13h2"/><path d="M20 13h2"/></svg>`,
  docSearch: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h9.5"/><polyline points="13 3 13 8 18 8"/><circle cx="15" cy="16" r="3"/><line x1="17.3" y1="18.3" x2="19.5" y2="20.5"/></svg>`,
  fuel: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="22" x2="15" y2="22"/><line x1="4" y1="9" x2="14" y2="9"/><path d="M4 22V4a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v18"/><path d="M14 9h2a2 2 0 0 1 2 2v5.5a1.5 1.5 0 0 0 3 0V9.5a2 2 0 0 0-.6-1.4L18 5.5"/></svg>`,
  calendar: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11z"/><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="16" x2="12" y2="16.01"/></svg>`,
  fileGeneric: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
  filesStack: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h9l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M4 7v13a1 1 0 0 0 1 1h1"/></svg>`,
  rupee: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="4" x2="18" y2="4"/><line x1="6" y1="8" x2="18" y2="8"/><path d="M6 8c5 0 8 1.5 8 4.5S11 17 6 17"/><line x1="6" y1="17" x2="15" y2="21"/></svg>`,
  download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><line x1="12" y1="11" x2="12" y2="16.5"/><line x1="12" y1="7.8" x2="12" y2="7.81"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
};

/* ─── Flow flow ─────────────────────────────────────────────── */
const flow = {
  project: null,
  files: [],          // [{ id, name, ext, kind: 'excel'|'csv'|'pdf', sizeBytes, uploadedAt, status }]
  queue: [],          // subset of files.id, ordered excel/csv first then pdf
  queueIndex: 0,
  mapping: {},         // fileId -> row[] from the parser, one per source column
  mapStats: {},        // fileId -> { detected, auto, review, ignored }
  fileSummaries: {},   // file name -> { rows, sheets, columnsCount } from the parser
  schemaFields: null,  // the canonical field list the parser offers
  parseError: null,    // why the parse failed, when it did
  pdfReview: {},       // fileId -> { expanded: bool, reviewed: Set<termId> }
  cameFromApp: false,  // true when entered mid-session (app shell was showing), so Back should return there rather than to Create Project
  //: The project's CURRENT dataset, from the server, as an audit record.
  //:
  //: `files` above is what THIS visit has attached. It starts empty on every
  //: entry, which is right for a new upload and was the only thing the screen
  //: ever showed — so a solved project reopened its uploader to "Uploaded
  //: Files (0)" and there was no route to the file, the mapping, the quality
  //: findings or the ingestion time behind its own numbers.
  dataset: null,
  //: How many uploads are still being read by the server.
  //:
  //: The file appears in the list the moment it is chosen, but the parse that
  //: produces the columns, the row counts and the mapping takes several
  //: seconds for a real workbook. "Continue to AI Analysis" used to be enabled
  //: on the first of those and not the second, so continuing promptly opened
  //: the mapping-review screen — whose whole purpose is to show what was
  //: parsed — with nothing parsed: no columns, no rows, "Not read".
  parsing: 0,
};

let uidCounter = 0;
function nextId(prefix) { uidCounter += 1; return `${prefix}-${uidCounter}`; }

function ingEsc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function classifyExt(filename) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  if (ext === 'pdf') return { ext, kind: 'pdf' };
  if (ext === 'csv') return { ext, kind: 'csv' };
  if (ext === 'xlsx' || ext === 'xls') return { ext, kind: 'excel' };
  return { ext, kind: null };
}

/* ─── Shared chrome ──────────────────────────────────────────── */
/**
 * The mapping-review screens' header: the shared top bar, then a Back pill.
 *
 * These screens had a fourth, narrower bar of their own, carrying a help
 * button with no handler on it. The bar now comes from
 * `js/workspace-chrome.js`, so the account control, the activity feed and the
 * help panel are the same three the project screens have — and all three do
 * something. `.ing-back-home-btn` keeps its class, because these screens' own
 * bindings look it up by that name.
 *
 * The avatar chip is still left empty and filled by `applyIdentity()`: the
 * screens re-render several times during an upload, and each render used to
 * restore a hard-coded "AK" for "Amit Kumar" over whoever was signed in.
 */
function topbar() {
  setTimeout(() => {
    if (typeof window.applyIdentity === 'function') window.applyIdentity();
  }, 0);
  return `${workspaceTopbarHtml()}
    <div class="ing-subbar">
      <button class="proj-back-pill ing-back-home-btn" type="button">
        ${I.chevronLeft}<span>Back</span>
      </button>
    </div>`;
}

/* Returns to whichever screen led into this flow: the app shell, exactly
   as it was, if this was a mid-session upload; otherwise Create Project.
   Used by the Upload Data screen — the first step, so "back" leaves the
   ingestion flow entirely. */
function goBack() {
  if (flow.cameFromApp) {
    hideIngestionPages();
    // `.hidden` as well as the inline style: it is what the rest of the
    // application reads to tell whether the landing page is up, including
    // the rule that pins the document to the viewport while it is.
    const landing = document.getElementById('landing-page');
    if (landing) {
      landing.classList.add('hidden');
      landing.style.display = 'none';
    }
    const shell = document.querySelector('.app-shell');
    if (shell) shell.style.display = 'flex';
    const fab = document.getElementById('floating-chatbot-fab');
    if (fab) fab.style.display = 'flex';
    return;
  }
  if (typeof window.showCreateProject === 'function') {
    const origin = typeof window.getCreateOrigin === 'function' ? window.getCreateOrigin() : 'existing';
    window.showCreateProject(origin);
  }
}

/* ═══════════════════════════════════════════════════════════════
   UPLOAD DATA
   ═══════════════════════════════════════════════════════════════ */
/**
 * One attached file, as a card.
 *
 * The six-column table this replaces did not fit the mock's right-hand
 * column, and half of it repeated the same value on every row ("Uploaded",
 * "Just now"). The card keeps every fact the table carried — name, kind,
 * size, when, status — and reads at 380px.
 */
function fileCardHtml(f) {
  const label = f.kind === 'pdf' ? 'PDF' : f.kind.toUpperCase();
  const sub = f.kind === 'pdf' ? 'Contract or rate document' : 'Tabular network dataset';
  const failed = (flow.parseErrors || {})[f.id];
  // Three states, and they are different facts: still being read, read, or not
  // readable. "Uploaded" on all three is what let the review screen be opened
  // for a file the server had not finished with.
  const state = failed
    ? `<div class="ing-file-failed">${I.warning}<span>Not parsed — ${ingEsc(failed)}</span></div>`
    : f.status === 'parsing'
      ? `<div class="ing-file-ok"><span class="ing-type-chip type-${f.kind}">${label}</span><span class="ing-status-chip reading"><span class="ing-spinner"></span>Reading…</span></div>`
      : `<div class="ing-file-ok"><span class="ing-type-chip type-${f.kind}">${label}</span><span class="ing-status-chip">${I.checkCircle}Uploaded</span></div>`;
  return `<div class="ing-file-card${failed ? ' failed' : ''}" data-file-id="${f.id}">
      <span class="ing-file-icon type-${f.kind}">${ingEsc(f.ext.toUpperCase())}</span>
      <div class="ing-file-card-main">
        <div class="ing-file-meta-name" title="${ingEsc(f.name)}">${ingEsc(f.name)}</div>
        <div class="ing-file-meta-sub">${sub} &middot; ${formatFileSize(f.sizeBytes)} &middot; ${ingEsc(f.uploadedAt)}</div>
        ${state}
      </div>
      <button class="ing-row-delete" type="button" data-remove="${f.id}"
              title="Remove ${ingEsc(f.name)}" aria-label="Remove ${ingEsc(f.name)}">${I.trash}</button>
    </div>`;
}

/** The right-hand column: the files attached in this visit, or why there are none. */
function uploadedPanelHtml() {
  if (!flow.files.length) {
    return `<div class="ing-empty-files">
        <span class="ing-empty-tile">${I.folder}</span>
        <div class="ing-empty-title">No files uploaded yet</div>
        <div class="ing-empty-sub">Add at least one file to continue.</div>
      </div>`;
  }
  return `<div class="ing-file-list">${flow.files.map(fileCardHtml).join('')}</div>`;
}

function renderUploadData() {
  const page = document.getElementById('upload-data-page');
  if (!page) return;

  page.innerHTML = `
    ${workspaceTopbarHtml()}
    <div class="proj-scroll">
      <div class="ing-body">
        <div class="ing-head-row">
          <div>
            <h1 class="ing-title">Upload &amp; Align Network Datasets</h1>
            <p class="ing-subtitle">Upload your network data files. Netgravity's AI will automatically align and prepare your data for analysis.</p>
          </div>
          <button type="button" class="proj-back-pill ing-back-home-btn">
            ${I.chevronLeft}<span>Back</span>
          </button>
        </div>

        ${currentDatasetHtml()}

        <div class="ing-split">
          <div class="ing-card">
            <div class="ing-card-title">
              <span class="ing-card-icon">${I.folder}</span>
              <span>1. Upload Datasets</span>
            </div>
            <div class="ing-card-sub">Supported formats: Excel (.xlsx, .xls), CSV (.csv), PDF (.pdf)</div>
            <ul class="ing-card-bullets"><li>Up to 25MB per file</li></ul>
            <div class="ing-dropzone" id="ing-dropzone" tabindex="0" role="button"
                 aria-label="Drag and drop your files here, or press to choose files">
              <span class="ing-dropzone-icon">${I.uploadCloud}</span>
              <div class="ing-dropzone-main">Drag &amp; drop your files here</div>
              <div class="ing-dropzone-or">or</div>
              <button type="button" class="ing-attach-btn" id="ing-attach-btn">${I.paperclip}<span>Attach files</span></button>
            </div>
            <input type="file" id="ing-file-input" accept=".xlsx,.xls,.csv,.pdf" multiple hidden />
            <div class="ing-error" id="ing-upload-error"></div>
            <div class="ing-card-foot">
              <button type="button" class="ing-foot-link" id="ing-template-btn">
                ${I.download}<span>Download template</span>
              </button>
              <button type="button" class="ing-foot-link" id="ing-help-btn">
                ${I.info}<span>Need help?</span>
              </button>
            </div>
          </div>

          <div class="ing-card">
            <div class="ing-card-head-row">
              <div class="ing-card-title">
                <span class="ing-card-icon">${I.fileGeneric}</span>
                <span>2. Uploaded Files <span class="ing-count-badge">(${flow.files.length})</span></span>
              </div>
              <button type="button" class="ing-add-more" id="ing-add-more">${I.plus}<span>Add more files</span></button>
            </div>
            <div id="ing-file-table-slot">${uploadedPanelHtml()}</div>
          </div>
        </div>

        <div class="ing-footer-bar">
          <button type="button" class="ing-skip-link" id="ing-skip-btn">Skip for now</button>
          <button type="button" class="proj-btn-primary" id="ing-continue-btn" ${flow.files.length ? '' : 'disabled'}>
            <span>Continue to AI Analysis</span>${I.arrowRight}
          </button>
        </div>
      </div>
    </div>`;

  bindWorkspaceTopbar(page, { help: 'upload' });
  bindUploadData();
}

function refreshFileTable() {
  const slot = document.getElementById('ing-file-table-slot');
  if (slot) slot.innerHTML = uploadedPanelHtml();
  const badge = document.querySelector('#upload-data-page .ing-count-badge');
  if (badge) badge.textContent = `(${flow.files.length})`;
  const continueBtn = document.getElementById('ing-continue-btn');
  if (continueBtn) {
    // Not while a file is still being read. The next screen exists to show
    // what the parse found, and opening it mid-parse showed an empty mapping
    // for a workbook that was about to arrive with 147 columns in it.
    const busy = flow.parsing > 0;
    continueBtn.disabled = flow.files.length === 0 || busy;
    const label = continueBtn.querySelector('span');
    if (label) label.textContent = busy ? 'Reading your file…' : 'Continue to AI Analysis';
  }
}

async function addFiles(fileList) {
  const errorEl = document.getElementById('ing-upload-error');
  if (errorEl) errorEl.textContent = '';
  const rejected = [];

  const rawList = Array.from(fileList || []);
  if (!rawList.length) return;

  const validRawFiles = [];
  rawList.forEach(file => {
    const { ext, kind } = classifyExt(file.name);
    if (!kind) { rejected.push(`${file.name} (unsupported type)`); return; }
    if (file.size > 25 * 1024 * 1024) { rejected.push(`${file.name} (over 25MB)`); return; }
    const id = nextId('file');
    flow.files.push({
      id,
      name: file.name,
      ext,
      kind,
      sizeBytes: file.size,
      uploadedAt: 'Just now',
      status: 'parsing',
    });
    validRawFiles.push({ id, file, name: file.name });
  });

  if (rejected.length && errorEl) {
    errorEl.textContent = `Couldn't add: ${rejected.join(', ')}.`;
  }
  if (validRawFiles.length) flow.parsing += 1;
  refreshFileTable();

  // Send real files to backend parser
  if (validRawFiles.length > 0) {
    try {
      const formData = new FormData();
      validRawFiles.forEach(item => {
        formData.append('files', item.file);
      });
      // Scoped to the active project and authenticated. The parse endpoint
      // moved under /preview in Phase 10.0 to separate "read the file" from
      // "commit a network the solver may run on".
      const projectId = (window.getCurrentProject && window.getCurrentProject()?.id)
        || getActiveProjectId();
      const data = await ingestionService.uploadAndParse(formData, projectId);
      flow.parseError = null;
      if (data) {
        flow.extractedNetwork = data.structure;
        flow.projectId = projectId;
        flow.schemaFields = data.schemaFields || flow.schemaFields;

        (data.files || []).forEach(summary => {
          if (summary && summary.name) flow.fileSummaries[summary.name] = summary;
        });

        if (data.mapping) {
          validRawFiles.forEach(item => {
            // Keyed by file name. Never fall back to "whatever the first key
            // is": with two uploads that showed one file's columns under the
            // other file's name.
            const mappedRows = Array.isArray(data.mapping)
              ? data.mapping : data.mapping[item.name];
            if (mappedRows && mappedRows.length) flow.mapping[item.id] = mappedRows;
            if (data.mapStats) flow.mapStats[item.id] = data.mapStats;
          });
        }
        // A file the parser could not read is named, not silently dropped.
        (data.parse_errors || []).forEach(pe => {
          const match = validRawFiles.find(v => v.name === pe.file);
          if (match) flow.parseErrors = { ...(flow.parseErrors || {}), [match.id]: pe.error };
        });
        if (data.dataQuality) {
          DATA_QUALITY.totalRecords = data.dataQuality.totalRecords ?? DATA_QUALITY.totalRecords;
          DATA_QUALITY.validRecords = data.dataQuality.validRecords ?? DATA_QUALITY.validRecords;
          DATA_QUALITY.validPct = data.dataQuality.validPct ?? DATA_QUALITY.validPct;
          DATA_QUALITY.nullCellPct = data.dataQuality.nullCellPct ?? null;
          DATA_QUALITY.duplicateRows = data.dataQuality.duplicateRows ?? null;
          DATA_QUALITY.emptyRows = data.dataQuality.emptyRows ?? null;
          // Replace, never merge: the demo issues describe the prototype's own
          // facilities and would sit alongside the real ones as if measured.
          DATA_QUALITY.issues = data.dataQuality.issues || [];
        }
      }
    } catch (err) {
      // The review screen's entire purpose is to show what was parsed. With no
      // parse there is nothing to review, and continuing would present a
      // mapping for a file nobody read.
      flow.parseError = (err && err.message) || 'the file could not be parsed';
      console.warn('Backend extraction notice:', err);
      validRawFiles.forEach((item) => {
        flow.parseErrors = {
          ...(flow.parseErrors || {}), [item.id]: flow.parseError,
        };
      });
    } finally {
      // In `finally`, so a failed parse re-enables the button rather than
      // leaving the screen permanently stuck on "Reading your file…".
      flow.parsing = Math.max(0, flow.parsing - 1);
      validRawFiles.forEach((item) => {
        const f = flow.files.find(x => x.id === item.id);
        if (f && f.status === 'parsing') f.status = 'uploaded';
      });
    }
    refreshFileTable();
  }
}

function bindUploadData() {
  document.querySelector('#upload-data-page .ing-back-home-btn')?.addEventListener('click', goBack);

  const dropzone = document.getElementById('ing-dropzone');
  const fileInput = document.getElementById('ing-file-input');

  const openPicker = () => fileInput?.click();
  document.getElementById('ing-attach-btn')?.addEventListener('click', openPicker);
  document.getElementById('ing-add-more')?.addEventListener('click', openPicker);
  dropzone?.addEventListener('click', openPicker);
  dropzone?.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPicker(); }
  });
  ['dragenter', 'dragover'].forEach(ev =>
    dropzone?.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(ev =>
    dropzone?.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('dragover'); }));
  dropzone?.addEventListener('drop', e => addFiles(e.dataTransfer?.files));
  fileInput?.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

  // Once, not once per render: this element survives `renderUploadData`, which
  // replaces its innerHTML, so re-binding here stacked a listener each time.
  const page = document.getElementById('upload-data-page');
  if (page && page.dataset.delegated !== '1') {
    page.dataset.delegated = '1';
    page.addEventListener('click', e => {
      const btn = e.target.closest('[data-remove]');
      if (!btn) return;
      flow.files = flow.files.filter(f => f.id !== btn.dataset.remove);
      refreshFileTable();
    });
  }

  document.getElementById('ing-skip-btn')?.addEventListener('click', () => {
    if (typeof window.enterApp === 'function') window.enterApp();
  });

  document.getElementById('ing-continue-btn')?.addEventListener('click', () => {
    if (!flow.files.length) return;
    startAiAnalysis();
  });

  document.getElementById('ing-template-btn')?.addEventListener('click', downloadTemplate);
  document.getElementById('ing-help-btn')?.addEventListener('click', showUploadHelp);
}

/**
 * Hand the user a workbook with the parser's own sheet names and headers.
 *
 * The schema is fetched rather than assumed: `GET /api/ingestions/preview/schema`
 * generates it from the extractor's column table, so the template can never
 * offer a column the parser does not read. A failure says so and offers the
 * field reference instead of downloading a file that would be a guess.
 */
async function downloadTemplate() {
  const btn = document.getElementById('ing-template-btn');
  const errorEl = document.getElementById('ing-upload-error');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  if (errorEl) errorEl.textContent = '';
  try {
    const res = await ingestionService.getUploadSchema();
    const blob = buildTemplateWorkbook(res && res.sheets);
    saveBlob(blob, templateFilename());
    recordActivity('Upload template downloaded.', 'success');
  } catch (err) {
    if (errorEl) {
      errorEl.textContent = `The template could not be built: `
        + `${err?.message || 'the field list could not be fetched'}.`;
    }
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/**
 * What this screen expects, and what happens next.
 *
 * The sheet and column list comes from the server so the help text and the
 * template describe the same parser; if it cannot be reached the guidance that
 * does not depend on it is still shown.
 */
async function showUploadHelp() {
  const close = showInfoPanel('Uploading your network data', `
    <p>Upload the workbook or CSVs describing your network. Every column is read,
      shown back to you and mapped to a field this build understands before
      anything is committed — nothing reaches the optimiser until you confirm
      the mapping.</p>
    <ul>
      <li><strong>Excel (.xlsx, .xls)</strong> — one sheet per table. Sheets are
        identified by their columns, not their names, so an unfamiliar sheet
        name is fine.</li>
      <li><strong>CSV (.csv)</strong> — one table per file.</li>
      <li><strong>PDF (.pdf)</strong> — rate cards and contracts.</li>
      <li>Up to 25 MB per file.</li>
    </ul>
    <div id="ing-help-schema"><p class="ing-help-loading">Loading the field list…</p></div>`);

  try {
    const res = await ingestionService.getUploadSchema();
    const host = document.getElementById('ing-help-schema');
    if (!host) return;
    host.innerHTML = `
      <p><strong>Tables this build reads</strong> — a column not listed here is
        kept with the upload and marked as not used by the model.</p>
      <div class="ing-help-tables">
        ${(res.sheets || []).map((s) => `
          <div class="ing-help-table">
            <div class="ing-help-table-name">${ingEsc(s.sheet)}</div>
            <div class="ing-help-cols">${(s.columns || [])
    .map((c) => `<code>${ingEsc(c.header)}</code>`).join(' ')}</div>
          </div>`).join('')}
      </div>`;
  } catch (err) {
    const host = document.getElementById('ing-help-schema');
    if (host) {
      host.innerHTML = '<p>The field list could not be loaded just now. '
        + 'Upload anything you have — every column is shown back to you for '
        + 'review before it is used.</p>';
    }
  }
  return close;
}

/* ═══════════════════════════════════════════════════════════════
   LOADING POP-UPS
   ═══════════════════════════════════════════════════════════════ */
function progressRingSvg(id, size, stroke) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return `<svg class="${id}" viewBox="0 0 ${size} ${size}">
      <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}"/>
      <circle class="fill" cx="${size / 2}" cy="${size / 2}" r="${r}"
        stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${c.toFixed(2)}" data-circumference="${c.toFixed(2)}"/>
    </svg>`;
}

function loadingOverlayHtml(cfg) {
  return `<div class="ing-loading-card">
      <div class="ing-loading-head">${I.sparkle}<div class="ing-loading-title">${ingEsc(cfg.title)}</div></div>
      <div class="ing-loading-sub">${ingEsc(cfg.subtitle)}</div>

      <div class="ing-loading-row">
        <div class="ing-loading-row-left">
          <span class="ing-loading-icon-stack">${I.filesStack}</span>
          <div>
            <div class="ing-loading-row-title">${ingEsc(cfg.summaryLabel)}</div>
            <div class="ing-loading-row-sub">${ingEsc(cfg.summarySub)}</div>
          </div>
        </div>
        <div class="ing-loading-row-right">
          <div class="ing-loading-status">${I.checkCircle}${ingEsc(cfg.summaryStatus)}</div>
          <div class="ing-loading-time">${ingEsc(cfg.summaryTime)}</div>
        </div>
      </div>

      <div class="ing-loading-row highlight">
        <div class="ing-loading-row-left">
          <span class="ing-agent-avatar">${I.agent}</span>
          <div>
            <div class="ing-loading-row-title">${ingEsc(cfg.agentName)}<span class="ing-live-pill">${I.waveform}LIVE</span></div>
            <div class="ing-loading-row-sub">${ingEsc(cfg.agentSub)}</div>
          </div>
        </div>
        <div class="ing-loading-row-right">
          <span class="ing-progress-pill"><span id="ing-loading-pct">0%</span> complete ${progressRingSvg('ing-progress-ring', 28, 3.5)}</span>
        </div>
      </div>

      <div class="ing-loading-row highlight">
        <div class="ing-loading-row-left">
          <span class="ing-sparkle-icon">${I.sparkle}</span>
          <div>
            <div class="ing-loading-row-title pulse" id="ing-loading-step-title">${ingEsc(cfg.stepTitle[0])}</div>
            <div class="ing-loading-row-sub" id="ing-loading-step-sub">${ingEsc(cfg.stepSub)}</div>
          </div>
        </div>
      </div>
    </div>`;
}

/** Show the shared loading pop-up, animate it to 100%, then call onDone.
 *
 *  Driven by a fixed-step setInterval rather than requestAnimationFrame:
 *  rAF's frame time is tied to the compositor, and in a throttled or
 *  headless tab that clock can stop advancing between frames, which
 *  turns a "run until real time catches up" rAF loop into a busy loop
 *  that never yields. A bounded step count can't do that — it always
 *  reaches 100% in exactly STEPS ticks and clears its own interval. */
function runLoadingOverlay(cfg, onDone) {
  const overlay = document.getElementById('loading-modal-overlay');
  if (!overlay) { onDone(); return; }

  overlay.innerHTML = loadingOverlayHtml(cfg);
  overlay.classList.add('active');

  const ring = overlay.querySelector('.ing-progress-ring .fill');
  const circumference = parseFloat(ring?.dataset.circumference || '0');
  const pctEl = overlay.querySelector('#ing-loading-pct');
  const stepTitleEl = overlay.querySelector('#ing-loading-step-title');

  const STEPS = 40;
  const STEP_MS = 55;
  let step = 0;

  const timer = setInterval(() => {
    step += 1;
    const pct = Math.min(100, Math.round((step / STEPS) * 100));
    if (pctEl) pctEl.textContent = `${pct}%`;
    if (ring) ring.style.strokeDashoffset = String(circumference * (1 - pct / 100));
    if (pct > 55 && stepTitleEl && stepTitleEl.textContent !== cfg.stepTitle[1]) {
      stepTitleEl.textContent = cfg.stepTitle[1];
    }
    if (step >= STEPS) {
      clearInterval(timer);
      setTimeout(() => {
        overlay.classList.remove('active');
        overlay.innerHTML = '';
        onDone();
      }, 350);
    }
  }, STEP_MS);
}

function startAiAnalysis() {
  const n = flow.files.length;
  runLoadingOverlay({
    title: 'AI is setting up your data',
    subtitle: 'Nexus Agent is analyzing your files, understanding columns and sampled values, and preparing smart mapping suggestions.',
    summaryLabel: `${n} document${n === 1 ? '' : 's'}`,
    summarySub: 'Upload successful',
    summaryStatus: 'Upload successful',
    summaryTime: 'Just now',
    agentName: 'Nexus Agent',
    agentSub: 'Working on your data in real time',
    stepTitle: ['Reading uploaded files...', 'Preparing suggested mapping...'],
    stepSub: 'Almost there!',
  }, () => {
    buildQueue();
    if (flow.queue.length) {
      showIngestionScreen(0);
    } else if (typeof window.enterApp === 'function') {
      window.enterApp();
    }
  });
}

function finishIngestion() {
  const n = flow.files.length;
  runLoadingOverlay({
    title: 'Building your logistics network',
    subtitle: 'Netgravity is assembling facilities, lanes, and constraints from your confirmed data into your digital twin.',
    summaryLabel: `${n} file${n === 1 ? '' : 's'} processed`,
    summarySub: 'Mapping confirmed',
    summaryStatus: 'Mapping confirmed',
    summaryTime: 'Just now',
    agentName: 'Network Agent',
    agentSub: 'Compiling your digital twin',
    stepTitle: ['Assembling network graph...', 'Finalizing network topology...'],
    stepSub: 'Almost there!',
  }, () => {
    hideIngestionPages();

    // Render the parsed topology immediately so the twin and tables are
    // populated while the network binds.
    if (flow.extractedNetwork && typeof loadNetworkData === 'function') {
      loadNetworkData(flow.extractedNetwork);
    }
    if (flow.project && typeof window.markProjectInProgress === 'function') {
      window.markProjectInProgress(flow.project.id);
    }
    // Reveal the shell WITHOUT hydrating: the network is not bound yet, so
    // `enterApp`'s own hydration would fail with NO_NETWORK_BOUND. The commit
    // and the analysis below run behind the analysis loading screen instead.
    if (typeof window.enterApp === 'function') window.enterApp({ hydrate: false });
    beginAnalysisLoading(
      (flow.project && flow.project.name) || 'your network', false);

    // Commit: assemble a CanonicalNetwork from the confirmed upload, register
    // it as a snapshot, bind it to this project — then hydrate every screen
    // from the authoritative KPI layer. Until this succeeds the user is
    // looking at their own topology but not yet at solved results, and the
    // banner says so rather than implying an analysis has run.
    const projectId = flow.projectId
      || (window.getCurrentProject && window.getCurrentProject()?.id)
      || getActiveProjectId();

    ingestionService.commitPreview(projectId)
      .then(async (res) => {
        setActiveProject(projectId);
        showNetworkNotice(
          `Network bound — ${res.network_summary.facilities} facilities, `
          + `${res.network_summary.lanes} lanes, `
          + `${res.network_summary.demands} demand rows. Running analysis…`,
          'info',
        );
        if (res.assumptions?.length) {
          console.info('Ingestion assumptions applied:', res.assumptions);
        }
        const report = await hydrateFromBackend(projectId, reportAnalysisStage);
        endAnalysisLoading();

        // "Analysis complete" used to be shown unconditionally, including when
        // the solver had proved the network infeasible and there was therefore
        // no figure below at all. A proved infeasibility is a real answer and
        // the most useful thing on the screen — so it is stated, with the
        // reason, instead of being papered over with a success message.
        if (report?.infeasible) {
          const why = (res.issues && res.issues.length)
            ? res.issues.join('  ')
            : (report.infeasibleReason
               || 'the solver proved no feasible plan exists for this network');
          showNetworkNotice(
            `No feasible plan exists for this network, so no cost or service `
            + `figure can be reported. ${why}`,
            'error',
          );
        } else if (report?.relaxed) {
          // Solved, but not fully. Every figure below describes a plan that
          // leaves part of the demand unserved, and saying "Analysis complete"
          // over the top of that would read as a clean bill of health.
          const short = report.relaxed.unservedDemand;
          const total = report.relaxed.totalDemand;
          const pct = (short != null && total)
            ? ` (${((short / total) * 100).toFixed(1)}% of demand)` : '';
          const why = (res.issues && res.issues.length) ? ` ${res.issues.join('  ')}` : '';
          showNetworkNotice(
            `Analysis complete, but your network cannot serve all of its demand `
            + `within its own service levels. The figures below are the best `
            + `achievable plan; `
            + `${short != null ? Number(short).toLocaleString() : 'some'} units`
            + `${pct} are left unserved.${why}`,
            'warning',
          );
        } else {
          showNetworkNotice(
            'Analysis complete. Every figure below is computed from your uploaded data.',
            'success',
          );
        }
      })
      .catch((err) => {
        endAnalysisLoading(`The network could not be built: ${err?.message || 'unknown error'}`);
        // Explicit. The user sees their topology but is told no analysis ran,
        // rather than being shown figures that describe nothing.
        showNetworkNotice(
          `Your files were parsed, but the network could not be built, so no `
          + `analysis has run: ${err?.message || 'unknown error'}`,
          'error',
        );
      });
  });
}

/** A dismissible banner on Home reporting the true state of the analysis. */
function showNetworkNotice(message, tone = 'info') {
  const host = document.getElementById('tab-home') || document.body;
  let el = document.getElementById('ng-network-notice');
  if (!el) {
    el = document.createElement('div');
    el.id = 'ng-network-notice';
    host.prepend(el);
  }
  const colour = tone === 'error' ? 'var(--red)'
    : tone === 'success' ? 'var(--green)'
    : tone === 'warning' ? 'var(--amber)' : 'var(--blue)';
  const bg = tone === 'error' ? 'var(--red-bg)'
    : tone === 'success' ? 'var(--green-bg)'
    : tone === 'warning' ? 'var(--amber-bg)' : 'var(--blue-bg)';
  el.setAttribute('role', 'status');
  el.style.cssText = `margin:0 0 var(--space-md);padding:10px 14px;border-radius:var(--r-md);`
    + `background:${bg};color:${colour};border:1px solid ${colour}33;`
    + `font-size:12.5px;font-weight:600`;
  el.textContent = message;
}

/* ═══════════════════════════════════════════════════════════════
   QUEUE
   ═══════════════════════════════════════════════════════════════ */
function buildQueue() {
  const excelLike = flow.files.filter(f => f.kind === 'excel' || f.kind === 'csv');
  const pdfs = flow.files.filter(f => f.kind === 'pdf');
  flow.queue = [...excelLike, ...pdfs].map(f => f.id);
  flow.queueIndex = 0;

  // No fallback mapping. A file the parser did not read has no columns to
  // review, and the screen says that instead of showing nine invented rows
  // under the stats "48 detected, 42 auto-mapped, 4 need review, 2 ignored" —
  // figures that were the same for every file ever uploaded.
  excelLike.forEach(f => {
    if (!flow.mapping[f.id]) flow.mapping[f.id] = [];
    if (!flow.mapStats[f.id]) {
      flow.mapStats[f.id] = { detected: 0, auto: 0, review: 0, ignored: 0 };
    }
  });
  pdfs.forEach(f => {
    if (!flow.pdfReview[f.id]) flow.pdfReview[f.id] = { expanded: false, reviewed: new Set() };
  });
}

function showIngestionScreen(index) {
  flow.queueIndex = index;
  const fileId = flow.queue[index];
  const file = flow.files.find(f => f.id === fileId);
  if (!file) { finishIngestion(); return; }

  if (typeof window.hideProjectPages === 'function') window.hideProjectPages();
  const upload = document.getElementById('upload-data-page');
  if (upload) upload.classList.add('hidden');

  if (file.kind === 'pdf') renderPdfIngestion(file);
  else renderExcelIngestion(file);

  const page = document.getElementById('ingestion-page');
  if (page) { page.classList.remove('hidden'); page.scrollTop = 0; }
}

function advanceQueue() {
  const next = flow.queueIndex + 1;
  if (next < flow.queue.length) showIngestionScreen(next);
  else finishIngestion();
}

function goBackInFlow() {
  if (flow.queueIndex > 0) { showIngestionScreen(flow.queueIndex - 1); return; }
  const page = document.getElementById('ingestion-page');
  if (page) page.classList.add('hidden');
  renderUploadData();
  const upload = document.getElementById('upload-data-page');
  if (upload) upload.classList.remove('hidden');
}

/* ═══════════════════════════════════════════════════════════════
   EXCEL / CSV INGESTION
   ═══════════════════════════════════════════════════════════════ */
function confidenceTone(c) { return c === 'high' ? 'high' : c === 'medium' ? 'medium' : 'low'; }
function confidenceIcon(c) { return c === 'high' ? I.check : c === 'medium' ? I.warning : I.ban; }
function confidenceIconTone(c) { return c === 'high' ? 'tone-green' : c === 'medium' ? 'tone-amber' : 'tone-red'; }

function actionPillHtml(row) {
  if (row.status === 'auto') return `<span class="ing-action-pill tone-auto">${I.check}Used</span>`;
  if (row.status === 'review') return `<span class="ing-action-pill tone-review">${I.warning}Review</span>`;
  // "Ignore or map" implied the column could be mapped to something useful.
  // It cannot: this build reads no field from it, and says so.
  return `<span class="ing-action-pill tone-ignored">${I.ban}Not used</span>`;
}

function mapRowHtml(row, idx) {
  const hidden = row.__filtered ? ' row-hidden' : '';
  const fields = schemaFields();
  // A server-suggested field that is somehow absent from the option list is
  // added rather than silently swapped for the list's first entry.
  const options = fields.includes(row.mapped) ? fields : [row.mapped, ...fields];
  const opts = options.map(f =>
    `<option value="${ingEsc(f)}"${f === row.mapped ? ' selected' : ''}>${ingEsc(f)}</option>`).join('');
  const unused = row.status === 'ignored';
  return `<tr class="${hidden.trim()}" data-row="${idx}">
      <td><span class="ing-map-source">${confidenceIcon(row.confidence).replace('<svg ', `<svg class="${confidenceIconTone(row.confidence)}" `)}${ingEsc(row.source)}</span></td>
      <td><span class="ing-map-sample" title="${ingEsc(row.sample)}">${ingEsc(row.sample)}</span></td>
      <td><select class="ing-map-select${unused ? ' no-match' : ''}" data-row-select="${idx}">${opts}</select></td>
      <td><span class="ing-confidence-pill tone-${confidenceTone(row.confidence)}">${row.confidence[0].toUpperCase()}${row.confidence.slice(1)}</span></td>
      <td>${actionPillHtml(row)}</td>
    </tr>`;
}

/* One table per sheet.
   A real workbook is 51 columns across 8 sheets; a single flat list of 51 rows
   gives no way to tell which "Capacity_Units" is which, which is exactly what
   this screen is for. Rows keep their index in the original array so the
   change handler and the stats still address the right row. */
function mappingTableHtml(rows, reviewOnly) {
  const indexed = rows.map((r, i) => ({ row: r, idx: i }));
  const bySheet = new Map();
  indexed.forEach(({ row, idx }) => {
    const key = row.sheet || '';
    if (!bySheet.has(key)) bySheet.set(key, []);
    bySheet.get(key).push({ row, idx });
  });

  const head = `<colgroup>
        <col class="c-source"><col class="c-sample"><col class="c-mapped">
        <col class="c-conf"><col class="c-status">
      </colgroup>
      <thead><tr>
        <th>Source column</th><th>Sample values</th><th>Mapped to field</th>
        <th>Confidence</th><th>Status</th>
      </tr></thead>`;

  const table = (entries) => `<table class="ing-map-table">${head}
      <tbody>${entries.map(({ row, idx }) =>
        mapRowHtml({ ...row, __filtered: reviewOnly && row.status === 'auto' }, idx)
      ).join('')}</tbody>
    </table>`;

  if (bySheet.size <= 1) return table(indexed);

  return [...bySheet.entries()].map(([sheet, entries]) => {
    const role = entries[0].row.sheetRole;
    const used = entries.filter(e => e.row.status !== 'ignored').length;
    return `<div class="ing-map-sheet-group">
        <div class="ing-map-sheet-head">
          <span class="ing-map-sheet-name">${ingEsc(sheet)}</span>
          <span class="ing-map-sheet-meta">${SHEET_ROLE_LABELS[role] || 'Not recognised'}
            &middot; ${used}/${entries.length} columns used</span>
        </div>
        ${table(entries)}
      </div>`;
  }).join('');
}

/* What the parser decided each sheet IS. Shown so the user can catch a
   misread sheet here, before it reaches the model. */
const SHEET_ROLE_LABELS = {
  facilities: 'Read as: facilities',
  markets: 'Read as: demand markets',
  lanes: 'Read as: lanes',
  products: 'Read as: products',
  demand_history: 'Read as: demand history',
  capacity_history: 'Read as: capacity history',
  lane_rates: 'Read as: freight rates',
  signals: 'Read as: external signals',
  unknown: 'Not recognised — no columns from this sheet are used',
};

function statsRowHtml(stats) {
  return `<div class="ing-stats-row">
      <div class="ing-stat-item">
        <span class="ing-stat-icon tone-purple">${I.columns}</span>
        <div><div class="ing-stat-value">${stats.detected}</div><div class="ing-stat-label">columns detected</div></div>
      </div>
      <div class="ing-stat-item">
        <span class="ing-stat-icon tone-green">${I.checkCircle}</span>
        <div><div class="ing-stat-value">${stats.auto}</div><div class="ing-stat-label">auto-mapped</div></div>
      </div>
      <div class="ing-stat-item">
        <span class="ing-stat-icon tone-amber">${I.warning}</span>
        <div><div class="ing-stat-value">${stats.review}</div><div class="ing-stat-label">need review</div></div>
      </div>
      <div class="ing-stat-item">
        <span class="ing-stat-icon tone-gray">${I.ban}</span>
        <div><div class="ing-stat-value">${stats.ignored}</div><div class="ing-stat-label">ignored</div></div>
      </div>
    </div>`;
}

// ─── S3 P0: Data Quality summary (record-level validity + issues) ───
// Distinct from the column-mapping stats above it (that answers "did we
// read the columns right?"; this answers "can I trust the data enough to
// run the model?"). Sourced entirely from DATA_QUALITY (data.js) — a
// fully-authored dataset that existed but was never surfaced anywhere in
// the ingestion flow. Reuses the same .ing-stats-row / .ing-review-item /
// .ing-confidence-pill classes already used elsewhere on this screen, so
// no new visual language is introduced.
function severityTone(sev) {
  return sev === 'warning' ? 'medium' : sev === 'info' ? 'high' : 'low';
}
function severityLabel(sev) {
  return sev === 'warning' ? 'Warning' : sev === 'info' ? 'Info' : 'Critical';
}

/* Data quality, as measured by the parser on the uploaded file.
   Every figure here is from `dataQuality` in the parse response. The demo
   dataset this once read described the prototype's own facilities ("Baddi
   Plant", "DC Delhi NCR") and was shown for any upload; its issue records also
   carried a `status` field the measured ones do not, so "need review" counted
   whatever the demo happened to say. */
function dataQualitySectionHtml() {
  const dq = DATA_QUALITY;
  const total = Number(dq.totalRecords) || 0;
  const valid = Number(dq.validRecords) || 0;
  const invalid = Math.max(0, total - valid);
  const issues = dq.issues || [];
  const warnings = issues.filter((i) => i.severity === 'warning').length;
  const critical = issues.filter((i) => i.severity === 'critical').length;

  const issueRows = issues.length ? issues.map((iss) => {
    // Measured issues are located by table and column; the demo ones used
    // facility/market/lane. Both are read so a mixed shape cannot blank the row.
    const location = iss.column || iss.table || iss.facility || iss.market
      || iss.lane || iss.source || '';
    return `<div class="ing-review-item">
        <div class="ing-review-item-text">
          <div class="ing-review-item-name">${ingEsc(iss.type || 'Issue')}${location ? ` — ${ingEsc(location)}` : ''}</div>
          <div class="ing-review-item-note">${ingEsc(iss.detail || '')}</div>
        </div>
        <span class="ing-confidence-pill tone-${severityTone(iss.severity)}">${severityLabel(iss.severity)}</span>
      </div>`;
  }).join('') : `<div class="ing-review-item"><div class="ing-review-item-text">
        <div class="ing-review-item-name">No issues found</div>
        <div class="ing-review-item-note">Every cell the parser read was usable.</div>
      </div></div>`;

  return `
    <div class="ing-card" id="ing-dq-section" style="margin-top:16px">
      <div class="ing-card-title">Data quality</div>
      <div class="ing-card-sub">Measured on your file. ${invalid
        ? `${invalid.toLocaleString()} of ${total.toLocaleString()} records need attention.`
        : `All ${total.toLocaleString()} records read cleanly.`}</div>
      <div class="ing-stats-row" style="margin-top:14px">
        <div class="ing-stat-item">
          <span class="ing-stat-icon tone-green">${I.checkCircle}</span>
          <div><div class="ing-stat-value">${valid.toLocaleString()} / ${total.toLocaleString()}</div><div class="ing-stat-label">records valid (${dq.validPct ?? '—'}%)</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon ${invalid > 0 ? 'tone-amber' : 'tone-green'}">${I.warning}</span>
          <div><div class="ing-stat-value">${invalid.toLocaleString()}</div><div class="ing-stat-label">invalid records</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon ${issues.length ? 'tone-amber' : 'tone-green'}">${I.warning}</span>
          <div><div class="ing-stat-value">${issues.length}</div><div class="ing-stat-label">issues found</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon ${critical ? 'tone-amber' : 'tone-gray'}">${I.ban}</span>
          <div><div class="ing-stat-value">${critical || warnings}</div><div class="ing-stat-label">${critical ? 'critical' : 'warnings'}</div></div>
        </div>
      </div>
      <div class="ing-dq-issue-list">${issueRows}</div>
    </div>`;
}

function renderExcelIngestion(file) {
  const page = document.getElementById('ingestion-page');
  if (!page) return;

  const rows = flow.mapping[file.id] || [];
  const stats = flow.mapStats[file.id] || { detected: 0, auto: 0, review: 0, ignored: 0 };
  const nRows = rowsAnalyzed(file);
  const rowsText = nRows === null ? 'Not read' : nRows.toLocaleString();
  const sheets = sheetsOf(file);
  const parseError = (flow.parseErrors || {})[file.id] || flow.parseError;
  // Share of columns this build actually reads. Was a fixed 99% under the
  // label "of fields are typically accepted as-is" — a statistic about no
  // particular file.
  const usedPct = stats.detected
    ? Math.round((stats.auto / stats.detected) * 100) : 0;
  const r = 26, c = 2 * Math.PI * r;

  page.innerHTML = `
    ${topbar()}
    <div class="proj-scroll">
      <div class="ing-body">

      <div class="ing-mapping-head">
        <div>
          <h1 class="ing-title">We mapped your data for you</h1>
          <p class="ing-subtitle">AI analyzed your Excel file using column names and sampled values. Review only the flagged fields, then continue.</p>
        </div>
        <div class="ing-file-summary-card">
          <span class="ing-file-summary-icon type-${file.kind}" style="background:${file.kind === 'pdf' ? '#c62d1f' : '#16794a'}">${file.ext.toUpperCase()}</span>
          <div>
            <div class="ing-file-summary-name">File: ${ingEsc(file.name)}</div>
            <div class="ing-file-summary-meta">
              <div><div class="ing-file-summary-meta-label">Rows read</div><div class="ing-file-summary-meta-value">${rowsText}</div></div>
              <div><div class="ing-file-summary-meta-label">Sheets</div><div class="ing-file-summary-meta-value">${sheets.length || '—'}</div></div>
              <div><div class="ing-file-summary-meta-label">File size</div><div class="ing-file-summary-meta-value">${formatFileSize(file.sizeBytes)}</div></div>
            </div>
            ${sheets.length ? `<div class="ing-file-summary-sheets">${sheets.map(s => `<span class="ing-sheet-chip">${ingEsc(s)}</span>`).join('')}</div>` : ''}
            <div class="ing-file-summary-badge">${parseError
              ? `<span class="ing-status-chip tone-error">${I.warning}Could not read this file</span>`
              : `<span class="ing-status-chip">${I.checkCircle}Read ${rowsText} rows across ${sheets.length} sheet(s)</span>`}</div>
          </div>
        </div>
      </div>

      ${statsRowHtml(stats)}
      ${dataQualitySectionHtml()}

      <div class="ing-mapping-layout">
        <div class="ing-mapping-main">
          <div class="ing-mapping-main-head">
            <div>
              <div class="ing-card-title">Field mapping</div>
              <div class="ing-card-sub">${stats.detected} column(s) read from your file.
                ${stats.ignored ? `${stats.ignored} are not used by the model.` : 'All are used by the model.'}
                Change any row that is wrong before continuing.</div>
            </div>
          </div>
          <div id="ing-map-table-slot">${rows.length
            ? mappingTableHtml(rows, false)
            : `<div class="ing-empty-files">${parseError
                ? `This file could not be parsed, so there is nothing to review: ${ingEsc(parseError)}`
                : 'No columns were read from this file.'}</div>`}</div>
        </div>

        <div class="ing-sidebar-card">
          <div class="ing-sidebar-title">${I.sparkle}What the AI used</div>
          <div class="ing-used-item">${I.columns}Column names</div>
          <div class="ing-used-item">${I.shuffle}Random sampled values</div>
          <div class="ing-used-item">${I.fingerprint}Pattern recognition</div>
          <div class="ing-used-item">${I.folder}Schema context</div>

          <div class="ing-accept-ring-wrap">
            <div class="ing-accept-ring">
              <svg viewBox="0 0 64 64">
                <circle class="track" cx="32" cy="32" r="${r}"/>
                <circle class="fill" cx="32" cy="32" r="${r}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - usedPct / 100)).toFixed(2)}"/>
              </svg>
              <div class="ing-accept-ring-pct">${usedPct}%</div>
            </div>
            <div class="ing-accept-ring-label">of this file's columns are read by the model</div>
          </div>
          <div class="ing-sidebar-note">A column marked "Not used" is parsed but reaches no calculation in this build.</div>
        </div>
      </div>

      <div class="ing-footer-row">
        <button type="button" class="ing-footer-link" id="ing-review-flagged-btn">Review flagged only</button>
        <button type="button" class="proj-btn-primary" id="ing-confirm-mapping-btn"><span>Confirm mapping &amp; continue</span>${I.arrowRight}</button>
      </div>
      </div>
    </div>`;

  bindExcelIngestion(file);
}

function refreshMapStats(file) {
  const stats = flow.mapStats[file.id];
  const wrap = document.querySelector('#ingestion-page .ing-stats-row');
  if (wrap) wrap.outerHTML = statsRowHtml(stats);
}

function bindExcelIngestion(file) {
  let reviewOnly = false;

  document.getElementById('ing-map-table-slot')?.addEventListener('change', e => {
    const sel = e.target.closest('[data-row-select]');
    if (!sel) return;
    const idx = Number(sel.dataset.rowSelect);
    const row = flow.mapping[file.id][idx];
    const wasResolved = row.status === 'auto';
    row.mapped = sel.value;

    if (sel.value === 'No match found') {
      row.confidence = 'low';
      row.status = 'ignored';
    } else {
      row.confidence = 'high';
      row.status = 'auto';
    }

    if (!wasResolved) {
      const stats = flow.mapStats[file.id];
      if (row.status === 'auto') {
        stats.auto += 1;
        if (sel.dataset.prevStatus === 'ignored') stats.ignored -= 1; else stats.review -= 1;
      }
    }
    sel.dataset.prevStatus = row.status;

    // Re-render just this row's pills/select styling.
    const tr = sel.closest('tr');
    if (tr) tr.outerHTML = mapRowHtml({ ...row, __filtered: reviewOnly && row.status === 'auto' }, idx);
    refreshMapStats(file);
  });

  document.getElementById('ing-review-flagged-btn')?.addEventListener('click', (e) => {
    reviewOnly = !reviewOnly;
    e.target.textContent = reviewOnly ? 'Show all fields' : 'Review flagged only';
    document.getElementById('ing-map-table-slot').innerHTML = mappingTableHtml(flow.mapping[file.id], reviewOnly);
  });

  bindWorkspaceTopbar(document.getElementById('ingestion-page'), { help: 'upload' });
  document.querySelector('#ingestion-page .ing-back-home-btn')?.addEventListener('click', goBackInFlow);
  document.getElementById('ing-confirm-mapping-btn')?.addEventListener('click', () => {
    file.status = 'mapped';
    advanceQueue();
  });

  // Confirming a mapping that does not exist would carry an unparsed file into
  // the network build, where it can only fail later and less clearly.
  const confirmBtn = document.getElementById('ing-confirm-mapping-btn');
  if (confirmBtn && !(flow.mapping[file.id] || []).length) {
    confirmBtn.disabled = true;
    confirmBtn.title = 'This file produced no readable columns.';
  }
}

/* ═══════════════════════════════════════════════════════════════
   PDF INGESTION
   ═══════════════════════════════════════════════════════════════ */
function findingCardsHtml(file, review) {
  const vendor = CONTRACT_VENDOR;
  const findTerm = (field) => vendor.extractedTerms.find((t) => t.field === field);
  const baseRate = findTerm('Base Rate');
  const fuelSurcharge = findTerm('Fuel Surcharge');
  const effectiveDate = findTerm('Effective Date');
  const nsl = findTerm('Non-Serviceable Surcharge');
  const minVolume = findTerm('Minimum Volume');
  const status = effectiveDate ? contractStatus(effectiveDate.value) : { label: 'Not available', tone: 'gray' };

  const reviewTerms = getReviewTerms(vendor);
  const flaggedCount = reviewTerms.length - review.reviewed.size;
  const warnClass = `ing-finding-card warn${review.expanded ? ' expanded' : ''}`;
  const reviewItems = reviewTerms.map((t) => {
    const id = reviewTermId(t);
    const done = review.reviewed.has(id);
    return `<div class="ing-review-item${done ? ' reviewed' : ''}">
        <div>
          <div class="ing-review-item-name">${ingEsc(t.field)}: ${ingEsc(t.value)}</div>
          <div class="ing-review-item-note">${done ? 'Reviewed' : 'Medium confidence — please verify'}</div>
        </div>
        <button type="button" class="ing-review-mark-btn" data-mark-term="${id}" ${done ? 'disabled' : ''}>${done ? 'Reviewed' : 'Mark reviewed'}</button>
      </div>`;
  }).join('');

  const hiddenCostAlertCard = vendor.hiddenCostAlert ? `
    <div class="ing-finding-card warn">
      <span class="ing-finding-icon">${I.shield}</span>
      <div><div class="ing-finding-title">Hidden cost alert</div><div class="ing-finding-desc">Headline rate is <strong>${ingEsc(vendor.headlineRate)}</strong>, but effective cost with surcharges applied is <span class="lowconf">${ingEsc(vendor.effectiveCost)}</span>.</div></div>
    </div>` : '';

  return `
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.docSearch}</span>
      <div><div class="ing-finding-title">Contract document detected</div><div class="ing-finding-desc">${ingEsc(vendor.name)} — transportation services agreement.</div></div>
    </div>
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.checkCircle}</span>
      <div><div class="ing-finding-title">Contract status</div><div class="ing-finding-value">${status.label}</div><div class="ing-finding-desc">${effectiveDate ? `Effective from ${ingEsc(effectiveDate.value)}` : 'Not available'}</div></div>
    </div>
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.rupee}</span>
      <div><div class="ing-finding-title">Base freight rate identified</div><div class="ing-finding-value">${baseRate ? ingEsc(baseRate.value) : 'Not available'}</div><div class="ing-finding-desc">Applies to standard road movement across contracted primary lanes.</div></div>
    </div>
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.fuel}</span>
      <div><div class="ing-finding-title">Fuel surcharge identified</div><div class="ing-finding-value">${fuelSurcharge ? ingEsc(fuelSurcharge.value) : 'Not available'}</div><div class="ing-finding-desc">Applicable per shipment per the contracted rate card.</div></div>
    </div>
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.calendar}</span>
      <div><div class="ing-finding-title">Effective period identified</div><div class="ing-finding-value">${effectiveDate ? `From ${ingEsc(effectiveDate.value)}` : 'Not available'}</div><div class="ing-finding-desc">No contract end date was found in this document.</div></div>
    </div>
    ${nsl ? `
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.ban}</span>
      <div><div class="ing-finding-title">Non-serviceable location (NSL) surcharge</div><div class="ing-finding-value">${ingEsc(nsl.value)}</div></div>
    </div>` : ''}
    ${minVolume ? `
    <div class="ing-finding-card">
      <span class="ing-finding-icon">${I.columns}</span>
      <div><div class="ing-finding-title">Minimum volume commitment</div><div class="ing-finding-value">${ingEsc(minVolume.value)}</div></div>
    </div>` : ''}
    ${hiddenCostAlertCard}
    <div class="${warnClass}" id="ing-warn-card">
      <span class="ing-finding-icon">${I.shield}</span>
      <div><div class="ing-finding-title">${flaggedCount} term${flaggedCount === 1 ? '' : 's'} need${flaggedCount === 1 ? 's' : ''} your review</div>
        <div class="ing-finding-desc">${reviewTerms.map((t) => t.field).join(' and ')} ${reviewTerms.length === 1 ? 'has' : 'have'} <span class="lowconf">medium confidence</span> values.</div></div>
      <span class="ing-finding-chevron">${I.chevronRight}</span>
    </div>
    <div class="ing-review-panel" id="ing-review-panel">${reviewItems}</div>`;
}

function renderPdfIngestion(file) {
  const page = document.getElementById('ingestion-page');
  if (!page) return;

  const review = flow.pdfReview[file.id];
  // Derived from the file name: there is no PDF parser in this build, so the
  // page count is not a measurement either. Shown as unknown rather than as a
  // number nobody counted.
  const highConfidence = CONTRACT_VENDOR.extractedTerms.filter((t) => t.confidence === 'HIGH').length;

  page.innerHTML = `
    ${topbar()}
    <div class="proj-scroll">
      <div class="ing-body">

      <div class="ing-pdf-layout">
        <h1 class="ing-title" style="font-size:calc(26*var(--u))">Contract terms</h1>
        <p class="ing-subtitle">Your file has been uploaded and stored.</p>

        <!-- The Excel/CSV path parses the real file. This one does not: there
             is no contract extractor in this build, and the terms below are a
             worked example. Presenting them as "what I've found in your
             document" would put a rate card nobody read in front of a user who
             would reasonably act on it. -->
        <div class="ing-demo-notice">
          <strong>No terms were extracted from this PDF.</strong>
          This build has no contract parser, so nothing below was read from your
          file. The terms shown are a worked example of the output format, and
          none of them reach the optimiser — freight rates come from your
          spreadsheet, not from an uploaded contract.
        </div>

        <div class="ing-pdf-file-card">
          <span class="ing-file-icon type-pdf" style="width:calc(40*var(--u));height:calc(40*var(--u))">PDF</span>
          <div class="ing-pdf-file-meta">
            <div class="ing-pdf-file-name">${ingEsc(file.name)}</div>
            <div class="ing-pdf-file-sub">${formatFileSize(file.sizeBytes)} &middot; stored, not parsed</div>
          </div>
          <span class="ing-status-chip">${I.checkCircle}Upload successful</span>
        </div>

        <div class="ing-findings-title">${I.docSearch}Example of extracted terms</div>
        <div id="ing-findings-slot" class="ing-findings-grid">${findingCardsHtml(file, review)}</div>
      </div>

      <div class="ing-footer-row">
        <div class="ing-pdf-actions" style="margin-top:0;flex:1;max-width:calc(560*var(--u));margin-left:auto">
          <div class="ing-pdf-actions-col">
            <button type="button" class="proj-btn-primary" id="ing-pdf-continue-btn"><span>Continue &amp; confirm</span>${I.arrowRight}</button>
            <div class="ing-pdf-action-caption">Continue — no contract term is carried into the model</div>
          </div>
          <div class="ing-pdf-actions-col">
            <button type="button" class="ing-btn-secondary" id="ing-pdf-review-btn" style="justify-content:center"><span>Review extracted terms</span>${I.arrowRight}</button>
            <div class="ing-pdf-action-caption">Review ${getReviewTerms(CONTRACT_VENDOR).length - review.reviewed.size} term${getReviewTerms(CONTRACT_VENDOR).length - review.reviewed.size === 1 ? '' : 's'} that may need changes</div>
          </div>
        </div>
      </div>
      </div>
    </div>`;

  bindPdfIngestion(file);
}

function bindPdfIngestion(file) {
  const review = flow.pdfReview[file.id];

  bindWorkspaceTopbar(document.getElementById('ingestion-page'), { help: 'upload' });
  document.querySelector('#ingestion-page .ing-back-home-btn')?.addEventListener('click', goBackInFlow);

  function refreshFindings() {
    document.getElementById('ing-findings-slot').innerHTML = findingCardsHtml(file, review);
  }

  document.getElementById('ing-findings-slot')?.addEventListener('click', e => {
    const markBtn = e.target.closest('[data-mark-term]');
    if (markBtn) {
      review.reviewed.add(markBtn.dataset.markTerm);
      refreshFindings();
      const caption = document.querySelectorAll('.ing-pdf-action-caption')[1];
      const remaining = getReviewTerms(CONTRACT_VENDOR).length - review.reviewed.size;
      if (caption) caption.textContent = `Review ${remaining} term${remaining === 1 ? '' : 's'} that may need changes`;
      return;
    }
    if (e.target.closest('#ing-warn-card')) {
      review.expanded = !review.expanded;
      refreshFindings();
    }
  });

  document.getElementById('ing-pdf-review-btn')?.addEventListener('click', () => {
    review.expanded = true;
    refreshFindings();
    document.getElementById('ing-warn-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  document.getElementById('ing-pdf-continue-btn')?.addEventListener('click', () => {
    file.status = 'extracted';
    advanceQueue();
  });
}

/* ═══════════════════════════════════════════════════════════════
   Entry points / navigation
   ═══════════════════════════════════════════════════════════════ */
export function showUploadData(project) {
  flow.project = project || null;
  flow.dataset = null;
  flow.files = [];
  flow.queue = [];
  flow.queueIndex = 0;
  flow.mapping = {};
  flow.mapStats = {};
  flow.pdfReview = {};

  const shell = document.querySelector('.app-shell');
  // Only 'flex' means the app was actually showing — a fresh page load
  // never explicitly sets this style at all, so checking "!== 'none'"
  // would wrongly treat that empty string as "was showing".
  flow.cameFromApp = !!(shell && shell.style.display === 'flex');

  if (typeof window.hideProjectPages === 'function') window.hideProjectPages();
  const landing = document.getElementById('landing-page');
  if (landing) { landing.classList.add('hidden'); landing.style.display = 'none'; }
  if (shell) shell.style.display = 'none';
  const fab = document.getElementById('floating-chatbot-fab');
  if (fab) fab.style.display = 'none';

  hideIngestionPages();
  renderUploadData();
  const page = document.getElementById('upload-data-page');
  if (page) { page.classList.remove('hidden'); page.scrollTop = 0; }

  // Then fetch what this project is already running on, and re-render. Async
  // so the uploader is usable immediately; a project with no dataset simply
  // renders nothing extra.
  loadCurrentDataset();
}

/**
 * The dataset this project's analysis currently runs on, from the server.
 *
 * This is the audit trail: the file, the mapping decisions as confirmed, the
 * measured quality, the cross-sheet integrity findings, every assumption the
 * assembly had to make, and when it was committed.
 */
async function loadCurrentDataset() {
  // `getActiveProjectId()` FIRST: it is the id every authoritative request is
  // already scoped by, and it is set whether the project was opened from the
  // picker or restored from storage on a reload. `flow.project` and
  // `getCurrentProject()` are both null on a restored session, which is
  // exactly the case this panel exists for.
  const projectId = getActiveProjectId()
    || flow.project?.id
    || (window.getCurrentProject && window.getCurrentProject()?.id);
  if (!projectId) return;
  try {
    const res = await ingestionService.getDataset(projectId);
    flow.dataset = (res && res.status && res.status !== 'NO_DATA') ? res : null;
  } catch (e) {
    // A missing audit record is not an error worth blocking the uploader for,
    // but it must not render as "nothing was uploaded" either.
    flow.dataset = null;
  }
  if (flow.dataset) renderUploadData();
}

/** The read-only record of the dataset this project is analysing. */
function currentDatasetHtml() {
  const d = flow.dataset;
  if (!d) return '';
  const c = d.committed;
  if (!c) {
    if (!d.preview) return '';
    return `
      <div class="ing-card">
        <div class="ing-card-title">Parsed, not yet analysed</div>
        <div class="ing-card-sub">This upload has been read but not confirmed,
          so no KPI runs against it yet.</div>
      </div>`;
  }

  const q = c.dataQuality || {};
  const stats = c.mapStats || {};
  const when = c.committed_at
    ? new Date(c.committed_at * 1000).toLocaleString() : 'Not recorded';
  const files = (c.files || []).map(f =>
    `<li>${ingEsc(f.name)} &mdash; ${Number(f.rows || 0).toLocaleString()} rows
       across ${(f.sheets || []).length} sheet(s)</li>`).join('');
  const integrity = (c.integrity || []).map(i =>
    `<li style="color:var(--red)">${ingEsc(i.detail)}</li>`).join('');
  const assumptions = (c.assumptions || []).map(a =>
    `<li>${ingEsc(a)}</li>`).join('');
  const issues = (c.issues || []).map(i => `<li>${ingEsc(i)}</li>`).join('');
  const geo = (c.geography || {}).region;

  return `
    <div class="ing-card">
      <div class="ing-card-head-row">
        <div class="ing-card-title">Current dataset
          <span class="ing-count-badge">(${(c.files || []).length} file)</span></div>
        <span class="ing-status-chip">${I.checkCircle}Analysing</span>
      </div>
      <div class="ing-card-sub">Committed ${ingEsc(when)} &middot; snapshot
        <code>${ingEsc(c.snapshot_id || '')}</code></div>

      <ul class="text-sm" style="margin:10px 0 0 18px;line-height:1.8">${files}</ul>

      <div class="ing-stats-row" style="margin-top:12px">
        <div class="ing-stat-item"><div><div class="ing-stat-value">${stats.auto ?? '—'} / ${stats.detected ?? '—'}</div>
             <div class="ing-stat-label">columns mapped</div></div></div>
        <div class="ing-stat-item"><div><div class="ing-stat-value">${stats.ignored ?? '—'}</div>
             <div class="ing-stat-label">not used by the model</div></div></div>
        <div class="ing-stat-item"><div><div class="ing-stat-value">${q.validPct ?? '—'}%</div>
             <div class="ing-stat-label">records valid</div></div></div>
        <div class="ing-stat-item"><div><div class="ing-stat-value">${c.currency || '—'}</div>
             <div class="ing-stat-label">currency${geo ? ' &middot; ' + ingEsc(geo) : ''}</div></div></div>
      </div>

      ${integrity ? `<div class="ing-card-title" style="font-size:13px;margin-top:16px">
          Referential integrity</div>
        <ul class="text-sm" style="margin:6px 0 0 18px;line-height:1.7">${integrity}</ul>` : ''}

      ${issues ? `<div class="ing-card-title" style="font-size:13px;margin-top:16px">
          What the network cannot serve</div>
        <ul class="text-sm" style="margin:6px 0 0 18px;line-height:1.7">${issues}</ul>` : ''}

      ${assumptions ? `<details style="margin-top:16px">
          <summary class="ing-card-title" style="font-size:13px;cursor:pointer">
            Assumptions this reading had to make (${(c.assumptions || []).length})</summary>
          <ul class="text-sm" style="margin:8px 0 0 18px;line-height:1.7">${assumptions}</ul>
        </details>` : ''}

      <div class="text-xs" style="color:var(--text-2);margin-top:14px">
        Uploading a new file below replaces this dataset once you confirm the
        mapping. Nothing changes until you do.
      </div>
    </div>`;
}

export function hideIngestionPages() {
  ['upload-data-page', 'ingestion-page'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  });
  const overlay = document.getElementById('loading-modal-overlay');
  if (overlay) { overlay.classList.remove('active'); overlay.innerHTML = ''; }
}

export function initIngestion() {
  if (typeof window !== 'undefined') {
    window.showUploadData = showUploadData;
    window.hideIngestionPages = hideIngestionPages;
  }
}
