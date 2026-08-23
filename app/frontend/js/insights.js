/**
 * Netgravity — Insights Page Controller
 * =====================================
 * Manages category/severity filtering, insight feed rendering,
 * and insight detail panel population based on the approved reference.
 */

export const INSIGHTS_FEED_DATA = [
  {
    id: 'delhi-capacity-ceiling',
    category: 'CAPACITY RISK',
    categoryFilter: 'Capacity',
    severity: 'Critical',
    accentColor: 'red',
    iconType: 'shield',
    title: 'Delhi NCR DC is approaching its capacity ceiling',
    desc: 'Current utilization is 94%, with forecast demand expected to push peak utilization above available capacity.',
    metrics: [
      { label: 'Utilization', value: '94%', color: 'red' },
      { label: 'Forecast Peak', value: '108%', color: 'red' },
      { label: 'Demand Growth', value: '+14%', color: 'red' }
    ],
    detail: {
      category: 'CAPACITY RISK',
      title: 'Delhi NCR DC is approaching its capacity ceiling',
      desc: 'Current utilization is 94%, with forecast demand expected to push peak utilization above available capacity.',
      whyItMatters: 'Without intervention, projected demand will exceed available DC capacity, increasing risk of service breaches and higher expediting costs.',
      metrics: [
        { label: 'Current Utilization', value: '94%', color: 'red' },
        { label: 'Forecast Peak', value: '108%', color: 'red' },
        { label: 'Demand Growth', value: '+14%', sub: 'vs. current', color: 'red' }
      ],
      evidence: [
        'Demand forecast for Q4 FY26 shows strong growth',
        'Historical peak utilization reached 92% last year',
        'Limited alternate capacity within same service radius',
        'Optimization model indicates constraint in peak period'
      ],
      sources: [
        { name: 'Network Data', icon: '🗄️' },
        { name: 'Demand Forecast', icon: '📈' },
        { name: 'Optimization Model', icon: '⚛️' }
      ],
      confidence: 'High',
      confidenceText: 'Based on validated network data and demand forecast.'
    }
  },
  {
    id: 'kolkata-spare-capacity',
    category: 'COST OPPORTUNITY',
    categoryFilter: 'Cost',
    severity: 'Opportunity',
    accentColor: 'green',
    iconType: 'arrow-down',
    title: 'Kolkata DC has available capacity to absorb additional volume',
    desc: 'Spare capacity in Kolkata can absorb flows from higher-pressure corridors, reducing overall network cost.',
    metrics: [
      { label: 'Utilization', value: '62%', color: 'green' },
      { label: 'Cost Impact', value: '-7.8%', color: 'green' }
    ],
    detail: {
      category: 'COST OPPORTUNITY',
      title: 'Kolkata DC has available capacity to absorb additional volume',
      desc: 'Spare capacity in Kolkata can absorb flows from higher-pressure corridors, reducing overall network cost.',
      whyItMatters: 'Redirecting non-urgent northern overflow to eastern nodes leverages existing fixed warehouse leases and eliminates rush transport surcharges.',
      metrics: [
        { label: 'Current Utilization', value: '53.3%', color: 'green' },
        { label: 'Absorbable Capacity', value: '2,800 u', color: 'green' },
        { label: 'Cost Savings', value: '-7.8%', sub: 'network-wide', color: 'green' }
      ],
      evidence: [
        'Kolkata DC historical throughput is under 60% of baseline capacity',
        'Direct rail and road connectivity exists from Baddi Plant',
        'Handling unit cost in Kolkata is ₹3.5 vs ₹4.2 in Delhi NCR'
      ],
      sources: [
        { name: 'Facility Master', icon: '🏢' },
        { name: 'Cost Baseline', icon: '💰' },
        { name: 'Routing Engine', icon: '🛣️' }
      ],
      confidence: 'High',
      confidenceText: 'Based on audited warehouse throughput and contract freight matrices.'
    }
  },
  {
    id: 'service-buffer-sla',
    category: 'SERVICE RISK',
    categoryFilter: 'Service',
    severity: 'Attention',
    accentColor: 'amber',
    iconType: 'alert-triangle',
    title: 'Limited service buffer against demand growth',
    desc: 'Projected demand growth may lead to SLA breaches in key lanes by Q4 FY26 without network adjustments.',
    metrics: [
      { label: 'SLA Risk', value: '18% ↑', color: 'amber' },
      { label: 'Lead Time', value: '+1.4 days', color: 'amber' }
    ],
    detail: {
      category: 'SERVICE RISK',
      title: 'Limited service buffer against demand growth',
      desc: 'Projected demand growth may lead to SLA breaches in key lanes by Q4 FY26 without network adjustments.',
      whyItMatters: 'Lead times on Delhi-to-consumer fulfillment lanes are projected to stretch from 1.0 day to 2.4 days during peak demand spikes.',
      metrics: [
        { label: 'On-Time SLA', value: '96.7%', color: 'amber' },
        { label: 'Peak SLA Risk', value: '18% ↑', color: 'amber' },
        { label: 'Lead Time Drift', value: '+1.4 d', sub: 'in peak', color: 'amber' }
      ],
      evidence: [
        'Tier-1 consumer markets in North zone show 14% Q4 demand growth',
        'Last-mile dispatch docks operate at 91% capacity during morning surge',
        'Carrier turn-around time increases by 28% during winter fog window'
      ],
      sources: [
        { name: 'SLA Telemetry', icon: '⏱️' },
        { name: 'Demand Forecast', icon: '📈' },
        { name: 'Carrier Logs', icon: '🚚' }
      ],
      confidence: 'High',
      confidenceText: 'Based on historical SLA telemetry and carrier dispatch logs.'
    }
  },
  {
    id: 'single-lane-dependence',
    category: 'NETWORK INSIGHT',
    categoryFilter: 'Network',
    severity: 'Informational',
    accentColor: 'purple',
    iconType: 'network',
    title: 'High dependence on single-lane routes',
    desc: '34% of total volume is routed through single-lane corridors, increasing disruption risk.',
    metrics: [
      { label: 'Single-lane', value: '34%', color: 'purple' },
      { label: 'Risk', value: 'Medium', color: 'dark' }
    ],
    detail: {
      category: 'NETWORK INSIGHT',
      title: 'High dependence on single-lane routes',
      desc: '34% of total volume is routed through single-lane corridors, increasing disruption risk.',
      whyItMatters: 'Network resilience analysis indicates that a localized outage or highway closure on NH44 would disrupt over one-third of active national shipments.',
      metrics: [
        { label: 'Single-Lane Volume', value: '34%', color: 'purple' },
        { label: 'Alternate Route Lead Time', value: '+2.1 d', color: 'purple' },
        { label: 'Resilience Score', value: '68/100', sub: 'index', color: 'purple' }
      ],
      evidence: [
        'Baddi-to-Delhi primary freight corridor handles 8,200 units/day',
        'Secondary bypass routes currently lack pre-contracted carrier SLA',
        'Optimization model identifies 2 viable alternate multi-modal paths'
      ],
      sources: [
        { name: 'Lane GIS Data', icon: '🗺️' },
        { name: 'Disruption Simulator', icon: '⚡' },
        { name: 'Network Graph', icon: '🌐' }
      ],
      confidence: 'High',
      confidenceText: 'Derived from topology analysis and multi-commodity graph flow simulations.'
    }
  },
  {
    id: 'low-emission-modes',
    category: 'SUSTAINABILITY OPPORTUNITY',
    categoryFilter: 'Resilience',
    severity: 'Opportunity',
    accentColor: 'green',
    iconType: 'leaf',
    title: 'Shift to low-emission modes can reduce CO₂ emissions',
    desc: 'Optimizing mode mix and routes can reduce emissions by 6.1% with minimal cost impact.',
    metrics: [
      { label: 'CO₂ Impact', value: '↓ 6.1%', color: 'green' },
      { label: 'Cost Impact', value: 'Low', color: 'green' }
    ],
    detail: {
      category: 'SUSTAINABILITY OPPORTUNITY',
      title: 'Shift to low-emission modes can reduce CO₂ emissions',
      desc: 'Optimizing mode mix and routes can reduce emissions by 6.1% with minimal cost impact.',
      whyItMatters: 'Modal shifting of long-haul non-perishable freight from road to dedicated freight rail corridors achieves sustainability targets while maintaining lead-time SLA.',
      metrics: [
        { label: 'CO₂ Reduction', value: '-6.1%', color: 'green' },
        { label: 'Carbon Avoided', value: '1,135 t', color: 'green' },
        { label: 'Cost Variance', value: '+0.2%', sub: 'negligible', color: 'green' }
      ],
      evidence: [
        'Western and Eastern Dedicated Freight Corridors (DFC) provide 98% rail transit reliability',
        'Current carbon baseline is 18.6K tonnes per quarter',
        'Modal shift potential is highest on Pune-to-Kolkata and Baddi-to-Mumbai lanes'
      ],
      sources: [
        { name: 'Emission Factor DB', icon: '🌱' },
        { name: 'Freight Rates Matrix', icon: '📊' },
        { name: 'Modal Engine', icon: '🚂' }
      ],
      confidence: 'High',
      confidenceText: 'Validated against GLEC framework carbon calculations and rail container tariffs.'
    }
  }
];

