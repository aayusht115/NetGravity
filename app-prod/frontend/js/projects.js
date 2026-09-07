/**
 * Netgravity — Project Workspace Controller
 * =========================================
 * Owns the two screens that sit between authentication and the app shell:
 *
 *   new user       landing → create account → CREATE PROJECT → app
 *   existing user  landing → sign in        → SELECT PROJECT → app
 *                                             └→ create new  → CREATE PROJECT
 *
 * Both screens render into empty placeholder divs in index.html, the same
 * way landing.js renders the map stage, so the bundled HTML stays thin.
 *
 * STATUS: PROTOTYPE / MOCKED — project records are in-memory only.
 */

import { projectService } from './integration/services/project-service.js';
import { mapProjectRecord } from './integration/mappers/project-mapper.js';
import { setActiveProject } from './integration/project-context.js';
import { hydrateFromBackend } from './integration/hydrate.js';
import { kpiService } from './integration/services/kpi-service.js';
import {
  beginAnalysisLoading, endAnalysisLoading, refineAnalysisLoading,
  reportAnalysisStage,
} from './analysis-loading.js';
import { clearNetworkModel } from './data.js';
import {
  bindWorkspaceTopbar, recordActivity, showInfoPanel, workspaceTopbarHtml,
} from './workspace-chrome.js';
import { firstNameOf, getCurrentUser } from './identity.js';

/* ─── Workspace data ───────────────────────────────────────────────
   Populated from the backend for the signed-in user. Phase 10.0 removed
   five hardcoded workspaces ("India Network 2024", "Cost Optimization
   Q2", …) that were listed for every visitor and all pointed at the same
   synthetic snapshot, so a user saw projects that were not theirs and did
   not exist. */
export const PROJECTS = [];

// Region / Scope options.
//
// Every entry was an Indian sub-region, so a user uploading a US, European or
// global network had literally no correct choice — they left the field blank,
// and the backend defaulted the project to India. Currency, maps, page
// subtitles and settings all followed that default.
//
// The list is now global, and the field is optional rather than silently
// defaulted: leaving it blank means the region is inferred from the
// coordinates in the uploaded data (see `ProjectRegistry.bind_network`), which
// is better evidence than a dropdown anyway.
const REGIONS = [
  'Global',
  'India', 'North India', 'South India', 'East India', 'West India',
  'Pan India', 'South Asia',
  'United States', 'Canada', 'North America', 'Latin America',
  'United Kingdom', 'Europe', 'Middle East', 'Africa',
  'China', 'Japan', 'Southeast Asia', 'Asia Pacific', 'Oceania',
];

// No suggestion list.
//
// This held seven named Indian companies, offered as this deployment's clients
// on every install. None of them is a client of anything; they were placeholder
// content presented as configuration, and the field is free text anyway. An
// empty datalist is the honest state until a deployment supplies real ones.
const CLIENTS = [];

/**
 * How to show a project's region.
 *
 * Three states, and they are different facts: the user stated it, we inferred
 * it from the coordinates in their upload, or nobody knows yet. Rendering all
 * three as a bare label — and defaulting the third to "India" — is what let a
 * US dataset be listed, and priced, and mapped, as an India network.
 */
function regionLabel(p) {
  if (!p.region) return '<span style="color:var(--proj-text-3,#9ca3af)">Not set</span>';
  const text = escapeHtml(p.region);
  return p.regionSource === 'inferred'
    ? `${text} <span style="color:var(--proj-text-3,#9ca3af);font-size:.85em" title="Inferred from the coordinates in the uploaded data">(from data)</span>`
    : text;
}

/**
 * The mark beside a region name.
 *
 * The mock shows a country flag. Regional-indicator emoji are the only way to
 * write one in text, and Windows ships no font that draws them — Chrome on
 * Windows renders 🇮🇳 as the two letters "IN", which reads as a rendering
 * fault rather than a flag. A pin is drawn identically everywhere and says the
 * same thing: this is where the network is.
 */
const REGION_PIN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="2.8"/></svg>`;

/**
 * What kind of workspace this is, from what the server says about it.
 *
 * There is no project `type` on the backend — `ProjectRecord` carries name,
 * region, client, description, status and the bound snapshot — so this reports
 * the workspace's actual stage rather than inventing a category. A project
 * with a network bound is a network the user has designed; one without is
 * still being set up; the bundled synthetic workspace is neither.
 */
function projectType(p) {
  if (p.isDemo) return { label: 'Sample network', tone: 'sample' };
  if (p.hasNetwork) return { label: 'Network design', tone: 'design' };
  return { label: 'Awaiting setup', tone: 'setup' };
}

function typeChip(p) {
  const t = projectType(p);
  return `<span class="proj-type-chip tone-${t.tone}">${escapeHtml(t.label)}</span>`;
}

/* ─── View state ─────────────────────────────────────────────── */
const ui = {
  /* 'first' when the user has just created an account (no projects yet),
     'existing' when they arrived from the select screen. Drives the
     create screen's wording and where Cancel goes back to. */
  createOrigin: 'first',
  search: '',
  sort: 'updated',
  view: 'list',
  /* Filters, applied on top of the search term. Every value is a real
     property of the record the server returned — there is no filter here
     for a field the backend does not have. */
  filters: { status: '', owner: '', region: '' },
  /* 'loading' | 'ready' | 'error' — what the list is currently doing, so the
     screen can say so rather than showing one empty table for all three. */
  listState: 'loading',
  listError: '',
};

function activeFilterCount() {
  return Object.values(ui.filters).filter(Boolean).length;
}

/* The project currently open in the app shell — set on create/open, read
   by the topbar's Upload Data button (see app.js) so a mid-session
   upload knows which project it belongs to. */
let currentProject = null;

/* ─── Icons ──────────────────────────────────────────────────── */
const ICONS = {
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
  globe: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/></svg>`,
  client: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21V6a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v15"/><path d="M13 10h6a1 1 0 0 1 1 1v10"/><line x1="8" y1="9" x2="8" y2="9.01"/><line x1="8" y1="13" x2="8" y2="13.01"/><line x1="8" y1="17" x2="8" y2="17.01"/><line x1="17" y1="14" x2="17" y2="14.01"/><line x1="17" y1="18" x2="17" y2="18.01"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  chevronLeft: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
  kebab: `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>`,
  search: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>`,
  grid: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.8"/></svg>`,
  list: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg>`,
  uploadCloud: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 16.5a4 4 0 0 0-1-7.87 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 6 17"/><polyline points="9 13 12 10 15 13"/><line x1="12" y1="10" x2="12" y2="19"/></svg>`,
  filter: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="21 4 3 4 10 12.2 10 18.5 14 20.5 14 12.2 21 4"/></svg>`,
  chevronDown: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`,
  folderSolid: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3.2 6.6A2 2 0 0 1 5.2 4.8h3.4a1 1 0 0 1 .8.4l1.2 1.6h8.2a2 2 0 0 1 2 2v8.8a2 2 0 0 1-2 2H5.2a2 2 0 0 1-2-2z"/></svg>`,
  upload: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 9 12 4 17 9"/><line x1="12" y1="4" x2="12" y2="16"/></svg>`,
  pencil: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>`,
  copy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
  open: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`,
};

