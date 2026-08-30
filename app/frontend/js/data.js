/**
 * NetGravity — Central Mock Data Layer
 * ======================================
 * All prototype data in one place. Internally consistent.
 * Storyline: December capacity risk at Baddi DC.
 *
 * STATUS: PROTOTYPE / MOCKED
 * In production, this data comes from the Python MILP engine via API.
 */

// ─── FACILITIES ─────────────────────────────────────────────
export const PLANTS = [
  { id: "PLT_BADDI", name: "Baddi Plant", city: "Baddi", state: "Himachal Pradesh", lat: 30.96, lng: 76.79, capacity: 12000, throughput: 11200, region: "North", status: "EXISTING" },
  { id: "PLT_PUNE", name: "Pune Plant", city: "Pune", state: "Maharashtra", lat: 18.52, lng: 73.86, capacity: 10000, throughput: 7800, region: "West", status: "EXISTING" },
  { id: "PLT_HYDERABAD", name: "Hyderabad Plant", city: "Hyderabad", state: "Telangana", lat: 17.38, lng: 78.49, capacity: 8000, throughput: 6100, region: "South", status: "EXISTING" },
  { id: "PLT_KOLKATA", name: "Kolkata Plant", city: "Kolkata", state: "West Bengal", lat: 22.57, lng: 88.36, capacity: 6000, throughput: 4200, region: "East", status: "EXISTING" },
];

export const DCS = [
  { id: "DC_DELHI", name: "Delhi NCR DC", city: "Delhi NCR", state: "Delhi", lat: 28.61, lng: 77.21, capacity: 10000, throughput: 9400, fixedCost: 120, handlingCost: 4.2, region: "North", status: "EXISTING", utilPct: 94.0 },
  { id: "DC_MUMBAI", name: "Mumbai DC", city: "Mumbai", state: "Maharashtra", lat: 19.08, lng: 72.88, capacity: 9000, throughput: 6800, fixedCost: 140, handlingCost: 4.8, region: "West", status: "EXISTING", utilPct: 75.6 },
  { id: "DC_BENGALURU", name: "Bengaluru DC", city: "Bengaluru", state: "Karnataka", lat: 12.97, lng: 77.59, capacity: 7500, throughput: 5600, fixedCost: 110, handlingCost: 4.0, region: "South", status: "EXISTING", utilPct: 74.7 },
  { id: "DC_KOLKATA", name: "Kolkata DC", city: "Kolkata", state: "West Bengal", lat: 22.57, lng: 88.36, capacity: 6000, throughput: 3200, fixedCost: 85, handlingCost: 3.5, region: "East", status: "EXISTING", utilPct: 53.3 },
  { id: "DC_GUWAHATI", name: "Guwahati DC", city: "Guwahati", state: "Assam", lat: 26.14, lng: 91.74, capacity: 4000, throughput: 2100, fixedCost: 65, handlingCost: 3.8, region: "Northeast", status: "EXISTING", utilPct: 52.5 },
];

export const MARKETS = [
  { id: "MKT_DELHI", name: "Delhi", lat: 28.70, lng: 77.10, demand: 4200, slaDays: 2, priority: "High", region: "North" },
  { id: "MKT_MUMBAI", name: "Mumbai", lat: 19.08, lng: 72.88, demand: 3800, slaDays: 2, priority: "High", region: "West" },
  { id: "MKT_BENGALURU", name: "Bengaluru", lat: 12.97, lng: 77.59, demand: 3200, slaDays: 2, priority: "High", region: "South" },
  { id: "MKT_CHENNAI", name: "Chennai", lat: 13.08, lng: 80.27, demand: 2400, slaDays: 3, priority: "Medium", region: "South" },
  { id: "MKT_HYDERABAD", name: "Hyderabad", lat: 17.38, lng: 78.49, demand: 2800, slaDays: 3, priority: "Medium", region: "South" },
  { id: "MKT_KOLKATA", name: "Kolkata", lat: 22.57, lng: 88.36, demand: 2200, slaDays: 3, priority: "Medium", region: "East" },
  { id: "MKT_AHMEDABAD", name: "Ahmedabad", lat: 23.03, lng: 72.57, demand: 1800, slaDays: 3, priority: "Medium", region: "West" },
  { id: "MKT_JAIPUR", name: "Jaipur", lat: 26.91, lng: 75.79, demand: 1500, slaDays: 3, priority: "Medium", region: "North" },
  { id: "MKT_LUCKNOW", name: "Lucknow", lat: 26.85, lng: 80.95, demand: 1400, slaDays: 3, priority: "Medium", region: "North" },
  { id: "MKT_GUWAHATI", name: "Guwahati", lat: 26.14, lng: 91.74, demand: 1100, slaDays: 4, priority: "Low", region: "Northeast" },
];

