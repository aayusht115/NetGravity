/**
 * NetGravity — Recommendations Workspace Controller
 * ==================================================
 * Implements the executive Recommendations page mirroring the Insights design language:
 * - Category filter pills + Priority dropdown selector
 * - 4 Contextual KPI Summary Cards
 * - Left interactive Recommendations feed
 * - Right detailed intervention breakdown with Impact Grid, Telemetry, and CTAs
 */

export const RECOMMENDATIONS_DATA = [
  {
    id: 'REC_REBALANCE_BADDI',
    title: 'Rebalance Baddi Manufacturing Volume → Kolkata DC',
    category: 'FLOW REBALANCING',
    categoryFilter: 'Flow Rebalancing',
    priority: 'High Impact',
    priorityClass: 'high-impact',
    accentColor: 'purple',
    status: 'Pending Review',
    desc: 'Shift 1,800 units/day from the constrained Baddi → Delhi corridor to Kolkata DC to relieve northern warehouse bottleneck and capture ₹2.4L monthly freight savings.',
    impact: {
      cost: '↓ ₹2.4L/mo',
      costPct: '-7.8%',
      sla: '96.7%',
      peakUtil: '↓ 14pp',
      capex: '₹0'
    },
    detail: {
      category: 'FLOW REBALANCING',
      title: 'Rebalance Baddi Manufacturing Volume → Kolkata DC',
      status: 'Pending Review',
      why: 'Delhi NCR DC is operating at 94% utilization with peak forecast approaching 108%. Diverting 1,800 units/day of Baddi volume to Kolkata DC utilizes available eastern warehouse buffer (53.3% utilization), eliminating spot-market premium freight and ensuring SLA resilience throughout Q4 festive surge.',
      impactGrid: [
        { label: 'Net Cost Impact', val: '-7.8% (₹2.4L/mo)', color: 'green' },
        { label: 'Network SLA', val: '96.7% (≥95% Target)', color: 'green' },
        { label: 'Delhi Peak Util', val: '80.0% (↓ 14pp)', color: 'green' },
        { label: 'Required CapEx', val: '₹0 (Immediate)', color: 'purple' }
      ],
      rootCause: [
        { label: 'Delhi DC Utilization', value: '94.0% (Near Ceiling)', provenance: 'SAP ERP WMS' },
        { label: 'Delhi Q4 Projected Peak', value: '108% (Capacity Breach)', provenance: 'DEMAND FORECAST' },
        { label: 'Kolkata DC Available Headroom', value: '2,800 units/day spare', provenance: 'FACILITY MASTER' },
        { label: 'Expedited Surcharge Avoidance', value: '₹1.85L / month', provenance: 'CARRIER AUDIT' }
      ],
      rejectedAlternatives: [
        { name: 'Expand Delhi NCR DC Immediately', reason: 'Requires ₹15L CapEx and 6-month civil build lead time; cannot solve immediate Q4 demand spike.' },
        { name: 'Reroute Baddi Volume to Mumbai DC', reason: 'Adds +1.8 days transit lead time to Eastern demand nodes and breaches 95% SLA target.' }
      ],
      executionSteps: [
        { step: 1, action: 'Update SAP ERP multi-echelon allocation matrix for Baddi Plant outputs', owner: 'SCM Lead' },
        { step: 2, action: 'Notify Eastern logistics 3PL partner for +1,800 u/d inbound handling', owner: 'Logistics Operations' },
        { step: 3, action: 'Monitor Delhi NCR daily throughput ceiling for 14-day stabilization', owner: 'NetGravity AI Watchdog' }
      ],
      analystEmail: `Subject: Recommended Action: Baddi Volume Flow Rebalancing to Kolkata DC

Executive Summary:
NetGravity has evaluated the upcoming Q4 demand surge (+14.2% in Northern markets) and determined that Delhi NCR DC will reach 108% capacity breach by Dec 2026.

Recommended Decision:
Reallocate 1,800 units/day from Baddi → Delhi corridor to Kolkata DC.

Key Benefits:
1. Net Cost Reduction: ₹2.4 Lakhs/month (-7.8% total freight expenditure).
2. Capacity Protection: Reduces Delhi DC peak utilization from 108% to a sustainable 80%.
3. SLA Compliance: Preserves 96.7% on-time order fulfillment.
4. Capital Expenditure: ₹0 (Software-driven allocation).

Please review and confirm approval in NetGravity Scenario Planning.`
    }
  },
  {
    id: 'REC_WESTERN_CONSOLIDATION',
    title: 'Implement Multi-Drop Consolidation on Western Corridor',
    category: 'COST OPTIMIZATION',
    categoryFilter: 'Cost Optimization',
    priority: 'Immediate Action',
    priorityClass: 'opportunity',
    accentColor: 'green',
    status: 'Pending Review',
    desc: 'Consolidate fragmented LTL shipments across Mumbai → Pune → Ahmedabad manufacturing routes into scheduled FTL milk-runs.',
    impact: {
      cost: '↓ ₹1.1L/mo',
      costPct: '-4.2%',
      sla: '97.2%',
      peakUtil: 'Stable',
      capex: '₹0'
    },
    detail: {
      category: 'COST OPTIMIZATION',
      title: 'Implement Multi-Drop Consolidation on Western Corridor',
      status: 'Pending Review',
      why: 'Current LTL shipments between Mumbai DC and satellite distribution hubs operate with 64% trailer cubic fill. Scheduling 3x weekly consolidated multi-drop routes increases load efficiency to 91%, capturing ₹1.1L/month in freight savings while lowering carbon footprint.',
      impactGrid: [
        { label: 'Net Cost Impact', val: '-4.2% (₹1.1L/mo)', color: 'green' },
        { label: 'Western Hub SLA', val: '97.2% (+0.8pp)', color: 'green' },
        { label: 'Fleet Fill Rate', val: '91% (↑ 27pp)', color: 'green' },
        { label: 'Carbon Reduction', val: '↓ 6.2% CO2e', color: 'purple' }
      ],
      rootCause: [
        { label: 'Current Western LTL Fill Rate', value: '64.0% average', provenance: 'TMS TELEMETRY' },
        { label: 'Duplicate Transit Runs', value: '14 runs / week', provenance: 'DISPATCH LOGS' },
        { label: 'Consolidation Target Headroom', value: '420 tons / month', provenance: 'OR-TOOLS SOLVER' }
      ],
      rejectedAlternatives: [
        { name: 'Shift all volume to single dedicated carrier', reason: 'High vendor concentration risk without significant spot-rate concession.' }
      ],
      executionSteps: [
        { step: 1, action: 'Align delivery windows for Pune and Ahmedabad retail distribution hubs', owner: 'Regional Distribution Mgr' },
        { step: 2, action: 'Issue revised route tenders for dedicated multi-drop 32ft MXL trailers', owner: 'Procurement' }
      ],
      analystEmail: `Subject: Action Item: Western Corridor FTL Milk-Run Consolidation

Team,
Analysis of western corridor lane telemetry indicates opportunity to eliminate 6 weekly duplicate dispatch runs by consolidating Mumbai-Pune-Ahmedabad freight into multi-drop FTL schedules.

Projected impact: ₹1.1L/month freight savings and 6.2% CO2e emissions reduction.`
    }
  },
  {
    id: 'REC_NORTHERN_CARRIER_BACKUP',
    title: 'Establish Secondary 3PL Carrier on Fog-Prone Northern Lanes',
    category: 'CARRIER & ROUTING',
    categoryFilter: 'Carrier & Routing',
    priority: 'Low Risk / High ROI',
    priorityClass: 'low-risk',
    accentColor: 'amber',
    status: 'Pending Review',
    desc: 'Contract secondary regional carrier with guaranteed winter fog SLA terms for Baddi → Delhi corridor to prevent delivery delays and spot price gouging.',
    impact: {
      cost: 'Protected',
      costPct: '0.0%',
      sla: '98.2%',
      peakUtil: 'Safe',
      capex: '₹0'
    },
    detail: {
      category: 'CARRIER & ROUTING',
      title: 'Establish Secondary 3PL Carrier on Fog-Prone Northern Lanes',
      status: 'Pending Review',
      why: 'Historical winter lane data (Nov–Jan) shows an average 3.2-day transit delay due to fog disruptions when reliant on single primary carrier. Pre-negotiating secondary capacity at contracted baseline rates protects service levels from dropping below 90% during weather events.',
      impactGrid: [
        { label: 'Risk Exposure', val: '↓ 65% Variance', color: 'green' },
        { label: 'Protected SLA', val: '98.2% in Winter', color: 'green' },
        { label: 'Spot Rate Premium', val: '₹0 Surcharge', color: 'green' },
        { label: 'Contract Lead Time', val: '10 Days', color: 'purple' }
      ],
      rootCause: [
        { label: 'Winter Fog Delay Variance', value: '+3.2 Days historical', provenance: 'HISTORICAL TMS' },
        { label: 'Single-Carrier Exposure', value: '100% volume on Lane 1', provenance: 'CONTRACT MASTER' },
        { label: 'SLA At-Risk Revenue', value: '₹8.4L in Q4', provenance: 'FINANCE AUDIT' }
      ],
      rejectedAlternatives: [
        { name: 'Rely on spot-market expedited freight', reason: 'Historical spot rates surge +45% during winter fog disruptions.' }
      ],
      executionSteps: [
        { step: 1, action: 'Finalize standby master service agreement with Secondary Carrier B', owner: 'Procurement SCM' },
        { step: 2, action: 'Configure automatic overflow dispatch trigger in NetGravity', owner: 'IT Operations' }
      ],
      analystEmail: `Subject: Risk Mitigation: Northern Lane Dual-Carrier Strategy

Leadership,
To protect Q4 on-time delivery across North India, NetGravity advises activating a dual-carrier agreement for the Baddi–Delhi corridor with contracted backup terms, mitigating ₹8.4L in at-risk SLA penalties.`
    }
  },
  {
    id: 'REC_DELHI_EXPANSION_PHASE1',
    title: 'Plan Delhi NCR DC Modular Capacity Expansion (Phase 1)',
    category: 'STRATEGIC EXPANSION',
    categoryFilter: 'Strategic Expansion',
    priority: 'Strategic Opportunity',
    priorityClass: 'strategic',
    accentColor: 'blue',
    status: 'Pending Review',
    desc: 'Evaluate 2,000 units/day modular racking and dock expansion at Delhi NCR facility to support long-term FY27 demand growth.',
    impact: {
      cost: '₹15L CapEx',
      costPct: '14 Mo Payback',
      sla: '99.0%',
      peakUtil: '72.0%',
      capex: '₹15.0L'
    },
    detail: {
      category: 'STRATEGIC EXPANSION',
      title: 'Plan Delhi NCR DC Modular Capacity Expansion (Phase 1)',
      status: 'Pending Review',
      why: 'While short-term flow rebalancing resolves immediate FY26 constraints, long-term network projections predict Northern demand will permanently outgrow 10,000 u/d by mid-2027. A brownfield modular racking expansion adds +2,000 u/d capacity with a 14-month ROI payback.',
      impactGrid: [
        { label: 'Capacity Ceiling', val: '12,000 u/d (+20%)', color: 'green' },
        { label: 'Estimated CapEx', val: '₹15.0 Lakhs', color: 'purple' },
        { label: 'Payback Period', val: '14 Months', color: 'green' },
        { label: 'Long-term SLA', val: '99.0% Projected', color: 'green' }
      ],
      rootCause: [
        { label: 'FY27 Projected Baseline Demand', value: '11,400 u/d', provenance: 'AI 12-MO FORECAST' },
        { label: 'Warehouse Footprint Expansion Room', value: '4,500 sq.ft available', provenance: 'REAL ESTATE LEASE' },
        { label: 'Expansion Payback IRR', value: '28.4% Return', provenance: 'CAPEX MODEL' }
      ],
      rejectedAlternatives: [
        { name: 'Greenfield New Build in Gurgaon', reason: 'High CapEx (₹85L+) and 18-month construction timeline vs modular brownfield expansion.' }
      ],
      executionSteps: [
        { step: 1, action: 'Commission detailed civil layout and automated racking vendor RFQ', owner: 'Infrastructure Lead' },
        { step: 2, action: 'Submit Capital Appropriation Request to Executive Committee', owner: 'VP Supply Chain' }
      ],
      analystEmail: `Subject: Strategic Investment Brief: Delhi NCR DC Modular Expansion

Executive Committee,
NetGravity has modeled FY27-FY28 network scaling requirements. A modular brownfield racking expansion of 2,000 u/d at Delhi NCR is recommended with ₹15L CapEx and 14-month payback.`
    }
  }
];