/* Decorative flow-lines wash, bottom right of both screens. */
function decorSvg() {
  const curves = [0, 1, 2, 3, 4, 5, 6, 7].map(i => {
    const off = i * 26;
    return `<path d="M ${40 + off} 520 C ${240 + off} ${430 - i * 12}, ${430 + off} ${330 - i * 16}, ${760} ${250 - i * 22}"
             fill="none" stroke="#9218EA" stroke-opacity="${(0.16 - i * 0.012).toFixed(3)}" stroke-width="1"/>`;
  }).join('');
  const dots = [[120, 470], [232, 402], [356, 356], [470, 300], [598, 268], [688, 214], [300, 462], [520, 380]]
    .map(([x, y], i) => `<circle cx="${x}" cy="${y}" r="${i % 3 === 0 ? 3.4 : 2.2}" fill="#9218EA" fill-opacity="${i % 3 === 0 ? 0.22 : 0.14}"/>`)
    .join('');
  return `<svg class="proj-decor" viewBox="0 0 760 520" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">${curves}${dots}</svg>`;
}

/* The brand lockup these screens used to render for themselves now comes from
   the shared top bar — see workspaceTopbarHtml() in js/workspace-chrome.js.
   Two nearly-identical local copies, one of which linked to the landing page
   and one of which did not, were the reason the same logo signed you out on
   one screen and did nothing on the next. */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ═══════════════════════════════════════════════════════════════
   CREATE PROJECT
   ═══════════════════════════════════════════════════════════════ */
