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

/* ─── Mock workspace data (mirrors Dump/Select Project.jpeg) ─── */
export const PROJECTS = [
  { id: 'pr-india-2024',   name: 'India Network 2024',    region: 'India',                updated: '2 hours ago', rank: 1, owner: 'You', status: 'In progress' },
  { id: 'pr-north-revamp', name: 'North Region Revamp',   region: 'North India',          updated: '3 days ago',  rank: 2, owner: 'You', status: 'In progress' },
  { id: 'pr-cost-q2',      name: 'Cost Optimization Q2',  region: 'Pan India',            updated: '1 week ago',  rank: 3, owner: 'You', status: 'In progress' },
  { id: 'pr-dc-consol',    name: 'DC Consolidation Study', region: 'West India',          updated: '2 weeks ago', rank: 4, owner: 'You', status: 'Draft' },
  { id: 'pr-demand-surge', name: 'Demand Surge Planning', region: 'Central & South India', updated: '3 weeks ago', rank: 5, owner: 'You', status: 'Draft' },
];

const REGIONS = [
  'India', 'North India', 'South India', 'East India', 'West India',
  'Central & South India', 'Pan India', 'South Asia',
];

const OBJECTIVES = [
  'Cost optimization',
  'Service level improvement',
  'Network resilience',
  'Capacity planning',
  'Carbon footprint reduction',
];

const CREATE_MODES = [
  { id: 'scratch',  label: 'Start from scratch' },
  { id: 'import',   label: 'Import network data' },
  { id: 'template', label: 'Use starter template' },
];

/* ─── View state ─────────────────────────────────────────────── */
const ui = {
  /* 'first' when the user has just created an account (no projects yet),
     'existing' when they arrived from the select screen. Drives the
     create screen's wording and where Cancel goes back to. */
  createOrigin: 'first',
  mode: 'scratch',
  search: '',
  sort: 'updated',
  view: 'list',
};

/* The project currently open in the app shell — set on create/open, read
   by the topbar's Upload Data button (see app.js) so a mid-session
   upload knows which project it belongs to. */
let currentProject = null;