let activeFilter = 'All';
let activeSeverity = 'All';
let selectedInsightId = 'delhi-capacity-ceiling';

/**
 * Initialize the Insights Page
 */
export function initInsightsPage() {
  bindInsightsEvents();
  renderInsightsSummary();
  renderInsightsFeed();
  renderInsightDetail(selectedInsightId);
}

/**
 * Bind Filter and Click Events
 */
function bindInsightsEvents() {
  // Category Filter Pills
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      activeFilter = pill.getAttribute('data-filter') || 'All';
      renderInsightsFeed();
    });
  });

  // Severity Dropdown
  const severitySelect = document.getElementById('filter-severity-select');
  if (severitySelect) {
    severitySelect.addEventListener('change', (e) => {
      activeSeverity = e.target.value;
      renderInsightsFeed();
    });
  }

  // Ask Netgravity Button
  const btnAsk = document.getElementById('btn-insights-ask-netgravity');
  if (btnAsk) {
    btnAsk.addEventListener('click', () => {
      if (typeof window.navigateToTab === 'function') {
        window.navigateToTab('home');
      }
    });
  }
}

/**
 * Render Summary Cards
 */
function renderInsightsSummary() {
  const container = document.getElementById('insights-summary-grid');
  if (!container) return;

  container.innerHTML = `
    <div class="summary-card">
      <div class="summary-card-top-row">
        <div class="summary-icon-box summary-icon-purple">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
          </svg>
        </div>
        <div class="summary-number">5</div>
      </div>
      <div class="summary-title">Key Insights</div>
      <div class="summary-sub">Across 6 categories</div>
    </div>

    <div class="summary-card">
      <div class="summary-card-top-row">
        <div class="summary-icon-box summary-icon-green">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <line x1="12" y1="5" x2="12" y2="19"></line>
            <polyline points="19 12 12 19 5 12"></polyline>
          </svg>
        </div>
        <div class="summary-number">2</div>
      </div>
      <div class="summary-title">Cost Opportunities</div>
      <div class="summary-sub">Potential savings identified</div>
    </div>

    <div class="summary-card">
      <div class="summary-card-top-row">
        <div class="summary-icon-box summary-icon-red">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
          </svg>
        </div>
        <div class="summary-number">2</div>
      </div>
      <div class="summary-title">Capacity Risks</div>
      <div class="summary-sub">Require attention</div>
    </div>

    <div class="summary-card">
      <div class="summary-card-top-row">
        <div class="summary-icon-box summary-icon-amber">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
            <line x1="12" y1="9" x2="12" y2="13"></line>
            <line x1="12" y1="17" x2="12.01" y2="17"></line>
          </svg>
        </div>
        <div class="summary-number">1</div>
      </div>
      <div class="summary-title">Service Risk</div>
      <div class="summary-sub">Impacting SLA</div>
    </div>
  `;
}