export function renderCreateProject() {
  const page = document.getElementById('create-project-page');
  if (!page) return;

  const first = ui.createOrigin === 'first';
  const cancelLabel = first ? 'Back to sign in' : 'Cancel';

  const regionOpts = REGIONS.map(r => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join('');
  const clientOpts = CLIENTS.map(c => `<option value="${escapeHtml(c)}"></option>`).join('');

  page.innerHTML = `
    ${workspaceTopbarHtml()}
    <div class="proj-scroll">
      ${decorSvg()}
      <div class="proj-create-body">
        <button type="button" class="proj-back-pill" id="proj-create-back">
          ${ICONS.chevronLeft}<span>Back</span>
        </button>

        <div class="proj-create-head">
          <h1 class="proj-create-title">Create Project</h1>
          <p class="proj-create-sub">Set up a logistics network workspace to analyze, simulate, and optimize decisions.</p>
        </div>

        <form class="proj-form-card" id="proj-create-form" novalidate>
          <div class="proj-form-row split">
            <span class="proj-row-icon">${ICONS.file}</span>
            <div class="proj-field-pair">
              <div>
                <label class="proj-field-label" for="proj-name">Project name</label>
                <input class="proj-input" id="proj-name" type="text" placeholder="Enter project name" autocomplete="off" />
              </div>
              <span class="proj-row-icon">${ICONS.globe}</span>
              <div>
                <label class="proj-field-label" for="proj-region">Region / Scope <span class="proj-field-optional">(optional)</span></label>
                <select class="proj-select placeholder" id="proj-region"
                        aria-describedby="proj-region-hint">
                  <option value="">Infer from my uploaded data</option>
                  ${regionOpts}
                </select>
                <!-- What this field decides is not guessable from its label,
                     and leaving it blank is the better answer more often than
                     not. Saying so here is cheaper than a user finding out
                     from a map of the wrong continent. -->
                <div class="proj-field-hint" id="proj-region-hint">
                  Sets the map and currency. Left blank, it is read from the
                  coordinates in your data.
                </div>
              </div>
            </div>
          </div>

          <div class="proj-form-row">
            <span class="proj-row-icon">${ICONS.client}</span>
            <div>
              <label class="proj-field-label" for="proj-client">Client <span class="proj-field-optional">(optional)</span></label>
              <input class="proj-input" id="proj-client" type="text" list="proj-client-list" placeholder="Who is this network for?" autocomplete="off" />
              <datalist id="proj-client-list">${clientOpts}</datalist>
            </div>
          </div>
        </form>

        <div class="proj-create-actions">
          <button type="button" class="proj-btn-primary proj-btn-lg" id="proj-create-submit">
            ${ICONS.uploadCloud}<span>Proceed to upload data</span>
          </button>
          <div class="proj-error" id="proj-create-error"></div>
          <button type="button" class="proj-link-btn" id="proj-create-cancel">${cancelLabel}</button>
        </div>
      </div>
    </div>`;

  bindWorkspaceTopbar(page, { help: 'create' });
  bindCreateProject();
}

function bindCreateProject() {
  const nameInput = document.getElementById('proj-name');
  const errorEl = document.getElementById('proj-create-error');

  // Keep the select's placeholder colour until a real option is chosen.
  ['proj-region'].forEach(id => {
    const sel = document.getElementById(id);
    sel?.addEventListener('change', () => sel.classList.toggle('placeholder', !sel.value));
  });

  nameInput?.addEventListener('input', () => {
    if (errorEl) errorEl.textContent = '';
    nameInput.removeAttribute('aria-invalid');
  });

  const submitBtn = document.getElementById('proj-create-submit');
  const submitLabel = submitBtn?.querySelector('span');
  let submitting = false;

  const submit = () => {
    // One project per press. Without this a double-click — or Enter held down
    // — created two workspaces, and the second was the one the user landed in
    // while the first sat in their list with no data and no explanation.
    if (submitting) return;

    const name = (nameInput?.value || '').trim();
    if (!name) {
      if (errorEl) {
        errorEl.textContent = 'Give the project a name — it is how you will '
          + 'find this network later.';
      }
      nameInput?.setAttribute('aria-invalid', 'true');
      nameInput?.focus();
      return;
    }

    submitting = true;
    if (submitBtn) submitBtn.disabled = true;
    if (submitLabel) submitLabel.textContent = 'Creating project…';

    const payload = {
      name,
      // Blank stays blank. It used to default to India here AND on the
      // server, so an unanswered question became a stated fact about the
      // client's business.
      region: document.getElementById('proj-region')?.value || '',
      client: (document.getElementById('proj-client')?.value || '').trim(),
    };

    // The project is created on the server so it is owned, isolated, and
    // persists for this session. It starts with no network bound; upload is
    // the next step and is what makes it analysable.
    projectService.createProject(payload).then((created) => {
      const project = mapProjectRecord(created);
      PROJECTS.unshift(project);
      PROJECTS.forEach((p, i) => { p.rank = i + 1; });
      currentProject = project;
      setActiveProject(project.id);
      recordActivity(`Project “${project.name}” created.`, 'success');

      // Data upload/AI ingestion is the next step, not the app itself —
      // see js/ingestion.js for Upload Data → mapping → network build.
      if (typeof window.showUploadData === 'function') window.showUploadData(project);
      else enterApp();
    }).catch((err) => {
      // Say what happened and what to do about it. The button comes back so
      // the attempt can be repeated — a failed create used to leave the screen
      // exactly as it was, with no indication anything had been tried.
      if (errorEl) {
        errorEl.textContent = `${err?.message || 'The project could not be created.'} `
          + 'Nothing was saved — check the name and try again.';
      }
      submitting = false;
      if (submitBtn) submitBtn.disabled = false;
      if (submitLabel) submitLabel.textContent = 'Proceed to upload data';
    });
  };

  submitBtn?.addEventListener('click', submit);
  // Enter anywhere in the form submits it, as it would in any form.
  document.getElementById('proj-create-form')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
      submit();
    }
  });

  // Both routes out of this screen go to the same place — the pill at the top
  // and the link under the button — so the user does not have to work out
  // which of two "back" controls is the real one.
  const leave = () => {
    if (ui.createOrigin === 'first') {
      hideProjectPages();
      if (typeof window.returnToLanding === 'function') window.returnToLanding();
    } else {
      showSelectProject();
    }
  };
  document.getElementById('proj-create-cancel')?.addEventListener('click', leave);
  document.getElementById('proj-create-back')?.addEventListener('click', leave);
}

/* ═══════════════════════════════════════════════════════════════
   SELECT PROJECT
   ═══════════════════════════════════════════════════════════════ */
function matchesFilters(p) {
  const f = ui.filters;
  if (f.status && p.status !== f.status) return false;
  if (f.owner && p.owner !== f.owner) return false;
  if (f.region && (p.region || '(none)') !== f.region) return false;
  return true;
}

function visibleProjects() {
  const q = ui.search.trim().toLowerCase();
  const list = PROJECTS.filter(p => matchesFilters(p)).filter(p =>
    !q || p.name.toLowerCase().includes(q) || (p.region || '').toLowerCase().includes(q)
    || (p.client || '').toLowerCase().includes(q));

  const sorters = {
    updated: (a, b) => a.rank - b.rank,
    name: (a, b) => a.name.localeCompare(b.name),
    status: (a, b) => a.status.localeCompare(b.status) || a.rank - b.rank,
  };
  return list.slice().sort(sorters[ui.sort] || sorters.updated);
}

/**
 * The server's own status, coloured by what it means.
 *
 * "Analysis ready" is the state the mock calls "Ready to view"; a workspace
 * with nothing ingested says so instead of borrowing the ready wording.
 */
function statusChip(status) {
  const cls = status === 'Analysis ready' ? 'proj-chip-ready'
    : status === 'Awaiting data' ? 'proj-chip-waiting'
      : 'proj-chip-draft';
  return `<span class="proj-chip ${cls}">${escapeHtml(status)}</span>`;
}

/** Region with its flag, or the three-state label when none is stated. */
function regionCell(p) {
  if (!p.region) return regionLabel(p);
  return `<span class="proj-region"><span class="proj-flag">${REGION_PIN}</span>${regionLabel(p)}</span>`;
}

function rowActions(p) {
  return `<div class="proj-row-actions">
      <button class="proj-open-outline" type="button" data-open="${p.id}">Open</button>
      <button class="proj-kebab" type="button" data-menu="${p.id}"
              aria-haspopup="menu" aria-expanded="false"
              aria-label="More actions for ${escapeHtml(p.name)}">${ICONS.kebab}</button>
    </div>`;
}

function recentCard(p) {
  return `<div class="proj-recent-card">
      <div class="proj-recent-top">
        <span class="proj-folder-tile">${ICONS.folder}</span>
        <div class="proj-recent-meta">
          <div class="proj-recent-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
          <div class="proj-recent-region">${regionCell(p)}</div>
        </div>
      </div>
      <div class="proj-recent-foot">
        ${statusChip(p.status)}
        ${rowActions(p)}
      </div>
    </div>`;
}