let activeRecFilter = 'All';
let activeRecPriority = 'All';
let selectedRecId = 'REC_REBALANCE_BADDI';

/**
 * Initialize Recommendations Page
 */
export function initRecommendationsPage() {
  renderRecsSummaryGrid();
  renderRecsFeed();
  renderRecDetail(selectedRecId);
  bindRecFilterEvents();
}

/**
 * Render 4 Top KPI Summary Cards
 */
export function renderRecsSummaryGrid() {
  const container = document.getElementById('recs-summary-grid');
  if (!container) return;

  const totalRecs = RECOMMENDATIONS_DATA.length;
  const approvedCount = RECOMMENDATIONS_DATA.filter(r => r.status === 'Approved').length;

  container.innerHTML = `
    <!-- Card 1: Active Interventions -->
    <div class="recs-summary-card">
      <div class="recs-summary-card-top">
        <div class="recs-summary-card-icon purple">✦</div>
        <span class="recs-summary-card-tag purple">${approvedCount > 0 ? `${approvedCount} Approved` : 'AI Prescribed'}</span>
      </div>
      <div class="recs-summary-card-value">${totalRecs} Decisions</div>
      <div class="recs-summary-card-label">${totalRecs - approvedCount} Pending Review · ${approvedCount} Approved</div>
    </div>

    <!-- Card 2: Total Cost Impact -->
    <div class="recs-summary-card">
      <div class="recs-summary-card-top">
        <div class="recs-summary-card-icon green">↓</div>
        <span class="recs-summary-card-tag green">-13.0% Network</span>
      </div>
      <div class="recs-summary-card-value">₹3.5L / mo</div>
      <div class="recs-summary-card-label">Combined Net Monthly Savings</div>
    </div>

    <!-- Card 3: Capacity Relief -->
    <div class="recs-summary-card">
      <div class="recs-summary-card-top">
        <div class="recs-summary-card-icon amber">🛡️</div>
        <span class="recs-summary-card-tag amber">100% Protected</span>
      </div>
      <div class="recs-summary-card-value">0 Breaches</div>
      <div class="recs-summary-card-label">Eliminates Delhi 108% Peak Risk</div>
    </div>

    <!-- Card 4: Required Capital -->
    <div class="recs-summary-card">
      <div class="recs-summary-card-top">
        <div class="recs-summary-card-icon blue">⚡</div>
        <span class="recs-summary-card-tag blue">Immediate ROI</span>
      </div>
      <div class="recs-summary-card-value">₹0 CapEx</div>
      <div class="recs-summary-card-label">Zero Initial Capital for Phase 1</div>
    </div>
  `;
}

