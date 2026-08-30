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
 * product spec gave), each rendered by its own template. All extraction
 * content is mocked — there is no real parsing — so it's generated
 * deterministically from the file's name/size so the same file always
 * shows the same numbers.
 *
 * STATUS: PROTOTYPE / MOCKED
 */

import { DATA_QUALITY, CONTRACT_DEMO, SOURCE_CONTACTS } from './data.js';

const SCHEMA_FIELDS = [
  'Customer ID', 'Distribution Centre', 'Demand Market', 'Service Level SLA',
  'Transit Time (Days)', 'Facility Name', 'Region / Zone', 'Total Cost', 'No match found',
];

/* Base mapping template — mirrors Dump/Excel ingestion.jpeg exactly.
   Re-used for every Excel/CSV file; only the file summary (name, rows,
   timestamp) is bound to the actual upload. */
function baseMappingRows() {
  return [
    { source: 'customer_id', sample: 'C1021, C2044, C3198', mapped: 'Customer ID', confidence: 'high', status: 'auto' },
    { source: 'origin_dc', sample: 'Mumbai DC, Delhi NCR, Kolkata DC', mapped: 'Distribution Centre', confidence: 'high', status: 'auto' },
    { source: 'destination_market', sample: 'Lucknow, Patna, Bengaluru', mapped: 'Demand Market', confidence: 'high', status: 'auto' },
    { source: 'sla_target', sample: '95%, 96%, 94%', mapped: 'Service Level SLA', confidence: 'high', status: 'auto' },
    { source: 'transit_days', sample: '2, 3, 5', mapped: 'Transit Time (Days)', confidence: 'medium', status: 'review' },
    { source: 'whse_name', sample: 'Ahmedabad Hub, Baddi Plant', mapped: 'Facility Name', confidence: 'medium', status: 'review' },
    { source: 'zone_code', sample: 'N1, E2, S1', mapped: 'Region / Zone', confidence: 'medium', status: 'review' },
    { source: 'amt_rs', sample: '120000, 540000, 88000', mapped: 'Total Cost', confidence: 'high', status: 'auto' },
    { source: 'misc_ref', sample: 'alpha-09, batch-x, internal', mapped: 'No match found', confidence: 'low', status: 'ignored' },
  ];
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

/* ─── Deterministic mock numbers (no real parsing happens) ───── */
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function mockRowsAnalyzed(file) { return 6000 + (hashStr(file.name) % 9000); }
function mockPages(file) { return 8 + (hashStr(file.name) % 34); }

/* ─── Icons ──────────────────────────────────────────────────── */
const I = {
  chevronRight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>`,
  chevronLeft: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18"/></svg>`,
  arrowRight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`,
  help: `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`,
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
};

/* ─── Flow flow ─────────────────────────────────────────────── */
const flow = {
  project: null,
  files: [],          // [{ id, name, ext, kind: 'excel'|'csv'|'pdf', sizeMB, uploadedAt, status }]
  queue: [],          // subset of files.id, ordered excel/csv first then pdf
  queueIndex: 0,
  mapping: {},         // fileId -> row[] (mutable copy of baseMappingRows())
  mapStats: {},        // fileId -> { detected, auto, review, ignored }
  pdfReview: {},       // fileId -> { expanded: bool, reviewed: Set<termId> }
  cameFromApp: false,  // true when entered mid-session (app shell was showing), so Back should return there rather than to Create Project
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

const LOGO_SVG = `<svg class="ing-brand-logo" viewBox="0 0 48 48" fill="none">
      <line x1="10" y1="10" x2="10" y2="38" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="38" y1="10" x2="38" y2="38" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="12" y1="12" x2="36" y2="36" stroke="#9218EA" stroke-width="4" stroke-linecap="round"/>
      <circle cx="10" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="10" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
    </svg>`;

/* ─── Shared chrome ──────────────────────────────────────────── */
function topbar() {
  return `<div class="ing-topbar">
      <div class="ing-topbar-left">
        <div class="ing-brand">
          ${LOGO_SVG}
          <span class="ing-brand-name">Netgravity</span>
        </div>
      </div>
      <div class="ing-topbar-right">
        <button class="ing-back-home-btn" type="button">${I.chevronLeft}<span>Back</span></button>
        <button class="topbar-icon-btn" type="button" title="Help & Documentation">${I.help}</button>
        <div class="user-avatar-ak" title="Amit Kumar">AK</div>
      </div>
    </div>`;
}

/* Returns to whichever screen led into this flow: the app shell, exactly
   as it was, if this was a mid-session upload; otherwise Create Project.
   Used by the Upload Data screen — the first step, so "back" leaves the
   ingestion flow entirely. */
function goBack() {
  if (flow.cameFromApp) {
    hideIngestionPages();
    const landing = document.getElementById('landing-page');
    if (landing) landing.style.display = 'none';
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
function fileRowHtml(f) {
  const label = f.kind === 'pdf' ? 'PDF' : f.kind.toUpperCase();
  const sub = f.kind === 'pdf' ? 'Contract or rate document' : 'Tabular network dataset';
  return `<tr data-file-id="${f.id}">
      <td>
        <div class="ing-file-name-cell">
          <span class="ing-file-icon type-${f.kind}">${f.ext.toUpperCase()}</span>
          <div>
            <div class="ing-file-meta-name">${ingEsc(f.name)}</div>
            <div class="ing-file-meta-sub">${sub}</div>
          </div>
        </div>
      </td>
      <td><span class="ing-type-chip type-${f.kind}">${label}</span></td>
      <td>${f.sizeMB.toFixed(1)} MB</td>
      <td>${ingEsc(f.uploadedAt)}</td>
      <td><span class="ing-status-chip">${I.checkCircle}Uploaded</span></td>
      <td><button class="ing-row-delete" type="button" data-remove="${f.id}" title="Remove file">${I.trash}</button></td>
    </tr>`;
}

function renderUploadData() {
  const page = document.getElementById('upload-data-page');
  if (!page) return;

  const rows = flow.files.map(fileRowHtml).join('');
  const tableOrEmpty = flow.files.length
    ? `<table class="ing-file-table">
        <thead><tr><th>File name</th><th>Type</th><th>Size</th><th>Uploaded on</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`
    : `<div class="ing-empty-files">No files uploaded yet — add at least one to continue.</div>`;

  page.innerHTML = `
    <div class="ing-body">
      ${topbar()}
      <h1 class="ing-title">Upload &amp; Align Network Datasets</h1>
      <p class="ing-subtitle">Upload CSVs, Excel order books, or Rate Card PDFs. NetGravity's AI will automatically align and prepare your data.</p>

      <div class="ing-card">
        <div class="ing-card-title">1. Upload Datasets</div>
        <div class="ing-card-sub">Supported formats: Excel (.xlsx, .xls), CSV (.csv), PDF (.pdf) &middot; up to 25MB each</div>
        <div class="ing-dropzone" id="ing-dropzone" tabindex="0" role="button">
          <span class="ing-dropzone-icon">${I.uploadCloud}</span>
          <div class="ing-dropzone-main">Drag &amp; drop your files here</div>
          <div class="ing-dropzone-or">or</div>
          <button type="button" class="ing-attach-btn" id="ing-attach-btn">${I.paperclip}<span>Attach files</span></button>
        </div>
        <input type="file" id="ing-file-input" accept=".xlsx,.xls,.csv,.pdf" multiple hidden />
        <div class="ing-error" id="ing-upload-error"></div>
      </div>

      <div class="ing-card">
        <div class="ing-card-head-row">
          <div class="ing-card-title">2. Uploaded Files <span class="ing-count-badge">(${flow.files.length})</span></div>
          <button type="button" class="ing-add-more" id="ing-add-more">${I.paperclip}<span>Add more files</span></button>
        </div>
        <div id="ing-file-table-slot">${tableOrEmpty}</div>
      </div>

      <div class="ing-footer-bar">
        <button type="button" class="ing-skip-link" id="ing-skip-btn">Skip for now, I'll upload data later</button>
        <button type="button" class="proj-btn-primary" id="ing-continue-btn" ${flow.files.length ? '' : 'disabled'}>
          <span>Continue to AI Analysis</span>${I.arrowRight}
        </button>
      </div>
    </div>`;

  bindUploadData();
}

function refreshFileTable() {
  const slot = document.getElementById('ing-file-table-slot');
  if (slot) {
    slot.innerHTML = flow.files.length
      ? `<table class="ing-file-table">
          <thead><tr><th>File name</th><th>Type</th><th>Size</th><th>Uploaded on</th><th>Status</th><th></th></tr></thead>
          <tbody>${flow.files.map(fileRowHtml).join('')}</tbody>
        </table>`
      : `<div class="ing-empty-files">No files uploaded yet — add at least one to continue.</div>`;
  }
  const badge = document.querySelector('#upload-data-page .ing-count-badge');
  if (badge) badge.textContent = `(${flow.files.length})`;
  const continueBtn = document.getElementById('ing-continue-btn');
  if (continueBtn) continueBtn.disabled = flow.files.length === 0;
}

function addFiles(fileList) {
  const errorEl = document.getElementById('ing-upload-error');
  if (errorEl) errorEl.textContent = '';
  const rejected = [];

  Array.from(fileList || []).forEach(file => {
    const { ext, kind } = classifyExt(file.name);
    if (!kind) { rejected.push(`${file.name} (unsupported type)`); return; }
    if (file.size > 25 * 1024 * 1024) { rejected.push(`${file.name} (over 25MB)`); return; }
    flow.files.push({
      id: nextId('file'),
      name: file.name,
      ext,
      kind,
      sizeMB: file.size / 1024 / 1024,
      uploadedAt: 'Just now',
      status: 'uploaded',
    });
  });

  if (rejected.length && errorEl) {
    errorEl.textContent = `Couldn't add: ${rejected.join(', ')}.`;
  }
  refreshFileTable();
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

  document.getElementById('upload-data-page')?.addEventListener('click', e => {
    const btn = e.target.closest('[data-remove]');
    if (!btn) return;
    flow.files = flow.files.filter(f => f.id !== btn.dataset.remove);
    refreshFileTable();
  });

  document.getElementById('ing-skip-btn')?.addEventListener('click', () => {
    if (typeof window.enterApp === 'function') window.enterApp();
  });

  document.getElementById('ing-continue-btn')?.addEventListener('click', () => {
    if (!flow.files.length) return;
    startAiAnalysis();
  });
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
    if (flow.project && typeof window.markProjectInProgress === 'function') {
      window.markProjectInProgress(flow.project.id);
    }
    if (typeof window.enterApp === 'function') window.enterApp();
  });
}