function listBody(rows) {
  // Still fetching: skeleton rows, so the screen reads as "loading" rather
  // than "you have no projects".
  if (ui.listState === 'loading' && !rows.length) {
    return `<div class="proj-table-wrap" aria-busy="true">
        <div class="proj-skeleton" role="status">
          <span class="sr-only">Loading your projects…</span>
          <div class="proj-skeleton-row"></div>
          <div class="proj-skeleton-row"></div>
          <div class="proj-skeleton-row"></div>
        </div>
      </div>`;
  }

  // The fetch failed. Say what failed and offer the one action that helps,
  // rather than an empty table that reads as an answer.
  if (ui.listState === 'error' && !rows.length) {
    return `<div class="proj-table-wrap"><div class="proj-empty">
        <strong>Your projects could not be loaded</strong>
        ${escapeHtml(ui.listError)}.<br>
        Your work is on the server, not in this page — nothing has been lost.
        <div><button type="button" class="proj-btn-primary" data-empty="retry">Try again</button></div>
      </div></div>`;
  }

  if (!rows.length) {
    const narrowed = ui.search.trim() || activeFilterCount();
    const body = ui.search.trim()
      ? `<strong>No projects match “${escapeHtml(ui.search)}”</strong>
         Try a shorter search term, or clear it to see everything.`
      : activeFilterCount()
        ? `<strong>No projects match these filters</strong>
           ${activeFilterCount()} filter${activeFilterCount() === 1 ? ' is' : 's are'} applied.`
        : `<strong>No projects yet</strong>
           A project is one logistics network — its data, its baseline and every
           scenario you run against it.`;
    const action = narrowed
      ? '<div><button type="button" class="proj-btn-primary" data-empty="clear">Clear search and filters</button></div>'
      : '<div><button type="button" class="proj-btn-primary" data-empty="create">Create your first project</button></div>';
    return `<div class="proj-table-wrap"><div class="proj-empty">${body}${action}</div></div>`;
  }

  if (ui.view === 'grid') {
    return `<div class="proj-card-grid">${rows.map(p => `
      <div class="proj-grid-card">
        <div class="proj-recent-top">
          <span class="proj-folder-tile">${ICONS.folder}</span>
          <div class="proj-recent-meta">
            <div class="proj-recent-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
            <div class="proj-recent-region">${regionCell(p)}</div>
          </div>
        </div>
        <div class="proj-grid-chips">${typeChip(p)}${statusChip(p.status)}</div>
        <div class="proj-recent-foot">
          <span class="proj-grid-when">${escapeHtml(p.updated)}</span>
          ${rowActions(p)}
        </div>
      </div>`).join('')}</div>`;
  }

  return `<div class="proj-table-wrap">
      <table class="proj-table">
        <thead>
          <tr>
            <th>Project name</th><th>Type</th><th>Region / Scope</th>
            <th>Status</th><th class="proj-th-actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(p => `
            <tr>
              <td><span class="proj-row-name">
                <span class="proj-row-folder">${ICONS.folderSolid}</span>${escapeHtml(p.name)}
              </span></td>
              <td>${typeChip(p)}</td>
              <td>${regionCell(p)}</td>
              <td>${statusChip(p.status)}</td>
              <td class="proj-td-actions">${rowActions(p)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

export function renderSelectProject() {
  const page = document.getElementById('select-project-page');
  if (!page) return;

  const recents = PROJECTS.slice().sort((a, b) => a.rank - b.rank).slice(0, 2);
  const filterCount = activeFilterCount();
  const sortLabel = { updated: 'Last updated', name: 'Name', status: 'Status' }[ui.sort]
    || 'Last updated';

  page.innerHTML = `
    ${workspaceTopbarHtml({ variant: 'wide' })}
    <div class="proj-scroll">
      <div class="proj-select-body">
        ${selectCameFromApp ? `
        <button type="button" class="proj-back-pill" id="proj-select-back">
          ${ICONS.chevronLeft}<span>Back to ${escapeHtml(currentProject ? currentProject.name : 'the workspace')}</span>
        </button>` : ''}
        <div class="proj-select-head">
          <div>
            <h1 class="proj-select-title">Welcome back, ${escapeHtml(firstNameOf(getCurrentUser()))}! <span class="proj-wave">👋</span></h1>
            <p class="proj-select-sub">Plan, analyze and optimise your logistics networks with AI-powered scenarios.</p>
          </div>
          <button class="proj-btn-primary" type="button" id="proj-new-btn">${ICONS.plus}<span>Create new project</span></button>
        </div>

        ${recents.length ? `
        <div class="proj-section-row">
          <div class="proj-section-title">Recent projects</div>
          <button class="proj-viewall" type="button" id="proj-view-all">View all</button>
        </div>
        <div class="proj-recent-grid">${recents.map(recentCard).join('')}</div>` : ''}

        <div class="proj-section-row" id="proj-all-anchor">
          <div class="proj-section-title">All projects</div>
        </div>
        <div class="proj-toolbar">
          <div class="proj-search-wrap">
            <span class="proj-search-icon">${ICONS.search}</span>
            <input class="proj-search" id="proj-search" type="search" placeholder="Search projects by name" value="${escapeHtml(ui.search)}" autocomplete="off" />
          </div>
          <div class="proj-toolbar-right">
            <button class="proj-tool-btn${filterCount ? ' active' : ''}" type="button" id="proj-filter-btn"
                    aria-haspopup="dialog" aria-expanded="false">
              ${ICONS.filter}<span>Filters</span>${filterCount ? `<span class="proj-tool-count">${filterCount}</span>` : ''}
            </button>
            <button class="proj-tool-btn" type="button" id="proj-sort-btn"
                    aria-haspopup="menu" aria-expanded="false">
              <span class="proj-tool-muted">Sort:</span><span>${escapeHtml(sortLabel)}</span>${ICONS.chevronDown}
            </button>
            <div class="proj-view-toggle">
              <button class="proj-view-btn${ui.view === 'list' ? ' active' : ''}" type="button" data-view="list" title="List view" aria-label="List view">${ICONS.list}</button>
              <button class="proj-view-btn${ui.view === 'grid' ? ' active' : ''}" type="button" data-view="grid" title="Grid view" aria-label="Grid view">${ICONS.grid}</button>
            </div>
          </div>
        </div>

        <div id="proj-list-slot">${listBody(visibleProjects())}</div>
      </div>
    </div>`;

  bindWorkspaceTopbar(page, { help: 'projects' });
  bindSelectProject();
}

function refreshList() {
  const slot = document.getElementById('proj-list-slot');
  if (slot) slot.innerHTML = listBody(visibleProjects());
}

/* ─── Floating popovers (filters, sort, row menu) ─────────────── */
function closeProjPopovers() {
  document.querySelectorAll('.proj-pop').forEach(el => el.remove());
  document.querySelectorAll('#select-project-page [aria-expanded="true"]')
    .forEach(b => b.setAttribute('aria-expanded', 'false'));
}

function mountPop(anchor, className, html, { align = 'right' } = {}) {
  closeProjPopovers();
  const pop = document.createElement('div');
  pop.className = `proj-pop ${className}`;
  pop.innerHTML = html;
  document.body.appendChild(pop);

  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  const rawLeft = align === 'right' ? r.right - w : r.left;
  pop.style.left = `${Math.round(Math.max(12, Math.min(rawLeft, window.innerWidth - w - 12)))}px`;
  // Flip above the anchor when there is not room below, so a menu on the last
  // row of a long list is not opened off the bottom of the screen.
  const belowRoom = window.innerHeight - r.bottom;
  const h = pop.offsetHeight;
  pop.style.top = belowRoom < h + 16 && r.top > h + 16
    ? `${Math.round(r.top - h - 8)}px`
    : `${Math.round(r.bottom + 8)}px`;
  anchor.setAttribute('aria-expanded', 'true');
  return pop;
}

/** Distinct values actually present, so no filter offers an empty result. */
function filterOptions() {
  const uniq = (vals) => Array.from(new Set(vals.filter(Boolean))).sort();
  return {
    status: uniq(PROJECTS.map(p => p.status)),
    owner: uniq(PROJECTS.map(p => p.owner)),
    region: uniq(PROJECTS.map(p => p.region)),
    hasUnset: PROJECTS.some(p => !p.region),
  };
}

function openFilterPop(anchor) {
  const o = filterOptions();
  const group = (key, label, values, extra = '') => `
    <div class="proj-pop-group">
      <div class="proj-pop-label">${label}</div>
      <div class="proj-pop-chips">
        <button type="button" class="proj-pop-chip${ui.filters[key] ? '' : ' on'}" data-filter="${key}" data-value="">All</button>
        ${values.map(v => `<button type="button" class="proj-pop-chip${ui.filters[key] === v ? ' on' : ''}" data-filter="${key}" data-value="${escapeHtml(v)}">${escapeHtml(v)}</button>`).join('')}
        ${extra}
      </div>
    </div>`;

  const pop = mountPop(anchor, 'proj-pop-filter', `
    <div class="proj-pop-head">Filters</div>
    ${group('status', 'Status', o.status)}
    ${group('owner', 'Owner', o.owner)}
    ${group('region', 'Region / Scope', o.region,
    o.hasUnset ? `<button type="button" class="proj-pop-chip${ui.filters.region === '(none)' ? ' on' : ''}" data-filter="region" data-value="(none)">Not set</button>` : '')}
    <div class="proj-pop-foot">
      <button type="button" class="proj-pop-clear" id="proj-filter-clear">Clear all</button>
    </div>`);

  pop.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-filter]');
    if (chip) {
      ui.filters[chip.dataset.filter] = chip.dataset.value;
      closeProjPopovers();
      renderSelectProject();
      return;
    }
    if (e.target.closest('#proj-filter-clear')) {
      ui.filters = { status: '', owner: '', region: '' };
      closeProjPopovers();
      renderSelectProject();
    }
  });
}