// ─── S2: de-overlap co-located nodes ──────────────────────────
// Several plants/DCs/markets share a city and were given the exact same
// lat/lng (e.g. Kolkata Plant, Kolkata DC and the Kolkata market all sit at
// 22.57,88.36), which made them render as fully stacked, indistinguishable
// icons on both the 2D Leaflet map and the 3D twin — neither has any
// de-collision logic of its own. Fixed once here, at the shared data
// source, so every consumer (map.js, twin3d.js, lane endpoints, FACILITIES)
// sees already-separated coordinates. This is a schematic nudge for
// legibility, not a claim about real inter-facility distance.
function deoverlapNodes(nodeArrays) {
  const groups = new Map();
  nodeArrays.flat().forEach((n) => {
    const key = `${n.lat.toFixed(4)},${n.lng.toFixed(4)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  });
  const FAN_RADIUS = 0.9; // degrees — visibly separates icons on the India-wide view
  groups.forEach((nodes) => {
    if (nodes.length < 2) return;
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / nodes.length;
      n.lat += FAN_RADIUS * Math.sin(angle);
      n.lng += FAN_RADIUS * Math.cos(angle);
    });
  });
}
deoverlapNodes([PLANTS, DCS, MARKETS]);

export const FACILITIES = [
  ...PLANTS.map((p) => ({ ...p, type: 'Plant' })),
  ...DCS.map((d) => ({ ...d, type: 'DC' })),
];

// ─── LANES (key corridors with cost, distance, lead time) ───
export const LANES = [
  // Plant → DC
  { from: "PLT_BADDI", to: "DC_DELHI", cost: 12, distance: 310, leadTime: 1.0, flow: 8200, mode: "ROAD" },
  { from: "PLT_BADDI", to: "DC_MUMBAI", cost: 28, distance: 1420, leadTime: 2.5, flow: 1800, mode: "ROAD" },
  { from: "PLT_BADDI", to: "DC_KOLKATA", cost: 32, distance: 1800, leadTime: 3.0, flow: 1200, mode: "ROAD" },
  { from: "PLT_PUNE", to: "DC_MUMBAI", cost: 8, distance: 150, leadTime: 0.5, flow: 5000, mode: "ROAD" },
  { from: "PLT_PUNE", to: "DC_BENGALURU", cost: 18, distance: 840, leadTime: 1.5, flow: 2800, mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_BENGALURU", cost: 15, distance: 570, leadTime: 1.0, flow: 2800, mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_MUMBAI", cost: 20, distance: 710, leadTime: 1.5, flow: 1500, mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_KOLKATA", cost: 25, distance: 1500, leadTime: 2.5, flow: 1300, mode: "ROAD" },
  { from: "PLT_KOLKATA", to: "DC_KOLKATA", cost: 5, distance: 30, leadTime: 0.2, flow: 2700, mode: "ROAD" },
  { from: "PLT_KOLKATA", to: "DC_GUWAHATI", cost: 22, distance: 1000, leadTime: 2.0, flow: 1500, mode: "ROAD" },
  { from: "PLT_BADDI", to: "DC_GUWAHATI", cost: 38, distance: 2200, leadTime: 3.5, flow: 600, mode: "ROAD" },
  // DC → Market (major flows)
  { from: "DC_DELHI", to: "MKT_DELHI", cost: 4, distance: 40, leadTime: 0.3, flow: 4200, mode: "ROAD" },
  { from: "DC_DELHI", to: "MKT_JAIPUR", cost: 10, distance: 270, leadTime: 0.8, flow: 1500, mode: "ROAD" },
  { from: "DC_DELHI", to: "MKT_LUCKNOW", cost: 14, distance: 500, leadTime: 1.2, flow: 1400, mode: "ROAD" },
  { from: "DC_DELHI", to: "MKT_AHMEDABAD", cost: 22, distance: 950, leadTime: 1.8, flow: 800, mode: "ROAD" },
  { from: "DC_MUMBAI", to: "MKT_MUMBAI", cost: 3, distance: 25, leadTime: 0.2, flow: 3800, mode: "ROAD" },
  { from: "DC_MUMBAI", to: "MKT_AHMEDABAD", cost: 12, distance: 530, leadTime: 1.0, flow: 1000, mode: "ROAD" },
  { from: "DC_MUMBAI", to: "MKT_HYDERABAD", cost: 18, distance: 710, leadTime: 1.5, flow: 1200, mode: "ROAD" },
  { from: "DC_BENGALURU", to: "MKT_BENGALURU", cost: 3, distance: 20, leadTime: 0.2, flow: 3200, mode: "ROAD" },
  { from: "DC_BENGALURU", to: "MKT_CHENNAI", cost: 10, distance: 350, leadTime: 0.8, flow: 2400, mode: "ROAD" },
  { from: "DC_KOLKATA", to: "MKT_KOLKATA", cost: 3, distance: 15, leadTime: 0.2, flow: 2200, mode: "ROAD" },
  { from: "DC_KOLKATA", to: "MKT_HYDERABAD", cost: 22, distance: 1500, leadTime: 2.5, flow: 600, mode: "ROAD" },
  { from: "DC_GUWAHATI", to: "MKT_GUWAHATI", cost: 4, distance: 30, leadTime: 0.3, flow: 1100, mode: "ROAD" },
  { from: "DC_DELHI", to: "MKT_HYDERABAD", cost: 28, distance: 1500, leadTime: 2.5, flow: 500, mode: "ROAD" },
  { from: "DC_GUWAHATI", to: "MKT_KOLKATA", cost: 18, distance: 1000, leadTime: 2.0, flow: 600, mode: "ROAD" },
  { from: "DC_MUMBAI", to: "MKT_CHENNAI", cost: 24, distance: 1330, leadTime: 2.2, flow: 500, mode: "ROAD" },
];

// ─── DEMAND HISTORY (24 months) & FORECAST ──────────────────
export const DEMAND_HISTORY = {
  months: [
    "Jan'25", "Feb'25", "Mar'25", "Apr'25", "May'25", "Jun'25",
    "Jul'25", "Aug'25", "Sep'25", "Oct'25", "Nov'25", "Dec'25",
    "Jan'26", "Feb'26", "Mar'26", "Apr'26", "May'26", "Jun'26",
    "Jul'26", "Aug'26", "Sep'26", "Oct'26", "Nov'26", "Dec'26",
  ],
  // North India aggregate demand (units/day)
  northIndia: [
    7100, 7000, 7300, 7200, 7400, 7100,
    7500, 7600, 7800, 8000, 8200, 8800,
    8400, 8300, 8600, 8500, 8800, 8600,
    9000, 9200, 9400, 9600, 9800, 10800,
  ],
  // Baddi DC capacity line (constant)
  baddiCapacity: 10000,
};

export const FORECAST = {
  months: ["Jan'27", "Feb'27", "Mar'27", "Apr'27", "May'27", "Jun'27"],
  northIndia: [10200, 10400, 10100, 10600, 10900, 11200],
  upper: [10800, 11200, 10900, 11500, 11800, 12400],
  lower: [9600, 9700, 9400, 9800, 10000, 10100],
  growthRate: 14.2,
  breachMonth: "Dec'26",
  breachFacility: "DC_DELHI",
  breachProjectedUtil: 108,
};

// ─── EXTERNAL SIGNALS ───────────────────────────────────────
export const EXTERNAL_SIGNALS = [
  {
    id: "SIG_01",
    title: "North India GDP Growth Accelerating",
    source: "RBI Quarterly Bulletin",
    publishedDate: "2026-07-15",
    effectiveDate: "2026-Q4",
    geography: "North India",
    direction: "UP",
    magnitude: "+2.1% above trend",
    confidence: "HIGH",
    rationale: "Strong industrial output and consumer spending in NCR, Punjab, Haryana",
    intendedUse: "Demand forecast enrichment — supports +14% North India demand growth estimate",
    type: "signal",
    icon: "📈",
    color: "#dc2626",
  },
  {
    id: "SIG_02",
    title: "Diesel Prices Expected to Rise 8%",
    source: "PPAC / Ministry of Petroleum",
    publishedDate: "2026-08-01",
    effectiveDate: "2026-10-01",
    geography: "Pan-India",
    direction: "UP",
    magnitude: "₹8–10/litre increase",
    confidence: "MEDIUM",
    rationale: "Global crude price increase + reduced subsidies expected in Q4",
    intendedUse: "Transport cost sensitivity — scenario with +8% lane costs",
    type: "signal",
    icon: "⛽",
    color: "#f59e0b",
  },
  {
    id: "SIG_03",
    title: "New Expressway: Delhi–Jaipur Corridor",
    source: "NHAI Press Release",
    publishedDate: "2026-06-20",
    effectiveDate: "2027-03-01",
    geography: "Delhi → Jaipur",
    direction: "DOWN",
    magnitude: "−15% transit time, −5% fuel cost",
    confidence: "HIGH",
    rationale: "Six-lane expressway reduces travel time from 5h to 3.5h",
    intendedUse: "Lane cost/time update for Delhi–Jaipur corridor in future periods",
    type: "signal",
    icon: "🛣️",
    color: "#22c55e",
  },
];

// ─── DATA QUALITY ───────────────────────────────────────────
export const DATA_QUALITY = {
  totalRecords: 4820,
  validRecords: 4743,
  validPct: 98.4,
  issues: [
    { id: "DQ_01", type: "Unit Inconsistency", field: "capacity", facility: "DC_GUWAHATI", detail: "Capacity reported in tonnes, expected units/day", severity: "warning", status: "needs_review" },
    { id: "DQ_02", type: "Unit Inconsistency", field: "demand", market: "MKT_LUCKNOW", detail: "Q3 demand in cases, rest in units", severity: "warning", status: "needs_review" },
    { id: "DQ_03", type: "Missing Value", field: "capacity", facility: "DC_GUWAHATI", detail: "Max capacity not specified for expansion scenario", severity: "info", status: "needs_review" },
    { id: "DQ_04", type: "AI Mapping Uncertain", field: "Qty → Demand", source: "Distributor A", detail: "Field 'Qty' mapped to Demand_Units (87% confidence)", severity: "warning", status: "needs_review" },
    { id: "DQ_05", type: "AI Mapping Uncertain", field: "Vol → Demand", source: "Distributor B", detail: "Field 'Volume' mapped to Demand_Units (91% confidence)", severity: "info", status: "needs_review" },
    { id: "DQ_06", type: "AI Mapping Uncertain", field: "Units Shipped", source: "Distributor C", detail: "Field 'Units Shipped' mapped to Demand_Units (95% confidence)", severity: "info", status: "auto_mapped" },
    { id: "DQ_07", type: "Outlier", field: "transport_cost", lane: "PLT_BADDI→DC_GUWAHATI", detail: "Cost ₹38/unit is 2.1σ above corridor average", severity: "info", status: "reviewed" },
    { id: "DQ_08", type: "Stale Data", field: "demand", market: "MKT_GUWAHATI", detail: "Last updated 45 days ago", severity: "info", status: "needs_review" },
  ],
  reviewCount: 12,
  uncertainMappings: 3,
  missingCapacity: 1,
};

// ─── CONTRACT EXTRACTION DEMO ───────────────────────────────
export const CONTRACT_DEMO = {
  vendorA: {
    name: "TransCorp Logistics",
    headlineRate: "₹10/kg",
    extractedTerms: [
      { field: "Base Rate", value: "₹10/kg", confidence: "HIGH" },
      { field: "Fuel Surcharge", value: "₹2/kg", confidence: "HIGH" },
      { field: "Non-Serviceable Surcharge", value: "₹5/kg for 12 pin codes in NE India", confidence: "MEDIUM" },
      { field: "Minimum Volume", value: "500 kg/shipment", confidence: "HIGH" },
      { field: "Penalty (Late Pickup)", value: "₹500/incident", confidence: "MEDIUM" },
      { field: "Effective Date", value: "2026-04-01", confidence: "HIGH" },
    ],
    effectiveCost: "₹12–17/kg (depending on route)",
    hiddenCostAlert: true,
  },
  vendorB: {
    name: "SpeedFreight India",
    headlineRate: "₹12/kg",
    extractedTerms: [
      { field: "Base Rate", value: "₹12/kg (all-inclusive)", confidence: "HIGH" },
      { field: "Fuel Surcharge", value: "Included", confidence: "HIGH" },
      { field: "Coverage", value: "All pin codes", confidence: "HIGH" },
      { field: "Minimum Volume", value: "200 kg/shipment", confidence: "HIGH" },
      { field: "Effective Date", value: "2026-04-01", confidence: "HIGH" },
    ],
    effectiveCost: "₹12/kg (flat)",
    hiddenCostAlert: false,
  },
};

// ─── SCHEMA MAPPING DEMO ────────────────────────────────────
export const SCHEMA_MAPPING = {
  distributors: [
    { name: "Distributor A", field: "Qty", mappedTo: "Demand_Units", confidence: 87 },
    { name: "Distributor B", field: "Volume", mappedTo: "Demand_Units", confidence: 91 },
    { name: "Distributor C", field: "Units Shipped", mappedTo: "Demand_Units", confidence: 95 },
    { name: "Distributor D", field: "Order_Qty", mappedTo: "Demand_Units", confidence: 98 },
  ],
  canonicalField: "Demand_Units",
  sourceTypes: ["ERP", "WMS", "TMS", "Facility Master", "Demand Data", "Lane Data"],
};

// ─── SCENARIO RESULTS (Canonical MILP outputs for Decision Cockpit) ──────
export const SCENARIOS = [
  {
    id: "SCN_ACTUAL",
    num: 0,
    name: "Current Baseline",
    shortName: "Current Baseline",
    cardTitle: "Baseline",
    type: "BASELINE",
    source: "system",
    badge: "Current Baseline",
    badgeClass: "tag-muted",
    status: "Baseline",
    description: "Current network configuration and observed flows",
    highlight: "Observed network operations without optimization.",
    totalCost: 1285000,
    costChange: 0,
    transportCost: 1325000,
    fixedCost: 312000,
    variableCost: 245000,
    inventoryCost: 81000,
    inventoryDays: 17,
    sla: 94.3,
    avgUtil: 72.4,
    maxUtil: 94.0,
    delhiUtil: 94.0,
    capacityRisk: "High",
    capacityRiskClass: "red",
    carbonKg: 105688,
    implementationCost: 0,
    implementationTime: "10 mins",
    confidence: "—",
    stars: 0,
    robustness: "Baseline",
    feasible: true,
    objective: {
      goal: "Current observed operations",
      primaryMetric: "Cost & SLA as-is",
      constraint: "None (Observed Actuals)",
    },
    changes: [
      { item: "Current Footprint", change: "4 Plants, 5 DCs, 10 Markets", note: "No changes applied" },
    ],
    assumptions: [
      { label: "Demand Horizon", value: "Historical Observed", type: "MODEL FACT" },
      { label: "Network State", value: "Actual Observed Footprint", type: "MODEL FACT" },
    ],
    optimisation: {
      objective: "Historical Baseline",
      lockedDecisions: "All allocations locked as observed",
      allowedDecisions: "None",
      slaConstraint: "Observed at 94.3%",
    },
    robustnessTests: [
      { test: "Historical Baseline", status: "PASS", detail: "Reflects observed operations" },
    ],
    aiAssessment: {
      recommendation: "Baseline network is vulnerable to the upcoming December demand surge in North India.",
      why: ["Delhi NCR DC is currently operating at 94% utilisation", "December forecast projects capacity breach to 108%"],
      whatIRejected: "N/A — This is the baseline starting point.",
    },
  },
  {
    id: "SCN_REBALANCE",
    num: 1,
    name: "Scenario 1 (Rec.)",
    shortName: "Recommended Scenario 1 (Rec.)",
    cardTitle: "Scenario 1 (Rec.)",
    type: "RECOMMENDED",
    source: "agent",
    badge: "Recommended",
    badgeClass: "tag-success",
    status: "Recommended",
    description: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata.",
    highlight: "Best balance of cost, service and capacity risk.",
    totalCost: 1184000,
    costChange: -7.8,
    transportCost: 1216000,
    fixedCost: 312000,
    variableCost: 228000,
    inventoryCost: 54000,
    inventoryDays: 24,
    sla: 96.7,
    avgUtil: 65.3,
    maxUtil: 91.0,
    delhiUtil: 91.0,
    capacityRisk: "Low",
    capacityRiskClass: "green",
    carbonKg: 231737,
    implementationCost: 25000,
    implementationTime: "13 mins",
    confidence: "High Confidence",
    stars: 5,
    robustness: "High",
    feasible: true,
    objective: {
      goal: "Reduce total cost while maintaining SLA ≥95% & mitigating capacity risk",
      primaryMetric: "Total Cost",
      constraint: "SLA ≥ 95%, Peak Utilisation ≤ 92%",
    },
    changes: [
      { item: "Baddi DC → Delhi NCR", change: "-12% Volume (-1,200 u/d)", note: "Relieve northern bottleneck" },
      { item: "Baddi DC → Kolkata", change: "+12% Volume (+800 u/d)", note: "Absorb into spare capacity" },
      { item: "Pune Plant → Mumbai DC", change: "+400 u/d", note: "Direct regional routing" },
    ],
    assumptions: [
      { label: "Demand Forecast", value: "December Peak Forecast (+14.2% North)", type: "FORECAST" },
      { label: "Facility Footprint", value: "Unchanged (Zero CapEx investment)", type: "MODEL FACT" },
      { label: "SLA Minimum Requirement", value: "≥95% on-time delivery", type: "MODEL FACT" },
      { label: "Regional GDP Factor", value: "+2.1% above trend", type: "EXTERNAL SIGNAL" },
    ],
    optimisation: {
      objective: "Minimise total network cost subject to multi-echelon capacity",
      lockedDecisions: "Facility locations & fixed footprint",
      allowedDecisions: "Multi-commodity corridor dispatch volumes",
      slaConstraint: "≥95% for Tier 1 & Tier 2 customer markets",
    },
    robustnessTests: [
      { test: "+15% Demand Surge", status: "PASS", detail: "Network absorbs volume; peak util stays below 95%" },
      { test: "Major Lane Disruption", status: "PASS", detail: "Kolkata & Mumbai alternate corridors absorb emergency overflow" },
      { test: "Handling Rate Elasticity", status: "PASS", detail: "Optimal ranking unchanged under ±10% rate fluctuations" },
    ],
    aiAssessment: {
      recommendation: "I recommend this scenario because it provides the best balance of cost reduction, SLA performance, and capacity protection without requiring capital investment.",
      why: [
        "7.8% lower total network cost (₹11.84L vs ₹12.85L baseline)",
        "SLA reaches 96.7% (exceeds 95.0% target)",
        "Relieves Delhi NCR DC peak utilisation from 94% down to 91%",
        "Passes +15% demand surge stress test without infeasibility",
      ],
      whatIRejected: "The Optimised Base Case (₹11.42L) was rejected because it fails the SLA threshold when December peak demand increases by 15%.",
    },
  },
  {
    id: "SCN_USER_1",
    num: 2,
    name: "Scenario 2 (My Scen 1)",
    shortName: "User Created 2 (My Scen 1)",
    cardTitle: "Scenario 2 (My Scen 1)",
    type: "USER_CREATED",
    source: "user",
    badge: "User Created",
    badgeClass: "tag-primary",
    status: "User Created",
    description: "User customized flow rebalancing with western corridor routing.",
    highlight: "Custom what-if rebalancing scenario.",
    totalCost: 1285000,
    costChange: -7.7,
    transportCost: 1315000,
    fixedCost: 312000,
    variableCost: 241000,
    inventoryCost: 52000,
    inventoryDays: 13,
    sla: 94.3,
    avgUtil: 72.4,
    maxUtil: 94.0,
    delhiUtil: 94.0,
    capacityRisk: "High",
    capacityRiskClass: "red",
    carbonKg: 104499,
    implementationCost: 45000,
    implementationTime: "21 mins",
    confidence: "Medium Confidence",
    stars: 3,
    robustness: "Medium",
    feasible: true,
    objective: {
      goal: "Custom flow optimization focused on Western hub distribution",
      primaryMetric: "Total Cost",
      constraint: "SLA ≥ 94%",
    },
    changes: [
      { item: "Baddi DC → Mumbai DC", change: "+15% Volume (+900 u/d)", note: "Western shift" },
      { item: "Delhi NCR DC", change: "Unchanged allocation", note: "Maintains current flows" },
    ],
    assumptions: [
      { label: "Demand Forecast", value: "December Forecast (+14.2%)", type: "FORECAST" },
      { label: "Western Route Rates", value: "₹18.2/unit discounted tariff", type: "EXTERNAL SIGNAL" },
    ],
    optimisation: {
      objective: "Minimise Corridor Freight Spend",
      lockedDecisions: "Northern DC allocation",
      allowedDecisions: "Western corridor dispatch",
      slaConstraint: "≥94.0%",
    },
    robustnessTests: [
      { test: "+15% Demand Surge", status: "FAIL", detail: "Delhi NCR breaches 100% capacity threshold" },
      { test: "SLA Integrity", status: "FAIL", detail: "SLA is 94.3% (below target ≥95%)" },
    ],
    aiAssessment: {
      recommendation: "Achieves cost improvement but leaves Delhi NCR exposed to December capacity breach.",
      why: [
        "Transport cost reaches ₹13.15L (↓7.7%)",
        "Higher average network utilisation at 72.4%",
      ],
      whatIRejected: "Not recommended because it fails the SLA target of ≥95% and leaves capacity risk High.",
    },
  },
  {
    id: "SCN_USER_2",
    num: 3,
    name: "Scenario 3",
    shortName: "User Created 3 (My Scen 2)",
    cardTitle: "Scenario 3",
    type: "CANDIDATE",
    source: "user",
    badge: "Candidate",
    badgeClass: "tag-warning",
    status: "Candidate",
    description: "Rapid execution capacity redistribution to Kolkata DC.",
    highlight: "Lowest implementation risk and fastest execution.",
    totalCost: 1384000,
    costChange: -7.8,
    transportCost: 1429000,
    fixedCost: 312000,
    variableCost: 260000,
    inventoryCost: 62000,
    inventoryDays: 18,
    sla: 96.7,
    avgUtil: 65.3,
    maxUtil: 90.0,
    delhiUtil: 90.0,
    capacityRisk: "Low",
    capacityRiskClass: "green",
    carbonKg: 97133,
    implementationCost: 20000,
    implementationTime: "10 mins",
    confidence: "High Confidence",
    stars: 4,
    robustness: "High",
    feasible: true,
    objective: {
      goal: "Fastest deployment rebalancing with low operational risk",
      primaryMetric: "Implementation Time",
      constraint: "SLA ≥ 95%",
    },
    changes: [
      { item: "Baddi DC → Kolkata DC", change: "+1,200 u/d shift", note: "Eastern capacity absorption" },
      { item: "Delhi NCR DC", change: "-800 u/d volume reduction", note: "Relieve northern bottleneck" },
    ],
    assumptions: [
      { label: "Implementation Speed", value: "10 mins automated WMS switch", type: "MODEL FACT" },
      { label: "Kolkata Spare Capacity", value: "41% headroom available", type: "MODEL FACT" },
    ],
    optimisation: {
      objective: "Minimise Execution Friction & Implementation Time",
      lockedDecisions: "Western & Southern nodes locked",
      allowedDecisions: "East-North reallocations",
      slaConstraint: "≥95.0%",
    },
    robustnessTests: [
      { test: "+15% Demand Surge", status: "PASS", detail: "Absorbs surge safely; peak util 90%" },
      { test: "SLA Integrity", status: "PASS", detail: "SLA remains solid at 96.7%" },
    ],
    aiAssessment: {
      recommendation: "Scenario 3 has the lowest implementation risk but only marginal cost improvement relative to S1.",
      why: [
        "Fastest implementation timeline: 10 mins",
        "Delivers 96.7% SLA and Low capacity risk",
        "Lowest Scope 3 Carbon: 97,133 kg CO₂",
      ],
      whatIRejected: "Carries higher transport cost (₹14.29L) than Scenario 1 (₹12.16L) due to longer freight routes.",
    },
  },
  {
    id: "SCN_AI_REC_4",
    num: 4,
    name: "Scenario 4 (AI Rec)",
    shortName: "AI Recommended 4 (AI Rec)",
    cardTitle: "Scenario 4 (AI Rec)",
    type: "CANDIDATE",
    source: "agent",
    badge: "Candidate",
    badgeClass: "tag-warning",
    status: "Candidate",
    description: "Consolidated multi-echelon rail corridor routing.",
    highlight: "High SLA and low carbon footprint.",
    totalCost: 1184000,
    costChange: -5.3,
    transportCost: 1835000,
    fixedCost: 312000,
    variableCost: 215000,
    inventoryCost: 45000,
    inventoryDays: 15,
    sla: 96.7,
    avgUtil: 65.3,
    maxUtil: 89.0,
    delhiUtil: 89.0,
    capacityRisk: "Low",
    capacityRiskClass: "green",
    carbonKg: 97777,
    implementationCost: 75000,
    implementationTime: "40 mins",
    confidence: "Medium Confidence",
    stars: 3,
    robustness: "High",
    feasible: true,
    objective: {
      goal: "Multi-echelon distribution consolidation with dedicated rail corridors",
      primaryMetric: "Total Cost & Carbon",
      constraint: "SLA ≥ 95%",
    },
    changes: [
      { item: "Northern Rail Hub", change: "+1,500 u/d intermodal transfer", note: "Shift from road to rail" },
      { item: "Delhi NCR DC Throughput", change: "-1,400 u/d volume reduction", note: "Relieve bottleneck" },
    ],
    assumptions: [
      { label: "Rail Freight Schedule", value: "Fixed 36-hr transit window", type: "MODEL FACT" },
      { label: "Implementation Ramp-up", value: "40 mins setup in TMS", type: "MODEL FACT" },
    ],
    optimisation: {
      objective: "Minimise Operating Cost + Scope 3 Carbon",
      lockedDecisions: "Plant production volumes",
      allowedDecisions: "Intermodal corridor selections",
      slaConstraint: "≥95.0%",
    },
    robustnessTests: [
      { test: "+15% Demand Surge", status: "PASS", detail: "Intermodal rail capacity absorbs surge" },
      { test: "SLA Integrity", status: "PASS", detail: "SLA maintained at 96.7%" },
    ],
    aiAssessment: {
      recommendation: "Strong environmental performance with Low capacity risk, but carries higher freight handling costs.",
      why: [
        "SLA reaches 96.7%",
        "Low capacity risk on Delhi NCR DC",
        "Scope 3 Carbon: 97,777 kg CO₂",
      ],
      whatIRejected: "Higher freight cost (₹18.35L) and longer implementation timeline (40 mins) than Scenario 1.",
    },
  },
];

// ─── SCENARIO COMPARISON INSIGHTS (Deterministic AI Assessments) ─
export const SCENARIO_COMPARISON_INSIGHTS = [
  {
    id: "SCI_1",
    num: 1,
    text: "S3 has the lowest implementation risk but marginal cost improvement.",
    evidence: "Implementation time: 10 mins (lowest in network); cost change: -7.8% (₹13.84L vs ₹12.85L baseline).",
    scenarioId: "SCN_USER_2",
  },
  {
    id: "SCI_2",
    num: 2,
    text: "S1 balances cost and SLA best, but S2 is cheaper.",
    evidence: "S1 delivers ₹11.84L total cost with 96.7% SLA and Low capacity risk; passes +15% demand surge.",
    scenarioId: "SCN_REBALANCE",
  },
  {
    id: "SCI_3",
    num: 3,
    text: "S2 increases average network utilization significantly.",
    evidence: "Average utilization reaches 72.4% with High capacity risk remaining on Delhi NCR.",
    scenarioId: "SCN_USER_1",
  },
  {
    id: "SCI_4",
    num: 4,
    text: "All robust check against 15% stress test.",
    evidence: "S1, S3, and S4 pass +15% demand surge test without infeasibility.",
    scenarioId: "SCN_REBALANCE",
  },
  {
    id: "SCI_5",
    num: 5,
    text: "All meet target of >=95% SLA except S2.",
    evidence: "S1 (96.7%), S3 (96.7%), S4 (96.7%) exceed SLA ≥95%; S2 (94.3%) breaches target.",
    scenarioId: "SCN_USER_1",
  },
];

// ─── MULTI-SCENARIO ACTION ITEMS ────────────────────────────
export const SCENARIO_COMPARISON_ACTIONS = [
  {
    id: "SCA_1",
    title: "Validate cost assumptions for Scenario 2.",
    why: "Scenario 2 achieves ₹13.15L transport cost but assumes discounted line-haul rates on western corridors.",
    scenarioId: "SCN_USER_1",
    evidence: "Baddi→Mumbai route rate ₹18.2/unit vs contract benchmark ₹19.5/unit.",
    nextStep: "Confirm rate card with SpeedFreight India.",
  },
  {
    id: "SCA_2",
    title: "Conduct deep dive into S3 implementation timeline.",
    why: "Scenario 3 estimates 10-minute automated dispatch rollout but requires WMS slotting reconfiguration.",
    scenarioId: "SCN_USER_2",
    evidence: "Kolkata DC throughput increases by +800 units/day.",
    nextStep: "Review warehouse floor capacity in Kolkata.",
  },
  {
    id: "SCA_3",
    title: "Review stakeholder alignment for Recommended Scenario (S1).",
    why: "Scenario 1 shifts 12% Baddi volume to Kolkata DC to avoid Delhi NCR bottleneck before December.",
    scenarioId: "SCN_REBALANCE",
    evidence: "Reduces total network cost by 7.8% (₹8.4L/mo) with 96.7% SLA.",
    nextStep: "Proceed to operational execution sign-off.",
  },
  {
    id: "SCA_4",
    title: "Automate network stress testing.",
    why: "Ensure real-time telemetry from S/4HANA automatically triggers rebalancing alerts if demand exceeds +12%.",
    scenarioId: "SCN_REBALANCE",
    evidence: "Peak demand buffer is 1,200 units/day.",
    nextStep: "Enable automated webhook alerts in TMS.",
  },
  {
    id: "SCA_5",
    title: "Set up KPI alerts for selected implementation.",
    why: "Track daily capacity utilization and on-time delivery across North & East corridors during ramp-up.",
    scenarioId: "SCN_REBALANCE",
    evidence: "Delhi NCR utilization threshold: 92%; Target SLA: 95.0%.",
    nextStep: "Configure threshold monitoring in KPI Dashboard.",
  },
];


// ─── AI AGENT STATE ─────────────────────────────────────────
export const AGENT_STATE = {
  status: "active",
  currentObjective: "Reduce network cost by at least 8% while maintaining SLA above 95%",
  activityTrace: [
    { step: 1, action: "Analysed the current network", status: "done", detail: "42 facilities, 120 demand zones, 380 lanes" },
    { step: 2, action: "Identified 2 capacity bottlenecks", status: "done", detail: "Delhi NCR DC at 94% utilisation, projected 108% in December" },
    { step: 3, action: "Identified 1 high-cost corridor", status: "done", detail: "Baddi → Guwahati corridor at ₹38/unit (2.1σ above average)" },
    { step: 4, action: "Generated 4 candidate interventions", status: "done", detail: "Flow rebalancing, capacity expansion, DC consolidation, new DC" },
    { step: 5, action: "Tested 4 scenarios through MILP optimiser", status: "done", detail: "All 4 scenarios evaluated — 3 feasible, 1 fails SLA target" },
    { step: 6, action: "Rejected DC Consolidation on SLA grounds", status: "done", detail: "SLA drops to 88.4% — below 95% threshold" },
    { step: 7, action: "Stress-tested leading options under +15% demand", status: "done", detail: "Flow Rebalancing passes all stress tests" },
    { step: 8, action: "Found the most robust configuration", status: "done", detail: "Flow Rebalancing: −7.8% cost, 96.7% SLA, robust under stress" },
  ],
  toolCalls: [
    { tool: "get_network_summary()", result: "42 facilities, 380 lanes, total demand 24,400 units/day" },
    { tool: "get_bottlenecks()", result: "DC_DELHI at 94% util, projected breach Dec'26" },
    { tool: "get_forecast(region='North')", result: "+14% demand growth, breach at 10,800 units in Dec" },
    { tool: "run_baseline()", result: "Total cost ₹12.84L/day, SLA 91.2%" },
    { tool: "optimize_current_footprint()", result: "Optimised to ₹11.98L/day, SLA 94.8%" },
    { tool: "run_scenario('rebalance')", result: "₹11.84L/day, SLA 96.7% — FEASIBLE" },
    { tool: "run_scenario('expand')", result: "₹12.46L/day, SLA 97.1% — FEASIBLE (high CapEx)" },
    { tool: "run_scenario('consolidate')", result: "₹11.42L/day, SLA 88.4% — REJECTED (SLA < 95%)" },
    { tool: "run_scenario('new_dc')", result: "₹12.15L/day, SLA 97.8% — FEASIBLE (high CapEx)" },
    { tool: "run_sensitivity('demand', +15%)", result: "Rebalance: PASS | Consolidate: FAIL" },
    { tool: "run_resilience('DC_DELHI')", result: "Rebalance handles facility disruption — reroutes to Kolkata" },
    { tool: "compare_scenarios()", result: "Rebalance is optimal on cost-adjusted-risk basis" },
  ],
};

// ─── RECOMMENDATION ─────────────────────────────────────────
export const RECOMMENDATION = {
  title: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata DCs",
  scenarioId: "SCN_REBALANCE",
  tier: 2, // PROPOSE — human approval required
  impact: {
    costChange: -7.8,
    sla: 96.7,
    peakUtilChange: -11.9,
    carbonChange: -4.2,
  },
  evidence: {
    whatIFound: "Delhi NCR DC is operating at 94% utilisation. With projected demand growth of +14% in North India, this facility will exceed capacity (108% projected) by December 2026.",
    whyItMatters: "Capacity breach at Delhi NCR will force unplanned spillover to costlier corridors, degrade SLA from 91.2% to an estimated 84%, and increase transport costs by ₹1.2L/day.",
    whatITested: [
      "Flow Rebalancing — redirect 1,200 units/day to Delhi NCR, 800 to Kolkata",
      "Capacity Expansion — expand Delhi NCR DC by 3,000 units/day (₹18L CapEx)",
      "DC Consolidation — close Guwahati, consolidate through Kolkata",
      "New DC — open Lucknow DC (₹25L CapEx)",
    ],
    whatIRejected: [
      { scenario: "DC Consolidation", reason: "Lowest cost (₹11.42L/day) but SLA drops to 88.4% — below 95% threshold. Also fails under +15% demand surge (Kolkata DC hits 112% utilisation)." },
      { scenario: "Capacity Expansion", reason: "Good SLA (97.1%) but high CapEx (₹18L) and ROI is sensitive to demand materialising as forecast." },
      { scenario: "New Lucknow DC", reason: "Best SLA (97.8%) and resilience but requires ₹25L CapEx and 6-month setup time. Consider for Phase 2." },
    ],
    whatCouldChange: [
      "If demand growth exceeds +20%, capacity expansion becomes necessary",
      "If diesel prices rise >10%, rebalancing savings increase further",
      "If Kolkata DC throughput proves unreliable, New Lucknow DC should be accelerated",
    ],
  },
  nextSteps: [
    { step: 1, action: "Confirm Delhi NCR DC can absorb 1,200 additional units/day", owner: "DC Operations" },
    { step: 2, action: "Validate carrier availability for new Kolkata corridors", owner: "Procurement" },
    { step: 3, action: "Confirm transition lead time (estimated 3–4 weeks)", owner: "Network Planning" },
    { step: 4, action: "Notify Baddi plant of revised dispatch schedule", owner: "Production Planning" },
  ],
  analystEmail: `Subject: Network Rebalancing Recommendation — Delhi NCR & Kolkata DCs

Dear Network Planning Team,

Following our analysis of the current logistics network, NetGravity has identified a capacity risk at Delhi NCR DC (94% utilisation, projected to exceed capacity by December 2026).

Recommended Action:
Rebalance 12% of Baddi-routed volume:
• Redirect 1,200 units/day to Delhi NCR DC
• Increase Kolkata DC throughput by 800 units/day

Expected Impact:
• Cost reduction: −7.8% (₹100K/day savings)
• SLA improvement: 91.2% → 96.7%
• Peak utilisation: 94% → 82.1%
• Carbon reduction: −4.2%

This recommendation has been stress-tested against +15% demand surge, lane disruptions, and transport cost sensitivity. It is the most robust option among 4 alternatives evaluated.

Next Steps:
1. Confirm Delhi NCR DC absorption capacity
2. Validate carrier availability for Kolkata corridors
3. Confirm transition timeline (est. 3–4 weeks)

Please review and confirm by [date].

Best regards,
NetGravity Decision Intelligence`,
};

// ─── GOVERNANCE TIERS ───────────────────────────────────────
export const GOVERNANCE_TIERS = [
  { tier: 1, label: "INFORM", description: "Low-risk informational insight. No approval required.", criteria: "Value at stake < ₹5L, fully reversible, no SLA impact", color: "#22c55e" },
  { tier: 2, label: "PROPOSE", description: "AI recommends and prepares action. Human approval required.", criteria: "Value at stake ₹5L–₹50L, or SLA impact, or partial reversibility", color: "#f59e0b" },
  { tier: 3, label: "HUMAN DECISION", description: "High-impact structural decision. AI analyses, cannot execute.", criteria: "Close/open DC, major contract change, CapEx > ₹50L", color: "#dc2626" },
];

// ─── ACTION AGENT: NOTIFICATION RECIPIENTS (mocked — see js/ingestion.js
// and js/insight-detail.js headers) ─────────────────────────
// Standing list for recommendation/investigate emails (Action Agent
// triggers 3/4). Seeded with the same two dev/test addresses the backend
// seeds its own store with (NETGRAVITY_DEFAULT_RECIPIENT_EMAIL /
// NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL) — fully editable here, not a
// permanent pair. This mock array has no connection to that backend store;
// see the STATUS notes in ingestion.js/insight-detail.js/app.js.
export const NOTIFICATION_RECIPIENTS = [
  { label: "Me", email: "aayush.t115@gmail.com" },
  { label: "Client (test)", email: "dummy.t115@gmail.com" },
];

// Per-source contact for missing-data emails (Action Agent triggers 1/2/5),
// keyed by source id. Empty until a reviewer sets one in the ingestion
// console's file-mapping step.
export const SOURCE_CONTACTS = {};

// ─── SYSTEM STATUS ──────────────────────────────────────────
export const SYSTEM_STATUS = {
  data: { facilities: 42, demandZones: 120, lanes: 380, historicalPeriods: 24, qualityPct: 98.4 },
  forecast: { model: "Enhanced Demand Forecast (ARIMA + external signals)", horizon: "6 months", lastUpdated: "2026-08-15" },
  optimisation: { solver: "PuLP / HiGHS (MILP)", status: "Optimal", lastRun: "2026-08-18T14:22:00" },
  ai: { agentStatus: "Active", model: "Configured LLM Provider", lastAction: "Stress-tested leading option" },
};


// ─── PERIODS ────────────────────────────────────────────────
export const PERIODS = [
  { id: "Q3_2026", label: "Q3 2026", short: "Q3 2026", prevId: "Q2_2026" },
  { id: "Q2_2026", label: "Q2 2026", short: "Q2 2026", prevId: "Q1_2026" },
  { id: "Q1_2026", label: "Q1 2026", short: "Q1 2026", prevId: "Q4_2025" },
  { id: "Q4_2025", label: "Q4 2025", short: "Q4 2025", prevId: "Q3_2025" },
];

// ─── FACILITY KPIs BY PERIOD ────────────────────────────────
// Each facility + period combination has its own KPI snapshot.
// "prev" values are for the comparison period.
export const FACILITY_KPIS = {
  DC_DELHI: {
    Q3_2026: {
      utilisation: { value: 94.0, capacity: 10000, unit: "units/day", prev: 88.0, status: "critical" },
      sla: { value: 96.7, target: 95, prev: 94.9, status: "normal" },
      totalCost: { value: 1184000, prev: 1223000, status: "normal" },
      inventoryDays: { value: 11.2, prev: 12.1, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 88.0, capacity: 10000, unit: "units/day", prev: 84.0, status: "warning" },
      sla: { value: 94.9, target: 95, prev: 95.1, status: "warning" },
      totalCost: { value: 1223000, prev: 1198000, status: "warning" },
      inventoryDays: { value: 12.1, prev: 11.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_MUMBAI: {
    Q3_2026: {
      utilisation: { value: 75.6, capacity: 9000, unit: "units/day", prev: 72.1, status: "normal" },
      sla: { value: 95.8, target: 95, prev: 94.2, status: "normal" },
      totalCost: { value: 980000, prev: 1010000, status: "normal" },
      inventoryDays: { value: 9.8, prev: 10.4, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 72.1, capacity: 9000, unit: "units/day", prev: 70.5, status: "normal" },
      sla: { value: 94.2, target: 95, prev: 94.8, status: "warning" },
      totalCost: { value: 1010000, prev: 995000, status: "normal" },
      inventoryDays: { value: 10.4, prev: 10.1, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_BENGALURU: {
    Q3_2026: {
      utilisation: { value: 74.7, capacity: 7500, unit: "units/day", prev: 71.2, status: "normal" },
      sla: { value: 96.2, target: 95, prev: 95.5, status: "normal" },
      totalCost: { value: 840000, prev: 860000, status: "normal" },
      inventoryDays: { value: 10.5, prev: 11.0, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 71.2, capacity: 7500, unit: "units/day", prev: 69.8, status: "normal" },
      sla: { value: 95.5, target: 95, prev: 95.0, status: "normal" },
      totalCost: { value: 860000, prev: 845000, status: "normal" },
      inventoryDays: { value: 11.0, prev: 10.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_KOLKATA: {
    Q3_2026: {
      utilisation: { value: 53.3, capacity: 6000, unit: "units/day", prev: 51.0, status: "normal" },
      sla: { value: 93.1, target: 95, prev: 92.4, status: "warning" },
      totalCost: { value: 520000, prev: 530000, status: "normal" },
      inventoryDays: { value: 14.3, prev: 14.8, status: "warning" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 51.0, capacity: 6000, unit: "units/day", prev: 49.5, status: "normal" },
      sla: { value: 92.4, target: 95, prev: 93.0, status: "warning" },
      totalCost: { value: 530000, prev: 525000, status: "normal" },
      inventoryDays: { value: 14.8, prev: 14.5, status: "warning" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_GUWAHATI: {
    Q3_2026: {
      utilisation: { value: 52.5, capacity: 4000, unit: "units/day", prev: 50.0, status: "normal" },
      sla: { value: 91.4, target: 95, prev: 90.8, status: "warning" },
      totalCost: { value: 310000, prev: 320000, status: "normal" },
      inventoryDays: { value: 16.1, prev: 16.8, status: "warning" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 50.0, capacity: 4000, unit: "units/day", prev: 48.2, status: "normal" },
      sla: { value: 90.8, target: 95, prev: 91.2, status: "warning" },
      totalCost: { value: 320000, prev: 315000, status: "normal" },
      inventoryDays: { value: 16.8, prev: 16.5, status: "warning" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_BADDI: {
    Q3_2026: {
      utilisation: { value: 93.3, capacity: 12000, unit: "units/day", prev: 88.0, status: "critical" },
      sla: { value: 97.1, target: 95, prev: 96.2, status: "normal" },
      totalCost: { value: 1420000, prev: 1380000, status: "warning" },
      inventoryDays: { value: 8.5, prev: 9.0, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 88.0, capacity: 12000, unit: "units/day", prev: 85.0, status: "warning" },
      sla: { value: 96.2, target: 95, prev: 95.8, status: "normal" },
      totalCost: { value: 1380000, prev: 1350000, status: "normal" },
      inventoryDays: { value: 9.0, prev: 8.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_PUNE: {
    Q3_2026: {
      utilisation: { value: 78.0, capacity: 10000, unit: "units/day", prev: 75.0, status: "normal" },
      sla: { value: 96.8, target: 95, prev: 96.0, status: "normal" },
      totalCost: { value: 1100000, prev: 1120000, status: "normal" },
      inventoryDays: { value: 10.0, prev: 10.5, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 75.0, capacity: 10000, unit: "units/day", prev: 73.0, status: "normal" },
      sla: { value: 96.0, target: 95, prev: 95.5, status: "normal" },
      totalCost: { value: 1120000, prev: 1100000, status: "normal" },
      inventoryDays: { value: 10.5, prev: 10.2, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_HYDERABAD: {
    Q3_2026: {
      utilisation: { value: 76.3, capacity: 8000, unit: "units/day", prev: 73.0, status: "normal" },
      sla: { value: 95.9, target: 95, prev: 95.2, status: "normal" },
      totalCost: { value: 890000, prev: 910000, status: "normal" },
      inventoryDays: { value: 11.8, prev: 12.3, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 73.0, capacity: 8000, unit: "units/day", prev: 71.5, status: "normal" },
      sla: { value: 95.2, target: 95, prev: 95.0, status: "normal" },
      totalCost: { value: 910000, prev: 895000, status: "normal" },
      inventoryDays: { value: 12.3, prev: 12.0, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_KOLKATA: {
    Q3_2026: {
      utilisation: { value: 70.0, capacity: 6000, unit: "units/day", prev: 67.0, status: "normal" },
      sla: { value: 94.5, target: 95, prev: 93.8, status: "warning" },
      totalCost: { value: 580000, prev: 590000, status: "normal" },
      inventoryDays: { value: 13.2, prev: 13.8, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    Q2_2026: {
      utilisation: { value: 67.0, capacity: 6000, unit: "units/day", prev: 65.0, status: "normal" },
      sla: { value: 93.8, target: 95, prev: 94.0, status: "warning" },
      totalCost: { value: 590000, prev: 580000, status: "normal" },
      inventoryDays: { value: 13.8, prev: 13.5, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
};

// ─── HOME INSIGHTS BY FACILITY ──────────────────────────────
// Each facility has context-specific insights matching the 5 numbered items.
export const HOME_INSIGHTS = {
  DC_DELHI: [
    {
      id: "INS_CAP_RISK",
      num: 1,
      icon: "⚠️", iconBg: "#fef2f2", iconColor: "#dc2626",
      title: "Capacity risk at Delhi NCR DC",
      subtitle: "Utilisation is projected to reach 108% in December.",
      impact: "High Impact", impactColor: "#dc2626",
      why: "Demand forecast shows 14.2% growth over the next 4 months, exceeding available capacity starting October.",
      action: "click → overlay", provenance: "FORECAST",
      detail: {
        whatIFound: "Delhi NCR DC is operating at 94% utilisation. Demand forecast projects a 14.2% growth in North India over the next 4 months.",
        whyItMatters: "At current trajectory, the facility will exceed its 10,000 units/day capacity by December 2026 (projected 108%), causing spillover to costlier corridors and SLA degradation.",
        evidence: [
          { label: "Current utilisation", value: "94%", provenance: "MODEL FACT" },
          { label: "Forecast demand growth", value: "+14.2%", provenance: "FORECAST" },
          { label: "Capacity threshold", value: "100% (10,000 u/d)", provenance: "MODEL FACT" },
          { label: "Projected Dec utilisation", value: "108%", provenance: "FORECAST" },
          { label: "Regional GDP signal", value: "+2.1% above trend", provenance: "EXTERNAL SIGNAL" },
        ],
        whatITested: ["Flow rebalancing across DCs", "Capacity expansion at Delhi NCR", "DC consolidation (close Guwahati)", "New DC at Lucknow"],
        recommendation: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata DCs.",
        nextAction: "Review Scenario",
      },
    },
    {
      id: "INS_INVESTIGATE_DELHI",
      num: 2,
      icon: "🔍", iconBg: "#fef2f2", iconColor: "#dc2626",
      title: "Investigate Delhi capacity risk",
      subtitle: "Exceeds 10,000 units/day threshold by Q4.",
      impact: "High Impact", impactColor: "#dc2626",
      why: "North India regional surge requires immediate throughput reallocation or hub capacity expansion.",
      action: "click → overlay", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Delhi NCR DC operates at 94% utilisation. Projected volume crosses critical threshold in Q4 2026.",
        whyItMatters: "Unmitigated capacity bottleneck will trigger ₹2.4L in emergency transport penalties and drop SLA below 95%.",
        evidence: [
          { label: "Capacity ceiling", value: "10,000 u/d", provenance: "MODEL FACT" },
          { label: "Headroom remaining", value: "600 u/d (6.0%)", provenance: "MODEL FACT" },
          { label: "Spillover corridor cost", value: "+18.4% surcharge", provenance: "MODEL FACT" },
        ],
        whatITested: ["Brownfield expansion (+3,000 u/d)", "Cross-dock volume bypass"],
        recommendation: "Investigate corridor rebalancing scenario to absorb northern overflow.",
        nextAction: "Review Scenario",
      },
    },
    {
      id: "INS_EXPLORE_UNDERUTIL",
      num: 3,
      icon: "📈", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Explore underutilised capacity",
      subtitle: "Kolkata DC has 41% spare capacity available.",
      impact: "Opportunity", impactColor: "#16a34a",
      why: "Current throughput is 3,200 units/day vs capacity of 6,000 units/day.",
      action: "click → overlay", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Kolkata DC is operating at only 53.3% utilisation with 2,800 units/day of spare capacity.",
        whyItMatters: "This spare capacity can absorb volume from overloaded northern facilities without capital expenditure.",
        evidence: [
          { label: "Kolkata capacity", value: "6,000 u/d", provenance: "MODEL FACT" },
          { label: "Current throughput", value: "3,200 u/d", provenance: "MODEL FACT" },
          { label: "Spare capacity", value: "2,800 u/d (46.7%)", provenance: "MODEL FACT" },
          { label: "Handling cost", value: "₹3.5/unit (lowest in network)", provenance: "MODEL FACT" },
        ],
        whatITested: ["Redirect 800 units/day from Delhi NCR overflow to Kolkata"],
        recommendation: "Include Kolkata DC in flow rebalancing scenario.",
        nextAction: "Review Scenario",
      },
    },
    {
      id: "INS_UNDERUTIL_CAP",
      num: 4,
      icon: "⚡", iconBg: "#eff6ff", iconColor: "#2563eb",
      title: "Underutilised capable capacity",
      subtitle: "Western and Eastern network headroom can absorb surges.",
      impact: "Optimization", impactColor: "#2563eb",
      why: "Network-wide aggregate DC utilisation is 72.4%, leaving 27.6% overall capacity headroom.",
      action: "", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Aggregate network DC utilisation is 72.4%, with spare handling bandwidth concentrated in Kolkata and Mumbai.",
        whyItMatters: "Enables multi-echelon load leveling across the supply chain network without adding fixed warehouse footprint.",
        evidence: [
          { label: "Network DC Capacity", value: "37,500 u/d", provenance: "MODEL FACT" },
          { label: "Active Throughput", value: "27,150 u/d", provenance: "MODEL FACT" },
          { label: "System Headroom", value: "10,350 u/d (27.6%)", provenance: "MODEL FACT" },
        ],
        whatITested: ["Network-wide multi-echelon MILP optimization"],
        recommendation: "Activate optimized base case routing.",
        nextAction: "Review Scenario",
      },
    },
    {
      id: "INS_KOLKATA_SPARE",
      num: 5,
      icon: "🏪", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Kolkata DC has spare capacity",
      subtitle: "Operating at 53.3% with ₹3.5/unit handling rate.",
      impact: "Opportunity", impactColor: "#16a34a",
      why: "East corridor provides a cost-effective alternative for North-East demand fulfillment.",
      action: "click → overlay", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Kolkata DC handles 3,200 u/d against 6,000 u/d total capacity with highest SLA reliability (97.4%).",
        whyItMatters: "Provides high SLA buffer and immediate absorption capacity for Baddi plant volume rebalancing.",
        evidence: [
          { label: "Kolkata SLA", value: "97.4%", provenance: "MODEL FACT" },
          { label: "Available Buffer", value: "2,800 u/d", provenance: "MODEL FACT" },
          { label: "Lead time to East markets", value: "1.1 days", provenance: "MODEL FACT" },
        ],
        whatITested: ["Routing Baddi → Kolkata DC → Patna/Ranchi markets"],
        recommendation: "Reallocate 12% Baddi volume to Kolkata DC.",
        nextAction: "Review Scenario",
      },
    },
  ],
  DC_MUMBAI: [
    {
      id: "INS_MUMBAI_SLA",
      icon: "⚠️", iconBg: "#fffbeb", iconColor: "#d97706",
      title: "SLA approaching target threshold",
      subtitle: "On-time delivery at 95.8%, close to 95% target.",
      impact: "Medium Impact", impactColor: "#d97706",
      why: "SLA has been trending downward over the last 2 months. Current buffer is only 0.8pp above target.",
      action: "Investigate", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Mumbai DC SLA has declined from 96.5% to 95.8% over the last 2 months.",
        whyItMatters: "If the trend continues, SLA will breach the 95% target within 1–2 months, triggering contractual penalties.",
        evidence: [
          { label: "Current SLA", value: "95.8%", provenance: "MODEL FACT" },
          { label: "Target SLA", value: "≥95%", provenance: "MODEL FACT" },
          { label: "Buffer", value: "0.8pp", provenance: "MODEL FACT" },
          { label: "Trend direction", value: "Declining", provenance: "FORECAST" },
        ],
        whatITested: ["Increasing carrier allocation", "Adjusting dispatch schedules"],
        recommendation: "Monitor closely and prepare carrier contingency.",
        nextAction: "Review forecast",
      },
    },
    {
      id: "INS_MUMBAI_COST",
      icon: "💰", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Cost reduction achieved",
      subtitle: "Total cost down 3.0% vs previous period.",
      impact: "Positive", impactColor: "#16a34a",
      why: "Optimised routing from Pune Plant reduced average transport cost per unit by ₹1.2.",
      action: "See why", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Mumbai DC total cost decreased from ₹10.1L to ₹9.8L through improved Pune routing.",
        whyItMatters: "Validates the effectiveness of the Pune Plant → Mumbai DC optimisation implemented last month.",
        evidence: [
          { label: "Cost reduction", value: "₹30K/period", provenance: "MODEL FACT" },
          { label: "Transport cost saving", value: "₹1.2/unit", provenance: "MODEL FACT" },
        ],
        whatITested: [],
        recommendation: "Continue current routing configuration.",
        nextAction: "No action required",
      },
    },
    {
      id: "INS_SPARE_CAP",
      icon: "📈", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Capacity headroom available",
      subtitle: "Mumbai DC at 75.6% — 2,200 units/day spare.",
      impact: "Opportunity", impactColor: "#16a34a",
      why: "Spare capacity could absorb overflow from constrained facilities in the western corridor.",
      action: "Explore", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Mumbai DC has 2,200 units/day of spare capacity.",
        whyItMatters: "Can serve as a buffer for demand surge or overflow from other facilities.",
        evidence: [
          { label: "Capacity", value: "9,000 u/d", provenance: "MODEL FACT" },
          { label: "Current throughput", value: "6,800 u/d", provenance: "MODEL FACT" },
          { label: "Spare", value: "2,200 u/d", provenance: "MODEL FACT" },
        ],
        whatITested: [],
        recommendation: "Consider for western corridor rebalancing.",
        nextAction: "Explore scenario",
      },
    },
    {
      id: "INS_RECOMMENDATION",
      icon: "✨", iconBg: "#f5f0fa", iconColor: "#6B2FA0",
      title: "Recommendation ready",
      subtitle: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata.",
      impact: "High Value", impactColor: "#6B2FA0",
      why: "Reduces total cost by 7.8% while maintaining SLA at 96.7%. Robust under +15% demand stress test.",
      action: "Review Recommendation", provenance: "AI ASSESSMENT",
      detail: null,
    },
  ],
  // Fallback: other facilities get a default set
  _default: [
    {
      id: "INS_PERF_STABLE",
      icon: "✅", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Performance within normal range",
      subtitle: "All KPIs within target thresholds for this facility.",
      impact: "Normal", impactColor: "#16a34a",
      why: "No anomalies detected in the current period. Utilisation, SLA, cost, and inventory are within expected ranges.",
      action: "Explore", provenance: "MODEL FACT",
      detail: {
        whatIFound: "This facility is operating within expected parameters across all monitored KPIs.",
        whyItMatters: "No immediate intervention required, but continued monitoring is recommended.",
        evidence: [
          { label: "Status", value: "All KPIs within range", provenance: "MODEL FACT" },
        ],
        whatITested: [],
        recommendation: "Continue monitoring. No immediate action required.",
        nextAction: "No action required",
      },
    },
    {
      id: "INS_RECOMMENDATION",
      icon: "✨", iconBg: "#f5f0fa", iconColor: "#6B2FA0",
      title: "Recommendation ready",
      subtitle: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata.",
      impact: "High Value", impactColor: "#6B2FA0",
      why: "Reduces total cost by 7.8% while maintaining SLA at 96.7%. Robust under +15% demand stress test.",
      action: "Review Recommendation", provenance: "AI ASSESSMENT",
      detail: null,
    },
  ],
};

// ─── HOME ACTION ITEMS ───────────────────────────────────────
export const HOME_ACTION_ITEMS = [
  {
    id: "ACT_REBALANCE_BADDI",
    title: "Rebalance Baddi volume",
    tag: "High Value",
    tagColor: "#6B2FA0",
    scenarioId: "SCENARIO_REBALANCE",
    why: "I identified a capacity constraint at Delhi NCR and found that reallocating Baddi volume can reduce cost while maintaining SLA.",
    rootCause: [
      { label: "Delhi NCR utilisation", value: "94%", provenance: "MODEL FACT" },
      { label: "Forecast demand growth", value: "14.2%", provenance: "FORECAST" },
      { label: "December capacity risk", value: "High (108% projected)", provenance: "FORECAST" },
    ],
    expectedImpact: {
      cost: "↓ 7.8%",
      sla: "96.7%",
      risk: "Low",
    },
    whatITested: [
      "Rebalancing 12% of Baddi volume to Delhi NCR and Kolkata",
      "Capacity expansion at Delhi NCR (+3,000 u/d)",
      "Redistribution to Kolkata DC (absorbs 800 u/d spare capacity)",
    ],
    nextAction: "Review Scenario",
  },
  {
    id: "ACT_INVESTIGATE_DELHI",
    title: "Investigate Delhi capacity risk",
    tag: "High Impact",
    tagColor: "#dc2626",
    scenarioId: "SCENARIO_EXPAND_DELHI",
    why: "Demand forecast shows North India volume will cross the 10,000 units/day threshold by October 2026.",
    rootCause: [
      { label: "Current Throughput", value: "9,400 u/d", provenance: "MODEL FACT" },
      { label: "Facility Capacity", value: "10,000 u/d", provenance: "MODEL FACT" },
      { label: "Headroom Remaining", value: "600 u/d (6.0%)", provenance: "MODEL FACT" },
    ],
    expectedImpact: {
      cost: "Avoids ₹2.4L penalty",
      sla: "≥96.5%",
      risk: "Eliminated",
    },
    whatITested: [
      "On-site brownfield facility expansion",
      "Shift-timing adjustments and bottleneck staging",
    ],
    nextAction: "Review Scenario",
  },
  {
    id: "ACT_EXPLORE_UNDERUTIL",
    title: "Explore underutilised capacity",
    tag: "Opportunity",
    tagColor: "#16a34a",
    scenarioId: "SCENARIO_REBALANCE",
    why: "Kolkata DC has 41% (2,800 units/day) of spare handling capacity available at low unit cost.",
    rootCause: [
      { label: "Kolkata DC Capacity", value: "6,000 u/d", provenance: "MODEL FACT" },
      { label: "Current Throughput", value: "3,200 u/d", provenance: "MODEL FACT" },
      { label: "Spare Capacity", value: "2,800 u/d (46.7%)", provenance: "MODEL FACT" },
    ],
    expectedImpact: {
      cost: "Lowest handling cost (₹3.5/u)",
      sla: "97.1%",
      risk: "Optimal",
    },
    whatITested: [
      "Absorbing eastern overflow directly through Kolkata DC",
      "Corridor flow rerouting from Baddi manufacturing plant",
    ],
    nextAction: "Review Scenario",
  },
];

export function getInsightsForFacility(facilityId) {
  return HOME_INSIGHTS[facilityId] || HOME_INSIGHTS.DC_DELHI || HOME_INSIGHTS._default || [];
}

export function getKpisForFacility(facilityId, periodId) {
  const facilityKpis = FACILITY_KPIS[facilityId] || FACILITY_KPIS.DC_DELHI;
  if (!facilityKpis) return null;
  return facilityKpis[periodId] || facilityKpis["Q3_2026"] || facilityKpis[Object.keys(facilityKpis)[0]] || null;
}

// ─── HELPER FUNCTIONS ───────────────────────────────────────
export function getFacilityById(id) {
  return [...PLANTS, ...DCS, ...MARKETS].find(f => f.id === id);
}

export function getScenarioById(id) {
  return SCENARIOS.find(s => s.id === id);
}

export function formatCurrency(value, decimals = 0) {
  if (value >= 100000) return "₹" + (value / 100000).toFixed(1) + "L";
  if (value >= 1000) return "₹" + (value / 1000).toFixed(1) + "K";
  return "₹" + value.toFixed(decimals);
}

export function formatNumber(value) {
  return value.toLocaleString("en-IN");
}

// Single owner for the utilization risk bands used everywhere in the app
// (KPI tiles, map markers, facility panels, legends) — Healthy <85%,
// Stress 85-95%, Critical >=95%, matching the audited KPI Formula/Logic
// Verification's DC Capacity Utilization thresholds. Every other place
// that needs a color, label or tag class for a utilization number should
// call one of these three functions rather than re-testing the raw
// percentage, so the band can never drift out of sync between screens.
export function getUtilColor(pct) {
  if (pct >= 95) return "#dc2626";
  if (pct >= 85) return "#f59e0b";
  return "#22c55e";
}

export function getUtilLabel(pct) {
  if (pct >= 95) return "Critical";
  if (pct >= 85) return "Stress";
  return "Healthy";
}

export function getUtilTagClass(pct) {
  if (pct >= 95) return "tag-danger";
  if (pct >= 85) return "tag-warning";
  return "tag-success";
}

// ─── S6: Optimized Base Case (single owner) ──────────────────────────
// The ONE authoritative computation of "today's facility footprint held
// fixed, only routing/allocation re-optimized" — Z = ΣC_ij·x_ij (transport)
// + ΣF_j·y_j (fixed facility, unchanged since footprint is fixed) +
// ΣP_unmet·u_k (shortage penalty, ₹10,000/unit) + SLA-pen, per the KPI
// Formula/Logic Verification's Total Network Cost definition.
//
// This is NOT the same figure as SCENARIOS' SCN_REBALANCE ("Recommended"):
// that scenario allows volume reallocation across plants and is a candidate
// action a user opts into, whereas the Optimized Base Case is the routing-
// only floor achievable with zero footprint change. Do not conflate them —
// every screen that shows a "Baseline Cost" / "Optimized Cost" / "Savings"
// figure for S6 must call this function rather than reading SCENARIOS or
// getNetworkKpis() (both are separate, independently-owned figures).
//
// Provenance: DEMO. No live optimizer is wired into this prototype, so
// these numbers are hand-authored to be internally consistent (fixed cost
// unchanged, transport cost improved, shortage penalty cleared by better
// routing) rather than fabricated as if they were a solver result. Once a
// real deterministic engine is wired in, this function's body — not its
// call sites — is what gets replaced, and `source` becomes DETERMINISTIC_ENGINE.
export function getOptimizedBaseCase() {
  const baseline = {
    transportCost: 640000,
    fixedCost: 480000,
    variableCost: 145000,
    unmetPenalty: 20000, // 2 units unmet demand @ ₹10,000/unit
    slaPenalty: 0,
    sla: 91.2,
    fillRate: 89.5,
    avgUtilization: 78.0,
    maxUtilization: 94.0, // DC Delhi NCR — Stress band
    inventoryCost: 95000,
    carbonKgCo2e: 342000,
    avgDistanceKm: 412,
  };
  baseline.totalCost = baseline.transportCost + baseline.fixedCost + baseline.variableCost
    + baseline.unmetPenalty + baseline.slaPenalty;

  const optimized = {
    transportCost: 590000,
    fixedCost: baseline.fixedCost, // footprint held fixed — must equal baseline
    variableCost: 142000,
    unmetPenalty: 0, // routing resolves the shortfall that caused baseline's penalty
    slaPenalty: 0,
    sla: 96.5,
    fillRate: 95.8,
    avgUtilization: 82.0,
    maxUtilization: 84.0, // DC Delhi NCR relieved from Stress into Healthy band
    inventoryCost: 92000,
    carbonKgCo2e: 318000,
    avgDistanceKm: 378,
  };
  optimized.totalCost = optimized.transportCost + optimized.fixedCost + optimized.variableCost
    + optimized.unmetPenalty + optimized.slaPenalty;

  const savingsAbs = baseline.totalCost - optimized.totalCost;
  const savingsPct = +((savingsAbs / baseline.totalCost) * 100).toFixed(1);

  return {
    source: "DEMO",
    footprintChange: "None (zero CapEx — same facility set as Actual)",
    baseline: { ...baseline, capacityHeadroomPct: +(100 - baseline.maxUtilization).toFixed(1) },
    optimized: { ...optimized, capacityHeadroomPct: +(100 - optimized.maxUtilization).toFixed(1) },
    savings: { abs: savingsAbs, pct: savingsPct },
  };
}