/* ─── Icons ──────────────────────────────────────────────────── */
const ICONS = {
  logo: `<svg class="proj-logo-svg" viewBox="0 0 48 48" fill="none">
      <line x1="10" y1="10" x2="38" y2="10" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="10" y1="38" x2="38" y2="38" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="12" y1="12" x2="36" y2="36" stroke="#9218EA" stroke-width="4" stroke-linecap="round"/>
      <circle cx="10" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="10" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
    </svg>`,
  scratch: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>`,
  import: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 16.5a4 4 0 0 0-1-7.87 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 6 17"/><polyline points="9 13 12 10 15 13"/><line x1="12" y1="10" x2="12" y2="19"/></svg>`,
  template: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/></svg>`,
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
  globe: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/></svg>`,
  target: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.4"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  arrowRight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
  kebab: `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>`,
  search: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>`,
  grid: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.8"/></svg>`,
  list: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg>`,
  help: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.2"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.85.83c0 1.67-2.45 2.5-2.45 2.5"/><line x1="12" y1="17" x2="12" y2="17"/></svg>`,
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

function brandLockup() {
  return `<div class="proj-brand" onclick="window.returnToLanding && window.returnToLanding()" title="Back to Netgravity">
      ${ICONS.logo}
      <div>
        <div class="proj-brand-title">Netgravity</div>
        <div class="proj-brand-sub">by Kearney</div>
      </div>
    </div>`;
}

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
  const title = first ? 'Create your first project' : 'Create a new project';
  const cancelLabel = first ? 'Back to sign in' : 'Cancel';

  const modes = CREATE_MODES.map(m => `
    <button type="button" class="proj-mode-card${m.id === ui.mode ? ' selected' : ''}" data-mode="${m.id}">
      <span class="proj-mode-icon">${ICONS[m.id]}</span>
      <span class="proj-mode-label">${m.label}</span>
    </button>`).join('');

  const regionOpts = REGIONS.map(r => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join('');
  const objectiveOpts = OBJECTIVES.map(o => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join('');

  page.innerHTML = `
    ${decorSvg()}
    ${brandLockup()}
    <div class="proj-create-body">
      <div class="proj-create-head">
        <h1 class="proj-create-title">${title}</h1>
        <p class="proj-create-sub">Set up a logistics network workspace to analyze, simulate, and optimize decisions.</p>
      </div>

      <div class="proj-mode-grid" id="proj-mode-grid">${modes}</div>

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
              <label class="proj-field-label" for="proj-region">Region / Scope</label>
              <select class="proj-select placeholder" id="proj-region">
                <option value="">Select region or scope</option>
                ${regionOpts}
              </select>
            </div>
          </div>
        </div>

        <div class="proj-form-row">
          <span class="proj-row-icon">${ICONS.target}</span>
          <div>
            <label class="proj-field-label" for="proj-objective">Primary objective</label>
            <select class="proj-select placeholder" id="proj-objective">
              <option value="">Select primary objective</option>
              ${objectiveOpts}
            </select>
          </div>
        </div>
      </form>

      <div class="proj-create-actions">
        <button type="button" class="proj-btn-primary" id="proj-create-submit">Create project</button>
        <div class="proj-error" id="proj-create-error"></div>
        <button type="button" class="proj-link-btn" id="proj-create-cancel">${cancelLabel}</button>
      </div>
    </div>`;

  bindCreateProject();
}

function bindCreateProject() {
  const grid = document.getElementById('proj-mode-grid');
  const nameInput = document.getElementById('proj-name');
  const errorEl = document.getElementById('proj-create-error');

  grid?.querySelectorAll('.proj-mode-card').forEach(card => {
    card.addEventListener('click', () => {
      ui.mode = card.dataset.mode;
      grid.querySelectorAll('.proj-mode-card').forEach(c => c.classList.toggle('selected', c === card));
    });
  });

  // Keep the select's placeholder colour until a real option is chosen.
  ['proj-region', 'proj-objective'].forEach(id => {
    const sel = document.getElementById(id);
    sel?.addEventListener('change', () => sel.classList.toggle('placeholder', !sel.value));
  });

  nameInput?.addEventListener('input', () => { if (errorEl) errorEl.textContent = ''; });

  document.getElementById('proj-create-submit')?.addEventListener('click', () => {
    const name = (nameInput?.value || '').trim();
    if (!name) {
      if (errorEl) errorEl.textContent = 'Give the project a name to continue.';
      nameInput?.focus();
      return;
    }

    const project = {
      id: 'pr-' + Date.now().toString(36),
      name,
      region: document.getElementById('proj-region')?.value || 'India',
      updated: 'Just now',
      rank: 0,
      owner: 'You',
      status: 'Draft',
    };
    PROJECTS.unshift(project);
    PROJECTS.forEach((p, i) => { p.rank = i + 1; });
    currentProject = project;

    // Data upload/AI ingestion is the next step, not the app itself —
    // see js/ingestion.js for Upload Data → mapping → network build.
    if (typeof window.showUploadData === 'function') window.showUploadData(project);
    else enterApp();
  });

  document.getElementById('proj-create-cancel')?.addEventListener('click', () => {
    if (ui.createOrigin === 'first') {
      hideProjectPages();
      if (typeof window.returnToLanding === 'function') window.returnToLanding();
    } else {
      showSelectProject();
    }
  });
}

/* ═══════════════════════════════════════════════════════════════
   SELECT PROJECT
   ═══════════════════════════════════════════════════════════════ */
function visibleProjects() {
  const q = ui.search.trim().toLowerCase();
  const list = PROJECTS.filter(p =>
    !q || p.name.toLowerCase().includes(q) || p.region.toLowerCase().includes(q));

  const sorters = {
    updated: (a, b) => a.rank - b.rank,
    name: (a, b) => a.name.localeCompare(b.name),
    status: (a, b) => a.status.localeCompare(b.status) || a.rank - b.rank,
  };
  return list.slice().sort(sorters[ui.sort] || sorters.updated);
}

function statusChip(status) {
  const cls = status === 'In progress' ? 'proj-chip-progress' : 'proj-chip-draft';
  return `<span class="proj-chip ${cls}">${escapeHtml(status)}</span>`;
}

function recentCard(p) {
  return `<div class="proj-recent-card">
      <div class="proj-recent-top">
        <span class="proj-folder-tile">${ICONS.folder}</span>
        <div class="proj-recent-meta">
          <div class="proj-recent-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
          <div class="proj-recent-when">Last opened &nbsp;·&nbsp; ${escapeHtml(p.updated)}</div>
        </div>
        <button class="proj-kebab" type="button" title="More options" data-noopen="1">${ICONS.kebab}</button>
      </div>
      <button class="proj-open-btn" type="button" data-open="${p.id}">
        <span>Open project</span>${ICONS.arrowRight}
      </button>
    </div>`;
}

function listBody(rows) {
  if (!rows.length) {
    return `<div class="proj-table-wrap"><div class="proj-empty">No projects match “${escapeHtml(ui.search)}”.</div></div>`;
  }

  if (ui.view === 'grid') {
    return `<div class="proj-card-grid">${rows.map(p => `
      <div class="proj-grid-card" data-open="${p.id}">
        <div class="proj-recent-top">
          <span class="proj-folder-tile">${ICONS.folder}</span>
          <div class="proj-recent-meta">
            <div class="proj-recent-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
            <div class="proj-recent-when">${escapeHtml(p.region)}</div>
          </div>
        </div>
        <div class="proj-grid-foot"><span>${escapeHtml(p.updated)}</span>${statusChip(p.status)}</div>
      </div>`).join('')}</div>`;
  }

  return `<div class="proj-table-wrap">
      <table class="proj-table">
        <thead>
          <tr>
            <th>Project name</th><th>Region / Scope</th><th>Last updated</th>
            <th>Owner</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(p => `
            <tr data-open="${p.id}">
              <td><span class="proj-row-name">${ICONS.folder}${escapeHtml(p.name)}</span></td>
              <td>${escapeHtml(p.region)}</td>
              <td>${escapeHtml(p.updated)}</td>
              <td><span class="proj-chip proj-chip-owner">${escapeHtml(p.owner)}</span></td>
              <td>${statusChip(p.status)}</td>
              <td><button class="proj-kebab" type="button" title="More options" data-noopen="1">${ICONS.kebab}</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

export function renderSelectProject() {
  const page = document.getElementById('select-project-page');
  if (!page) return;

  const recents = PROJECTS.slice().sort((a, b) => a.rank - b.rank).slice(0, 3);

  page.innerHTML = `
    ${decorSvg()}
    ${brandLockup()}
    <button class="proj-help-btn" type="button" title="Help">${ICONS.help}</button>

    <div class="proj-select-body">
      <div class="proj-select-head">
        <div>
          <h1 class="proj-select-title">Select or create a project</h1>
          <p class="proj-select-sub">Work on your logistics networks, analyze performance, and run AI-powered scenarios.</p>
        </div>
        <button class="proj-btn-primary" type="button" id="proj-new-btn">${ICONS.plus}<span>Create new project</span></button>
      </div>

      <div class="proj-section-title">Recent projects</div>
      <div class="proj-recent-grid">${recents.map(recentCard).join('')}</div>

      <div class="proj-section-title">All projects</div>
      <div class="proj-toolbar">
        <div class="proj-search-wrap">
          <span class="proj-search-icon">${ICONS.search}</span>
          <input class="proj-search" id="proj-search" type="search" placeholder="Search projects" value="${escapeHtml(ui.search)}" autocomplete="off" />
        </div>
        <div class="proj-toolbar-right">
          <label class="proj-sort">Sort by:
            <select id="proj-sort">
              <option value="updated"${ui.sort === 'updated' ? ' selected' : ''}>Last updated</option>
              <option value="name"${ui.sort === 'name' ? ' selected' : ''}>Name</option>
              <option value="status"${ui.sort === 'status' ? ' selected' : ''}>Status</option>
            </select>
          </label>
          <div class="proj-view-toggle">
            <button class="proj-view-btn${ui.view === 'grid' ? ' active' : ''}" type="button" data-view="grid" title="Grid view">${ICONS.grid}</button>
            <button class="proj-view-btn${ui.view === 'list' ? ' active' : ''}" type="button" data-view="list" title="List view">${ICONS.list}</button>
          </div>
        </div>
      </div>

      <div id="proj-list-slot">${listBody(visibleProjects())}</div>
    </div>`;

  bindSelectProject();
}

function refreshList() {
  const slot = document.getElementById('proj-list-slot');
  if (slot) slot.innerHTML = listBody(visibleProjects());
}

function bindSelectProject() {
  const page = document.getElementById('select-project-page');
  if (!page) return;

  document.getElementById('proj-new-btn')?.addEventListener('click', () => showCreateProject('existing'));

  const search = document.getElementById('proj-search');
  search?.addEventListener('input', () => { ui.search = search.value; refreshList(); });

  const sort = document.getElementById('proj-sort');
  sort?.addEventListener('change', () => { ui.sort = sort.value; refreshList(); });

  page.querySelectorAll('.proj-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      ui.view = btn.dataset.view;
      page.querySelectorAll('.proj-view-btn').forEach(b => b.classList.toggle('active', b === btn));
      refreshList();
    });
  });

  // One delegated handler covers recent cards, table rows and grid cards.
  page.addEventListener('click', e => {
    if (e.target.closest('[data-noopen]')) return;
    const opener = e.target.closest('[data-open]');
    if (opener) openProject(opener.dataset.open);
  });
}

/** Called by ingestion.js once a newly created project's data has been
 *  mapped and the network build finishes, so it stops reading "Draft". */
export function markProjectInProgress(id) {
  const p = PROJECTS.find(x => x.id === id);
  if (p) p.status = 'In progress';
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

export function showSelectProject() {
  hideLanding();
  hideProjectPages();
  if (typeof window.hideIngestionPages === 'function') window.hideIngestionPages();
  renderSelectProject();
  const page = document.getElementById('select-project-page');
  if (page) {
    page.classList.remove('hidden');
    page.scrollTop = 0;
  }
}

export function showCreateProject(origin) {
  ui.createOrigin = origin === 'existing' ? 'existing' : 'first';
  ui.mode = 'scratch';
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

/** Leave the project screens and hand off to the authenticated app shell. */
export function enterApp() {
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
}

export function initProjects() {
  if (typeof window !== 'undefined') {
    window.showSelectProject = showSelectProject;
    window.showCreateProject = showCreateProject;
    window.hideProjectPages = hideProjectPages;
    window.enterApp = enterApp;
    window.markProjectInProgress = markProjectInProgress;
    window.getCurrentProject = () => currentProject;
  }
}