/* ═══════════════════════════════════════════════════════════════
   QUEUE
   ═══════════════════════════════════════════════════════════════ */
function buildQueue() {
  const excelLike = flow.files.filter(f => f.kind === 'excel' || f.kind === 'csv');
  const pdfs = flow.files.filter(f => f.kind === 'pdf');
  flow.queue = [...excelLike, ...pdfs].map(f => f.id);
  flow.queueIndex = 0;

  excelLike.forEach(f => {
    if (!flow.mapping[f.id]) {
      flow.mapping[f.id] = baseMappingRows();
      flow.mapStats[f.id] = { detected: 48, auto: 42, review: 4, ignored: 2 };
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
  if (row.status === 'auto') return `<span class="ing-action-pill tone-auto">${I.check}Auto-mapped</span>`;
  if (row.status === 'review') return `<span class="ing-action-pill tone-review">${I.warning}Review</span>`;
  return `<span class="ing-action-pill tone-ignored">${I.ban}Ignore or map</span>`;
}

function mapRowHtml(row, idx) {
  const hidden = row.__filtered ? ' row-hidden' : '';
  const opts = SCHEMA_FIELDS.map(f =>
    `<option value="${ingEsc(f)}"${f === row.mapped ? ' selected' : ''}>${ingEsc(f)}</option>`).join('');
  return `<tr class="${hidden.trim()}" data-row="${idx}">
      <td><span class="ing-map-source">${confidenceIcon(row.confidence).replace('<svg ', `<svg class="${confidenceIconTone(row.confidence)}" `)}${ingEsc(row.source)}</span></td>
      <td><span class="ing-map-sample" title="${ingEsc(row.sample)}">${ingEsc(row.sample)}</span></td>
      <td><select class="ing-map-select${row.mapped === 'No match found' ? ' no-match' : ''}" data-row-select="${idx}">${opts}</select></td>
      <td><span class="ing-confidence-pill tone-${confidenceTone(row.confidence)}">${row.confidence[0].toUpperCase()}${row.confidence.slice(1)}</span></td>
      <td>${actionPillHtml(row)}</td>
    </tr>`;
}

function mappingTableHtml(rows, reviewOnly) {
  return `<table class="ing-map-table">
      <thead><tr><th>Source column</th><th>Sample values</th><th>Mapped to schema</th><th>Confidence <span title="AI's confidence in this suggestion">ⓘ</span></th><th>Status / Action</th></tr></thead>
      <tbody>${rows.map((r, i) => mapRowHtml({ ...r, __filtered: reviewOnly && r.status === 'auto' }, i)).join('')}</tbody>
    </table>`;
}

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

function dataQualitySectionHtml() {
  const dq = DATA_QUALITY;
  const invalid = dq.totalRecords - dq.validRecords;
  const needsReview = dq.issues.filter((i) => i.status === 'needs_review').length;

  const issueRows = dq.issues.map((iss) => {
    const done = iss.status === 'reviewed' || iss.status === 'auto_mapped';
    const location = iss.facility || iss.market || iss.lane || iss.source || '';
    return `<div class="ing-review-item${done ? ' reviewed' : ''}">
        <div>
          <div class="ing-review-item-name">${ingEsc(iss.type)}${location ? ` — ${ingEsc(location)}` : ''}</div>
          <div class="ing-review-item-note">${ingEsc(iss.detail)}</div>
        </div>
        <span class="ing-confidence-pill tone-${severityTone(iss.severity)}">${severityLabel(iss.severity)}</span>
      </div>`;
  }).join('');

  return `
    <div class="ing-card" id="ing-dq-section" style="margin-top:16px">
      <div class="ing-card-title">Data Quality</div>
      <div class="ing-card-sub">Can this data be trusted to run the model? ${invalid} of ${dq.totalRecords.toLocaleString('en-IN')} records need attention.</div>
      <div class="ing-stats-row" style="margin-top:14px">
        <div class="ing-stat-item">
          <span class="ing-stat-icon tone-green">${I.checkCircle}</span>
          <div><div class="ing-stat-value">${dq.validRecords.toLocaleString('en-IN')} / ${dq.totalRecords.toLocaleString('en-IN')}</div><div class="ing-stat-label">records valid (${dq.validPct}%)</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon ${invalid > 0 ? 'tone-amber' : 'tone-green'}">${I.warning}</span>
          <div><div class="ing-stat-value">${invalid}</div><div class="ing-stat-label">invalid records</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon tone-amber">${I.warning}</span>
          <div><div class="ing-stat-value">${dq.issues.length}</div><div class="ing-stat-label">issues found</div></div>
        </div>
        <div class="ing-stat-item">
          <span class="ing-stat-icon tone-gray">${I.ban}</span>
          <div><div class="ing-stat-value">${needsReview}</div><div class="ing-stat-label">need review</div></div>
        </div>
      </div>
      <div style="margin-top:14px;display:flex;flex-direction:column;gap:8px">${issueRows}</div>
    </div>`;
}

function renderExcelIngestion(file) {
  const page = document.getElementById('ingestion-page');
  if (!page) return;

  const rows = flow.mapping[file.id];
  const stats = flow.mapStats[file.id];
  const rowsAnalyzed = mockRowsAnalyzed(file).toLocaleString('en-IN');
  const acceptPct = 99;
  const r = 26, c = 2 * Math.PI * r;

  page.innerHTML = `
    <div class="ing-body">
      ${topbar()}

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
              <div><div class="ing-file-summary-meta-label">Rows analyzed</div><div class="ing-file-summary-meta-value">${rowsAnalyzed}</div></div>
              <div><div class="ing-file-summary-meta-label">Last uploaded</div><div class="ing-file-summary-meta-value">Just now</div></div>
            </div>
            <div class="ing-file-summary-badge"><span class="ing-status-chip">${I.checkCircle}File processed successfully</span></div>
            <div class="ing-file-summary-meta-label" style="margin-top:10px">Source contact (for missing-data emails)</div>
            <input id="ing-source-contact-input" type="email" placeholder="name@company.com"
                   value="${ingEsc((SOURCE_CONTACTS[file.id] || {}).email || '')}"
                   style="margin-top:4px;width:100%;padding:5px 8px;border:1px solid var(--border-light);border-radius:var(--r-sm);font-size:12px">
          </div>
        </div>
      </div>

      ${statsRowHtml(stats)}
      ${dataQualitySectionHtml()}

      <div class="ing-mapping-layout">
        <div class="ing-mapping-main">
          <div class="ing-mapping-main-head">
            <div>
              <div class="ing-card-title">Suggested field mapping</div>
              <div class="ing-card-sub">Most fields are ready. Only review rows with medium confidence or no match.</div>
            </div>
            <button type="button" class="ing-filters-btn">${I.filter}<span>Filters (1)</span></button>
          </div>
          <div id="ing-map-table-slot">${mappingTableHtml(rows, false)}</div>
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
                <circle class="fill" cx="32" cy="32" r="${r}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - acceptPct / 100)).toFixed(2)}"/>
              </svg>
              <div class="ing-accept-ring-pct">${acceptPct}%</div>
            </div>
            <div class="ing-accept-ring-label">of fields are typically accepted as-is</div>
          </div>
          <div class="ing-sidebar-note">You can edit mappings later in project settings.</div>
        </div>
      </div>

      <div class="ing-footer-row">
        <button type="button" class="ing-footer-link" id="ing-review-flagged-btn">Review flagged only</button>
        <button type="button" class="proj-btn-primary" id="ing-confirm-mapping-btn"><span>Confirm mapping &amp; continue</span>${I.arrowRight}</button>
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

  document.getElementById('ing-source-contact-input')?.addEventListener('change', e => {
    const email = e.target.value.trim();
    if (email) SOURCE_CONTACTS[file.id] = { ...(SOURCE_CONTACTS[file.id] || {}), email };
    else delete SOURCE_CONTACTS[file.id];
  });

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

  document.querySelector('#ingestion-page .ing-back-home-btn')?.addEventListener('click', goBackInFlow);
  document.getElementById('ing-confirm-mapping-btn')?.addEventListener('click', () => {
    file.status = 'mapped';
    advanceQueue();
  });
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
  const pages = mockPages(file);
  const highConfidence = CONTRACT_VENDOR.extractedTerms.filter((t) => t.confidence === 'HIGH').length;

  page.innerHTML = `
    <div class="ing-body">
      ${topbar()}

      <div class="ing-pdf-layout">
        <h1 class="ing-title" style="font-size:calc(26*var(--u))">AI is understanding your document</h1>
        <p class="ing-subtitle">Your file has been uploaded. NetGravity is extracting key information and identifying what matters.</p>

        <div class="ing-pdf-file-card">
          <span class="ing-file-icon type-pdf" style="width:calc(40*var(--u));height:calc(40*var(--u))">PDF</span>
          <div class="ing-pdf-file-meta">
            <div class="ing-pdf-file-name">${ingEsc(file.name)}</div>
            <div class="ing-pdf-file-sub">${pages} pages &middot; ${file.sizeMB.toFixed(1)} MB</div>
          </div>
          <span class="ing-status-chip">${I.checkCircle}Upload successful</span>
        </div>

        <div class="ing-findings-title">${I.docSearch}What I've found so far</div>
        <div id="ing-findings-slot" class="ing-findings-grid">${findingCardsHtml(file, review)}</div>
      </div>

      <div class="ing-footer-row">
        <div class="ing-pdf-actions" style="margin-top:0;flex:1;max-width:calc(560*var(--u));margin-left:auto">
          <div class="ing-pdf-actions-col">
            <button type="button" class="proj-btn-primary" id="ing-pdf-continue-btn"><span>Continue &amp; confirm</span>${I.arrowRight}</button>
            <div class="ing-pdf-action-caption">Proceed with ${highConfidence} high-confidence terms</div>
          </div>
          <div class="ing-pdf-actions-col">
            <button type="button" class="ing-btn-secondary" id="ing-pdf-review-btn" style="justify-content:center"><span>Review extracted terms</span>${I.arrowRight}</button>
            <div class="ing-pdf-action-caption">Review ${getReviewTerms(CONTRACT_VENDOR).length - review.reviewed.size} term${getReviewTerms(CONTRACT_VENDOR).length - review.reviewed.size === 1 ? '' : 's'} that may need changes</div>
          </div>
        </div>
      </div>
    </div>`;

  bindPdfIngestion(file);
}

function bindPdfIngestion(file) {
  const review = flow.pdfReview[file.id];

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