function openSortPop(anchor) {
  const opts = [
    ['updated', 'Last updated'],
    ['name', 'Name'],
    ['status', 'Status'],
  ];
  const pop = mountPop(anchor, 'proj-pop-menu', opts.map(([v, label]) => `
    <button type="button" class="proj-pop-item${ui.sort === v ? ' on' : ''}" data-sort="${v}">
      <span>${label}</span>${ui.sort === v ? '<span class="proj-pop-tick">✓</span>' : ''}
    </button>`).join(''));

  pop.addEventListener('click', (e) => {
    const item = e.target.closest('[data-sort]');
    if (!item) return;
    ui.sort = item.dataset.sort;
    closeProjPopovers();
    renderSelectProject();
  });
}

/**
 * The per-project menu.
 *
 * Every entry does something this build can actually do. There is no Delete
 * because there is no delete endpoint — `/api/projects/<id>` accepts GET, PUT
 * and PATCH only — and a Delete that silently fails is worse than none.
 */
function openRowMenu(anchor, id) {
  const p = PROJECTS.find(x => x.id === id);
  if (!p) return;
  const readOnly = p.owner === 'Sample';

  const pop = mountPop(anchor, 'proj-pop-menu', `
    <button type="button" class="proj-pop-item" data-act="open">${ICONS.open}<span>Open project</span></button>
    <button type="button" class="proj-pop-item" data-act="upload">${ICONS.upload}<span>Upload data</span></button>
    <button type="button" class="proj-pop-item" data-act="rename"${readOnly ? ' disabled title="The bundled sample workspace cannot be renamed."' : ''}>${ICONS.pencil}<span>Rename…</span></button>
    <div class="proj-pop-sep"></div>
    <button type="button" class="proj-pop-item" data-act="copy">${ICONS.copy}<span>Copy project ID</span></button>`);

  pop.addEventListener('click', (e) => {
    const item = e.target.closest('[data-act]');
    if (!item || item.disabled) return;
    const act = item.dataset.act;
    closeProjPopovers();
    if (act === 'open') { openProject(id); return; }
    if (act === 'upload') {
      currentProject = p;
      setActiveProject(p.id);
      if (typeof window.showUploadData === 'function') window.showUploadData(p);
      return;
    }
    if (act === 'rename') { openRenameDialog(p); return; }
    if (act === 'copy') {
      const done = () => recordActivity(`Project ID copied: ${p.id}`);
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(p.id).then(done).catch(() => {
          showInfoPanel('Project ID', `<p><code>${escapeHtml(p.id)}</code></p>`);
        });
      } else {
        showInfoPanel('Project ID', `<p><code>${escapeHtml(p.id)}</code></p>`);
      }
    }
  });
}