/**
 * Render Feed of Insight Cards
 */
export function renderInsightsFeed() {
  const container = document.getElementById('insights-feed-list');
  if (!container) return;

  const filtered = INSIGHTS_FEED_DATA.filter(item => {
    const matchCategory = activeFilter === 'All' || item.categoryFilter === activeFilter;
    const matchSeverity = activeSeverity === 'All' || item.severity === activeSeverity;
    return matchCategory && matchSeverity;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div style="padding: 48px; text-align: center; color: #6b7280; background: #fff; border-radius: 12px; border: 1px dashed #e5e7eb;">
        <p style="font-weight: 600; font-size: 15px; color: #374151;">No matching insights found</p>
        <p style="font-size: 13px; margin-top: 4px;">Try selecting a different filter category or severity level.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(item => {
    const isSelected = item.id === selectedInsightId;
    const iconSvg = getIconSvg(item.iconType);

    return `
      <div class="insight-feed-card insight-card-${item.accentColor} ${isSelected ? 'selected' : ''}" data-insight-id="${item.id}">
        <div class="insight-card-icon-wrap icon-wrap-${item.accentColor}">
          ${iconSvg}
        </div>
        <div class="insight-card-body">
          <div class="insight-card-top-meta">
            <span class="insight-category-label category-${item.accentColor}">${item.category}</span>
            <span class="insight-status-tag tag-${item.severity.toLowerCase()}">${item.severity}</span>
          </div>
          <div class="insight-card-title">${item.title}</div>
          <div class="insight-card-desc">${item.desc}</div>
          <div class="insight-card-footer">
            <div class="insight-card-metrics">
              ${item.metrics.map(m => `
                <div class="card-metric-item">
                  <span class="metric-item-label">${m.label}</span>
                  <span class="metric-item-value val-${m.color}">${m.value}</span>
                </div>
              `).join('')}
            </div>
            <div class="insight-card-action">
              <span>Explore Insight</span>
              <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2">
                <path d="M5 10h10M11 6l4 4-4 4"/>
              </svg>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join('');

  // Bind click on each card
  container.querySelectorAll('.insight-feed-card').forEach(card => {
    card.addEventListener('click', () => {
      const id = card.getAttribute('data-insight-id');
      if (id) {
        selectedInsightId = id;
        container.querySelectorAll('.insight-feed-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        renderInsightDetail(id);
      }
    });
  });
}

/**
 * Render Detail Panel for Selected Insight
 */
export function renderInsightDetail(insightId) {
  const panel = document.getElementById('insight-detail-panel');
  if (!panel) return;

  panel.style.display = 'flex';

  const item = INSIGHTS_FEED_DATA.find(i => i.id === insightId) || INSIGHTS_FEED_DATA[0];
  const d = item.detail;

  panel.innerHTML = `
    <div class="detail-panel-header">
      <div class="detail-category-badge category-${item.accentColor}">
        <span>${getCategoryIcon(item.category)}</span>
        <span>${d.category}</span>
      </div>
      <button class="detail-close-btn" id="btn-close-detail" title="Close Panel">✕</button>
    </div>

    <div class="detail-title">${d.title}</div>
    <div class="detail-desc">${d.desc}</div>

    <div class="detail-section-block">
      <div class="detail-section-heading">Why it matters</div>
      <div class="detail-why-text">${d.whyItMatters}</div>
    </div>

    <div class="detail-metrics-grid">
      ${d.metrics.map(m => `
        <div class="detail-metric-card">
          <div class="detail-metric-label">${m.label}</div>
          <div class="detail-metric-val val-${m.color}">${m.value}</div>
          ${m.sub ? `<div class="detail-metric-sub">${m.sub}</div>` : ''}
        </div>
      `).join('')}
    </div>

    <div class="detail-section-block">
      <div class="detail-section-heading">Evidence</div>
      <div class="detail-evidence-list">
        ${d.evidence.map(e => `
          <div class="evidence-bullet-item">
            <span class="evidence-dot"></span>
            <span>${e}</span>
          </div>
        `).join('')}
      </div>
    </div>

    <div class="detail-section-block">
      <div class="detail-section-heading">Source</div>
      <div class="detail-source-row">
        ${d.sources.map(s => `
          <div class="source-item">
            <span class="source-icon">${s.icon}</span>
            <span>${s.name}</span>
          </div>
        `).join('')}
      </div>
    </div>

    <div class="detail-confidence-box">
      <div class="detail-section-heading">Confidence</div>
      <div class="confidence-val-row">
        <span class="confidence-high-tag">${d.confidence}</span>
      </div>
      <div class="confidence-support-text">${d.confidenceText}</div>
    </div>
  `;

  // Bind close button
  const btnClose = panel.querySelector('#btn-close-detail');
  if (btnClose) {
    btnClose.addEventListener('click', () => {
      panel.style.display = 'none';
    });
  } else {
    panel.style.display = 'flex';
  }
}

/**
 * Return SVGs for Icons
 */
function getIconSvg(type) {
  switch (type) {
    case 'shield':
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`;
    case 'arrow-down':
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"></line><polyline points="19 12 12 19 5 12"></polyline></svg>`;
    case 'alert-triangle':
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`;
    case 'network':
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line></svg>`;
    case 'leaf':
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>`;
    default:
      return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>`;
  }
}

function getCategoryIcon(cat) {
  if (cat.includes('CAPACITY')) return '🛡️';
  if (cat.includes('COST')) return '📉';
  if (cat.includes('SERVICE')) return '⚠️';
  if (cat.includes('NETWORK')) return '🌐';
  if (cat.includes('SUSTAINABILITY')) return '🌱';
  return '✦';
}

if (typeof window !== 'undefined') {
  window.initInsightsPage = initInsightsPage;
  window.renderInsightsFeed = renderInsightsFeed;
  window.renderInsightDetail = renderInsightDetail;
}



/**
 * Open Insight Detail Slide-in Drawer from Right with Blurred Backdrop
 */
export function openInsightDrawer(insightIdOrObj) {
  const overlay = document.getElementById('insight-drawer-overlay');
  const content = document.getElementById('insight-drawer-content');
  if (!overlay || !content) return;

  let ins = null;
  if (typeof insightIdOrObj === 'object' && insightIdOrObj !== null) {
    ins = insightIdOrObj;
  } else if (typeof insightIdOrObj === 'string') {
    if (typeof HOME_INSIGHTS !== 'undefined') {
      for (const fac in HOME_INSIGHTS) {
        const found = HOME_INSIGHTS[fac].find(i => i.id === insightIdOrObj);
        if (found) { ins = found; break; }
      }
    }
    if (!ins && typeof INSIGHTS_FEED_DATA !== 'undefined') {
      ins = INSIGHTS_FEED_DATA.find(i => i.id === insightIdOrObj);
    }
  }
  if (!ins && typeof INSIGHTS_FEED_DATA !== 'undefined' && INSIGHTS_FEED_DATA.length > 0) {
    ins = INSIGHTS_FEED_DATA[0];
  }
  if (!ins) return;

  const title = ins.title || 'Insight Details';
  const category = ins.detail?.category || ins.category || 'NETWORK OPTIMIZATION';
  const why = ins.detail?.whyItMatters || ins.why || 'Critical network condition requiring tactical rebalancing.';
  const whatIFound = ins.detail?.whatIFound || ins.subtitle || ins.desc || '';
  const evidence = ins.detail?.evidence || [];
  const metrics = ins.detail?.metrics || [];

  content.innerHTML = `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;">
      <div style="display:inline-flex;padding:4px 10px;border-radius:12px;font-size:11px;font-weight:700;letter-spacing:0.04em;background:#f5f3ff;color:#9218EA;">
        <span>${category.toUpperCase()}</span>
      </div>
      <button class="facility-panel-close" onclick="window.closeInsightDrawer && window.closeInsightDrawer()" style="background:none;border:none;font-size:22px;color:#9ca3af;cursor:pointer;padding:4px;line-height:1;">✕</button>
    </div>

    <h2 style="font-size:20px;font-weight:800;color:#111827;line-height:1.3;margin:0 0 8px;">${title}</h2>
    ${whatIFound ? `<p style="font-size:13.5px;color:#4b5563;line-height:1.5;margin:0 0 16px;">${whatIFound}</p>` : ''}

    <div style="background:#fafafc;border:1px solid #f0f0f5;border-radius:12px;padding:14px 16px;margin-bottom:16px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:#9218EA;margin-bottom:6px;letter-spacing:0.05em;">Why it matters</div>
      <div style="font-size:13px;color:#374151;line-height:1.55;">${why}</div>
    </div>

    ${metrics.length > 0 ? `
      <div class="detail-metrics-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px;">
        ${metrics.map(m => `
          <div class="detail-metric-card" style="background:#fafafc;border:1px solid #f0f0f5;border-radius:10px;padding:12px 10px;display:flex;flex-direction:column;gap:4px;">
            <div style="font-size:11px;color:#6b7280;">${m.label}</div>
            <div style="font-size:18px;font-weight:800;color:${m.color === 'red' ? '#ef4444' : m.color === 'green' ? '#10b981' : '#9218EA'};">${m.value}</div>
            ${m.sub ? `<div style="font-size:10.5px;color:#9ca3af;">${m.sub}</div>` : ''}
          </div>
        `).join('')}
      </div>
    ` : ''}

    ${evidence.length > 0 ? `
      <div style="margin-bottom:16px;">
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:#6b7280;margin-bottom:8px;letter-spacing:0.05em;">Evidence & Findings</div>
        <div style="display:flex;flex-direction:column;gap:8px;">
          ${evidence.map(e => {
            const lbl = typeof e === 'string' ? e : `${e.label}: ${e.value}`;
            const prov = e.provenance ? `<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:#f3f4f6;color:#6b7280;margin-left:auto;">${e.provenance}</span>` : '';
            return `
              <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:#ffffff;border:1px solid #eef0f3;border-radius:8px;font-size:12.5px;color:#374151;">
                <div style="display:flex;align-items:center;gap:8px;">
                  <span style="width:6px;height:6px;border-radius:50%;background:#9218EA;flex-shrink:0;"></span>
                  <span>${lbl}</span>
                </div>
                ${prov}
              </div>
            `;
          }).join('')}
        </div>
      </div>
    ` : ''}

    <div style="display:flex;flex-direction:column;gap:10px;margin-top:auto;padding-top:16px;">
      <button class="btn-primary" onclick="window.navigateToTab && window.navigateToTab('scenarios'); window.closeInsightDrawer && window.closeInsightDrawer();" style="width:100%;padding:12px;border-radius:8px;background:#9218EA;color:#fff;border:none;font-weight:600;font-size:13px;cursor:pointer;box-shadow:0 2px 8px rgba(146,24,234,0.25);">
        Simulate Rebalancing in Scenario Planner →
      </button>
      <button onclick="window.navigateToTab && window.navigateToTab('insights'); window.closeInsightDrawer && window.closeInsightDrawer();" style="width:100%;padding:9px;border-radius:8px;background:#f9fafb;color:#374151;border:1px solid #e5e7eb;font-weight:600;font-size:12.5px;cursor:pointer;">
        Open Full Insights Page →
      </button>
    </div>
  `;

  overlay.classList.add('active');
  overlay.style.display = 'flex';
}

export function closeInsightDrawer() {
  const overlay = document.getElementById('insight-drawer-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.style.display = 'none';
  }
}

if (typeof window !== 'undefined') {
  window.openInsightDrawer = openInsightDrawer;
  window.closeInsightDrawer = closeInsightDrawer;
}