/**
 * Render Left Recommendations Feed
 */
export function renderRecsFeed() {
  const container = document.getElementById('recs-feed-list');
  if (!container) return;

  const filtered = RECOMMENDATIONS_DATA.filter(rec => {
    const matchCategory = activeRecFilter === 'All' || rec.categoryFilter === activeRecFilter;
    const matchPriority = activeRecPriority === 'All' || rec.priority === activeRecPriority;
    return matchCategory && matchPriority;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div style="padding: 40px; text-align: center; color: #6b7280; background: #fff; border-radius: 12px; border: 1px dashed #e5e7eb;">
        <p style="font-weight: 600; font-size: 14px; color: #374151;">No matching recommendations found</p>
        <p style="font-size: 12.5px; margin-top: 4px;">Try selecting a different filter category or priority level.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(rec => {
    const isSelected = rec.id === selectedRecId;
    return `
      <div class="rec-card-item rec-card-${rec.accentColor} ${isSelected ? 'selected' : ''}" data-rec-id="${rec.id}">
        <div class="rec-card-icon-wrap rec-icon-${rec.accentColor}">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z"/>
          </svg>
        </div>
        <div class="rec-card-body">
          <div class="rec-card-top-meta">
            <span class="rec-category-label rec-category-${rec.accentColor}">${rec.category}</span>
            <span class="rec-priority-tag rec-tag-${rec.priorityClass}">${rec.priority}</span>
          </div>
          <div class="rec-card-title">${rec.title}</div>
          <div class="rec-card-desc">${rec.desc}</div>
          <div class="rec-card-footer">
            <div class="rec-card-metrics">
              <div class="rec-metric-item">
                <span class="rec-metric-item-label">Cost Impact</span>
                <span class="rec-metric-item-value val-green">${rec.impact.cost}</span>
              </div>
              <div class="rec-metric-item">
                <span class="rec-metric-item-label">Target SLA</span>
                <span class="rec-metric-item-value">${rec.impact.sla}</span>
              </div>
              <div class="rec-metric-item">
                <span class="rec-metric-item-label">Peak Util</span>
                <span class="rec-metric-item-value val-purple">${rec.impact.peakUtil}</span>
              </div>
            </div>
            <div class="rec-card-action">
              <span>Explore Action</span>
              <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2">
                <path d="M5 10h10M11 6l4 4-4 4"/>
              </svg>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join('');

  // Bind click handlers
  container.querySelectorAll('.rec-card-item').forEach(card => {
    card.addEventListener('click', () => {
      const id = card.getAttribute('data-rec-id');
      if (id) {
        selectedRecId = id;
        container.querySelectorAll('.rec-card-item').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        renderRecDetail(id);
      }
    });
  });
}

/**
 * Render Right Detail Panel for Selected Recommendation
 */
export function renderRecDetail(recId) {
  const panel = document.getElementById('rec-detail-panel');
  if (!panel) return;

  const rec = RECOMMENDATIONS_DATA.find(r => r.id === recId) || RECOMMENDATIONS_DATA[0];
  const d = rec.detail;
  const isApproved = rec.status === 'Approved';

  panel.innerHTML = `
    <div class="rec-detail-panel-card">
      <div class="rec-detail-top-meta">
        <span class="rec-category-badge ${rec.accentColor}" style="font-size:12px;padding:3px 10px;background:#faf5ff;border-radius:12px;">
          ✦ ${d.category}
        </span>
        <span class="rec-priority-pill ${isApproved ? 'opportunity' : rec.priorityClass}" style="font-size:11.5px;padding:4px 10px;">
          ${isApproved ? '✓ APPROVED' : rec.priority.toUpperCase()}
        </span>
      </div>

      <h2 class="rec-detail-title">${d.title}</h2>

      <!-- Why We Recommend This -->
      <div class="rec-detail-box-highlight">
        <div class="rec-detail-box-title">Why NetGravity Recommends This Action</div>
        <p class="rec-detail-box-text">${d.why}</p>
      </div>

      <!-- Expected Impact 4-Box Grid -->
      <div>
        <div class="rec-section-subtitle">Expected Business & Operational Impact</div>
        <div class="rec-impact-grid">
          ${d.impactGrid.map(item => `
            <div class="rec-impact-card ${item.color === 'purple' ? 'purple' : ''}">
              <div class="rec-impact-card-label">${item.label}</div>
              <div class="rec-impact-card-val">${item.val}</div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- Root Cause & Network Telemetry -->
      <div>
        <div class="rec-section-subtitle">Root Cause & Verified Network Telemetry</div>
        <div class="rec-telemetry-list">
          ${d.rootCause.map(rc => `
            <div class="rec-telemetry-row">
              <span class="rec-telemetry-label">${rc.label}</span>
              <div class="rec-telemetry-val-group">
                <span class="rec-telemetry-value">${rc.value}</span>
                <span class="provenance-badge ${rc.provenance.toLowerCase().replace(/ /g, '-')}" style="font-size:10px;padding:2px 6px;border-radius:4px;background:#f3f4f6;color:#6b7280;">
                  ${rc.provenance}
                </span>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- Evaluated Alternatives -->
      <div>
        <div class="rec-section-subtitle">Evaluated Alternatives (Rejected Trade-offs)</div>
        <div class="rec-rejected-list">
          ${d.rejectedAlternatives.map(alt => `
            <div class="rec-rejected-item">
              <div class="rec-rejected-name">✕ ${alt.name}</div>
              <div class="rec-rejected-reason">${alt.reason}</div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- Implementation Next Steps -->
      <div>
        <div class="rec-section-subtitle">Implementation Next Steps & Designated Owners</div>
        <div class="rec-steps-list">
          ${d.executionSteps.map(s => `
            <div class="rec-step-item">
              <div class="rec-step-badge">${s.step}</div>
              <div class="rec-step-content">
                <div class="rec-step-action">${s.action}</div>
                <div class="rec-step-owner">Owner: ${s.owner}</div>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- Action Buttons & CTAs -->
      <div class="rec-action-buttons-group">
        <button class="rec-btn-approve" id="btn-rec-approve" ${isApproved ? 'disabled style="opacity:0.7"' : ''}>
          ${isApproved ? '✓ Approved' : '✓ Approve Decision'}
        </button>
        <button class="rec-btn-simulate" id="btn-rec-simulate">
          Simulate in Scenario Planner →
        </button>
        <button class="rec-btn-email" id="btn-rec-email">
          ✉ Generate Analyst Brief
        </button>
        <button class="rec-btn-reject" id="btn-rec-reject">
          ✕ Defer / Reject
        </button>
      </div>

      <!-- Email Preview Drawer -->
      <div class="rec-email-preview-box" id="rec-email-preview">${d.analystEmail}</div>
    </div>
  `;

  // Bind CTA listeners
  document.getElementById('btn-rec-approve')?.addEventListener('click', () => {
    rec.status = 'Approved';
    showToastNotification('✓ Recommendation Approved! SAP routing change request generated.');
    renderRecsSummaryGrid();
    renderRecsFeed();
    renderRecDetail(recId);
  });

  document.getElementById('btn-rec-simulate')?.addEventListener('click', () => {
    if (window.navigateToTab) window.navigateToTab('scenarios');
  });

  document.getElementById('btn-rec-email')?.addEventListener('click', () => {
    const preview = document.getElementById('rec-email-preview');
    if (preview) preview.classList.toggle('visible');
  });

  document.getElementById('btn-rec-reject')?.addEventListener('click', () => {
    rec.status = 'Deferred';
    showToastNotification('Recommendation marked as Deferred.');
    renderRecsFeed();
    renderRecDetail(recId);
  });
}

/**
 * Bind Filter bar listeners
 */
function bindRecFilterEvents() {
  const pills = document.querySelectorAll('.recs-filter-pill');
  pills.forEach(pill => {
    pill.addEventListener('click', () => {
      pills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      activeRecFilter = pill.getAttribute('data-filter') || 'All';
      renderRecsFeed();
    });
  });

  const prioritySelect = document.getElementById('recs-filter-priority-select');
  if (prioritySelect) {
    prioritySelect.addEventListener('change', (e) => {
      activeRecPriority = e.target.value;
      renderRecsFeed();
    });
  }
}

/**
 * Toast Notification
 */
function showToastNotification(msg) {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
    background: #111827; color: #ffffff; padding: 12px 24px;
    border-radius: 24px; font-size: 13.5px; font-weight: 500;
    box-shadow: 0 10px 30px rgba(0,0,0,0.25); z-index: 100002;
    animation: fadeIn .25s ease;
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity .3s';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// Expose globally on window
if (typeof window !== 'undefined') {
  window.initRecommendationsPage = initRecommendationsPage;
  window.renderRecDetail = renderRecDetail;
}