/** Rename through `PUT /api/projects/<id>`, the endpoint that already exists. */
function openRenameDialog(p) {
  const close = showInfoPanel('Rename project', `
    <label class="proj-field-label" for="proj-rename-input">Project name</label>
    <input class="proj-input" id="proj-rename-input" type="text"
           value="${escapeHtml(p.name)}" autocomplete="off" />
    <div class="proj-error" id="proj-rename-error"></div>
    <div class="proj-modal-actions">
      <button type="button" class="proj-link-btn" id="proj-rename-cancel">Cancel</button>
      <button type="button" class="proj-btn-primary" id="proj-rename-save">Save name</button>
    </div>`);

  const input = document.getElementById('proj-rename-input');
  const err = document.getElementById('proj-rename-error');
  input?.focus();
  input?.select();

  const save = () => {
    const name = (input?.value || '').trim();
    if (!name) { if (err) err.textContent = 'A project needs a name.'; return; }
    if (name === p.name) { close(); return; }
    projectService.updateProject(p.id, { name })
      .then((updated) => {
        const mapped = mapProjectRecord(updated);
        const idx = PROJECTS.findIndex(x => x.id === p.id);
        if (idx >= 0) PROJECTS[idx] = { ...mapped, rank: PROJECTS[idx].rank };
        if (currentProject && currentProject.id === p.id) currentProject = PROJECTS[idx];
        const nameEl = document.getElementById('topbar-current-project-name');
        if (nameEl && currentProject && currentProject.id === p.id) {
          nameEl.textContent = mapped.name;
        }
        recordActivity(`Project renamed to “${mapped.name}”.`, 'success');
        close();
        renderSelectProject();
      })
      .catch((e) => {
        if (err) err.textContent = e?.message || 'The project could not be renamed.';
      });
  };

  document.getElementById('proj-rename-save')?.addEventListener('click', save);
  document.getElementById('proj-rename-cancel')?.addEventListener('click', close);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
  });
}

function bindSelectProject() {
  const page = document.getElementById('select-project-page');
  if (!page) return;

  document.getElementById('proj-new-btn')?.addEventListener('click', () => showCreateProject('existing'));

  // Only rendered when this screen was opened from inside the app; a user who
  // has just signed in has nothing to go back to but the landing page, and the
  // account menu is where signing out belongs.
  document.getElementById('proj-select-back')?.addEventListener('click', backFromSelectProject);

  // "View all" is a jump to the full list, and it drops the filters and search
  // that are hiding rows from it — a link labelled "view all" that scrolls to a
  // filtered list would show fewer projects than it promises.
  document.getElementById('proj-view-all')?.addEventListener('click', () => {
    const hadNarrowing = ui.search.trim() || activeFilterCount();
    ui.search = '';
    ui.filters = { status: '', owner: '', region: '' };
    if (hadNarrowing) renderSelectProject();
    document.getElementById('proj-all-anchor')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  const filterBtn = document.getElementById('proj-filter-btn');
  filterBtn?.addEventListener('click', () => {
    const open = document.querySelector('.proj-pop-filter');
    closeProjPopovers();
    if (!open) openFilterPop(filterBtn);
  });

  const sortBtn = document.getElementById('proj-sort-btn');
  sortBtn?.addEventListener('click', () => {
    const open = document.querySelector('.proj-pop-menu');
    closeProjPopovers();
    if (!open) openSortPop(sortBtn);
  });

  // One dismisser, replaced on every render rather than stacked.
  if (window.__projDismiss) document.removeEventListener('click', window.__projDismiss);
  window.__projDismiss = (e) => {
    if (e.target.closest('.proj-pop')) return;
    if (e.target.closest('#proj-filter-btn, #proj-sort-btn, .proj-kebab')) return;
    closeProjPopovers();
  };
  document.addEventListener('click', window.__projDismiss);

  const search = document.getElementById('proj-search');
  // `input` alone is not enough on `<input type="search">`.
  //
  // The native clear (×) fires `search` in WebKit and, depending on the path,
  // may not fire `input` at all — so the box emptied on screen while
  // `ui.search` kept the old term and the list stayed filtered. A user saw an
  // empty search box next to a list missing most of their projects, which
  // reads as projects having disappeared.
  //
  // Escape clears too, because a filter you cannot see is a filter you cannot
  // undo.
  const applySearch = () => {
    if (ui.search === search.value) return;
    ui.search = search.value;
    refreshList();
  };
  ['input', 'search', 'change'].forEach((evt) =>
    search?.addEventListener(evt, applySearch));
  search?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { search.value = ''; applySearch(); }
  });

  page.querySelectorAll('.proj-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      ui.view = btn.dataset.view;
      page.querySelectorAll('.proj-view-btn').forEach(b => b.classList.toggle('active', b === btn));
      refreshList();
    });
  });

  // One delegated handler covers recent cards, table rows, grid cards and the
  // per-row menu — attached to the page element ONCE, not once per render.
  //
  // `renderSelectProject` replaces this element's innerHTML but not the element
  // itself, so re-binding here stacked a second identical listener on every
  // re-render. Two listeners meant a kebab click opened the menu and then
  // immediately closed it, and a rename — which re-renders — made every row
  // menu on the screen stop working.
  if (page.dataset.delegated !== '1') {
    page.dataset.delegated = '1';
    page.addEventListener('click', e => {
      // The empty and error states offer the one action that helps. They are
      // delegated because `refreshList()` re-renders the list slot on every
      // keystroke in the search box WITHOUT re-running this binder — a handler
      // attached to the button by id survived the first render and no other,
      // so "Clear search and filters" silently stopped working the moment the
      // search that produced it was typed.
      const empty = e.target.closest('[data-empty]');
      if (empty) {
        const act = empty.dataset.empty;
        if (act === 'create') { showCreateProject('existing'); return; }
        if (act === 'clear') {
          ui.search = '';
          ui.filters = { status: '', owner: '', region: '' };
          renderSelectProject();
          return;
        }
        if (act === 'retry') { showSelectProject(); return; }
      }

      const menuBtn = e.target.closest('[data-menu]');
      if (menuBtn) {
        const open = document.querySelector('.proj-pop-menu');
        closeProjPopovers();
        if (!open) openRowMenu(menuBtn, menuBtn.dataset.menu);
        return;
      }
      if (e.target.closest('[data-noopen]')) return;
      const opener = e.target.closest('[data-open]');
      if (opener) openProject(opener.dataset.open);
    });
    page.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeProjPopovers();
    });
  }
}

/** Called by ingestion.js once a newly created project's data has been
 *  mapped and the network build finishes, so it stops reading "Draft". */
export function markProjectInProgress(id) {
  const p = PROJECTS.find(x => x.id === id);
  if (p) p.status = 'In progress';
}

/**
 * Fetch this user's projects into `PROJECTS`.
 *
 * Split out of `showSelectProject` so a session restored on page load can find
 * out which projects exist without rendering the picker first.
 */
export async function loadProjects() {
  const remote = await projectService.listProjects();
  (remote || []).forEach((rp) => {
    const mapped = mapProjectRecord(rp);
    const idx = PROJECTS.findIndex(p => p.id === mapped.id);
    if (idx >= 0) PROJECTS[idx] = mapped;
    else PROJECTS.push(mapped);
  });
  PROJECTS.forEach((p, i) => { if (!p.rank) p.rank = i + 1; });
  return PROJECTS;
}

/**
 * Re-open a project by id, as if the user had clicked it.
 *
 * Returns false when the id names nothing this user can open — a project
 * deleted, or belonging to someone else — so the caller can fall back to the
 * picker rather than entering a shell with no network behind it.
 */
export function openProjectById(id) {
  if (!id || !PROJECTS.some(p => p.id === id)) return false;
  openProject(id);
  return true;
}

function openProject(id) {
  const p = PROJECTS.find(x => x.id === id);
  if (p) {
    // Opening promotes the project to the top of "recent".
    PROJECTS.forEach(x => { if (x.rank < p.rank) x.rank += 1; });
    p.rank = 1;
    p.updated = 'Just now';
    currentProject = p;
  }
  // Scope every subsequent request to this project BEFORE the shell renders,
  // so one project's figures can never appear under another's name.
  setActiveProject(id);
  enterApp();
}

/* ═══════════════════════════════════════════════════════════════
   Navigation
   ═══════════════════════════════════════════════════════════════ */
export function hideProjectPages() {
  ['select-project-page', 'create-project-page'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  });
}

function hideLanding() {
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }
  const fab = document.getElementById('floating-chatbot-fab');
  if (fab) fab.style.display = 'none';
  const shell = document.querySelector('.app-shell');
  if (shell) shell.style.display = 'none';
}

/* True when Select Project was opened mid-session (the "Current Project"
   pill on Home), so Back should restore the app shell rather than sign out. */
let selectCameFromApp = false;

export function showSelectProject() {
  const shell = document.querySelector('.app-shell');
  selectCameFromApp = !!(shell && shell.style.display === 'flex');

  hideLanding();
  hideProjectPages();
  if (typeof window.hideIngestionPages === 'function') window.hideIngestionPages();

  // The screen says which of three things is true, rather than showing the
  // same empty table for all of them: still fetching, fetched and empty, or
  // the fetch failed. It used to render an empty list immediately and refresh
  // only on a NON-EMPTY response — so a user with no projects, and a user
  // whose request had failed, both saw the identical "no projects" table with
  // no way to tell that anything had gone wrong or was still happening.
  ui.listState = PROJECTS.length ? 'ready' : 'loading';
  ui.listError = '';

  projectService.listProjects().then(remoteProjects => {
    (remoteProjects || []).forEach(rp => {
      const mapped = mapProjectRecord(rp);
      const idx = PROJECTS.findIndex(p => p.id === mapped.id);
      if (idx >= 0) PROJECTS[idx] = { ...mapped, rank: PROJECTS[idx].rank };
      else PROJECTS.push(mapped);
    });
    PROJECTS.forEach((p, i) => { if (!p.rank) p.rank = i + 1; });
    ui.listState = 'ready';
    renderSelectProject();
  }).catch(e => {
    console.warn('Project listing sync note:', e);
    ui.listState = 'error';
    ui.listError = e?.message || 'the workspace list could not be reached';
    renderSelectProject();
  });

  renderSelectProject();
  const page = document.getElementById('select-project-page');
  if (page) {
    page.classList.remove('hidden');
    page.scrollTop = 0;
  }
}

function backFromSelectProject() {
  if (selectCameFromApp) {
    enterAppAsIs();
    return;
  }
  if (typeof window.returnToLanding === 'function') window.returnToLanding();
}

/* Restore the app shell exactly as it was, without forcing a Home
   redirect — used when Back should return to whatever tab was active. */
function enterAppAsIs() {
  hideProjectPages();
  if (typeof window.hideIngestionPages === 'function') window.hideIngestionPages();
  // Both, as everywhere else that puts the landing page away. `.hidden` is
  // what the rest of the application reads to tell whether the landing page
  // is up — including the rule in landing.css that pins the document to the
  // viewport while it is, which would otherwise keep the app shell from
  // scrolling on this path.
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }
  const shell = document.querySelector('.app-shell');
  if (shell) shell.style.display = 'flex';
  const fab = document.getElementById('floating-chatbot-fab');
  if (fab) fab.style.display = 'flex';
}

export function showCreateProject(origin) {
  ui.createOrigin = origin === 'existing' ? 'existing' : 'first';
  hideLanding();
  hideProjectPages();
  if (typeof window.hideIngestionPages === 'function') window.hideIngestionPages();
  renderCreateProject();
  const page = document.getElementById('create-project-page');
  if (page) {
    page.classList.remove('hidden');
    page.scrollTop = 0;
  }
}

/**
 * Leave the project screens and hand off to the authenticated app shell.
 *
 * `hydrate: false` reveals the shell without pulling figures — used by the
 * ingestion flow, which shows the parsed topology first and then commits and
 * hydrates behind its own loading screen. Without the option, that flow ran
 * two hydrations of the same project and the first failed with
 * NO_NETWORK_BOUND because the commit had not happened yet.
 */
export function enterApp({ hydrate = true } = {}) {
  hideProjectPages();
  if (typeof window.hideIngestionPages === 'function') window.hideIngestionPages();

  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }
  const shell = document.querySelector('.app-shell');
  if (shell) shell.style.display = 'flex';
  const fab = document.getElementById('floating-chatbot-fab');
  if (fab) fab.style.display = 'flex';

  const nameEl = document.getElementById('topbar-current-project-name');
  if (nameEl && currentProject) nameEl.textContent = currentProject.name;

  if (typeof window.navigateToTab === 'function') window.navigateToTab('home');

  setTimeout(() => {
    if (typeof window.renderHome === 'function') window.renderHome();
    window.dispatchEvent(new Event('resize'));
  }, 60);

  // Drop whatever the previously open project left behind BEFORE asking for
  // this one's figures. Hydration fills the model; nothing else empties it, so
  // without this the screens keep the last project's network until — and only
  // if — the new one's hydration succeeds.
  clearNetworkModel();

  // Pull authoritative figures for this project and write them into the app's
  // own data structures, so every existing screen renders solved values.
  //
  // The loading screen stays up until this settles. It used to be absent
  // entirely here: the dashboard appeared immediately and the figures arrived
  // twenty to forty seconds later, so the first thing a user saw after opening
  // a project was a complete screen of dashes that looked like an answer.
  if (currentProject && hydrate) {
    const projectId = currentProject.id;
    const projectName = currentProject.name;

    // The overlay goes up FIRST, before anything is asked of the server.
    //
    // It used to be raised inside `getReadiness().then()`, which meant the
    // dashboard was visible with no figures behind it for the duration of that
    // HTTP round trip. Short, and exactly what the loading screen exists to
    // prevent — check L-02 in
    // `validation/phase_10_7/run_greenfield_and_hardcoded_check.py` samples
    // every 80 ms and caught it.
    //
    // Readiness only ever affected the WORDING ("this has been solved before,
    // so it should be quick"), so it is applied when it arrives rather than
    // waited for. A failure there still only affects the wording.
    beginAnalysisLoading(projectName, false);
    kpiService.getReadiness(projectId)
      .catch(() => null)
      .then((readiness) => {
        refineAnalysisLoading(Boolean(readiness && readiness.ready));
        return hydrateFromBackend(projectId, reportAnalysisStage);
      })
      .then((report) => {
        endAnalysisLoading();
        if (report?.ok) {
          showProjectNotice(
            `${report.kpisValid} of ${report.kpisTotal} KPIs computed from `
            + `snapshot ${report.snapshotId} · ${report.facilities} facilities.`,
            'success',
          );
        }
      })
      .catch((err) => {
        // A project with no bound network is the ordinary state for a new
        // workspace, and it is said plainly rather than filled with figures
        // that describe a different network.
        const noNetwork = err?.code === 'NO_NETWORK_BOUND';
        endAnalysisLoading(noNetwork
          ? 'This project has no network yet.'
          : `Analysis unavailable: ${err?.message || 'unknown error'}`);
        // The model stays empty and every screen renders its own empty state.
        // This branch used to show a banner reading "no network yet" ON TOP OF
        // a fully populated dashboard, which is worse than either alone: the
        // numbers look authoritative and the banner is easy to miss.
        clearNetworkModel();
        if (typeof window.renderHome === 'function') window.renderHome();
        if (typeof window.renderTwinTables === 'function') window.renderTwinTables();
        showProjectNotice(
          noNetwork
            ? 'This project has no network yet. Upload your data to run the analysis.'
            : `Analysis unavailable: ${err?.message || 'unknown error'}`,
          noNetwork ? 'info' : 'error',
        );
      });
  }
}

/** A banner on Home stating the true analysis state of the open project. */
function showProjectNotice(message, tone = 'info') {
  const host = document.getElementById('tab-home');
  if (!host) return;
  let el = document.getElementById('ng-network-notice');
  if (!el) {
    el = document.createElement('div');
    el.id = 'ng-network-notice';
    host.prepend(el);
  }
  const colour = tone === 'error' ? 'var(--red)'
    : tone === 'success' ? 'var(--green)' : 'var(--blue)';
  const bg = tone === 'error' ? 'var(--red-bg)'
    : tone === 'success' ? 'var(--green-bg)' : 'var(--blue-bg)';
  el.setAttribute('role', 'status');
  el.style.cssText = `margin:0 0 var(--space-md);padding:10px 14px;`
    + `border-radius:var(--r-md);background:${bg};color:${colour};`
    + `border:1px solid ${colour}33;font-size:12.5px;font-weight:600`;
  el.textContent = message;
}

export function initProjects() {
  if (typeof window !== 'undefined') {
    window.showSelectProject = showSelectProject;
    window.showCreateProject = showCreateProject;
    window.hideProjectPages = hideProjectPages;
    window.enterApp = enterApp;
    window.markProjectInProgress = markProjectInProgress;
    window.getCurrentProject = () => currentProject;
    window.getCreateOrigin = () => ui.createOrigin;
  }
}
