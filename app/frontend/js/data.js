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
  { id: "PLT_BADDI",     name: "Baddi Plant",      city: "Baddi",      state: "Himachal Pradesh", lat: 30.96, lng: 76.79, capacity: 12000, throughput: 11200, region: "North",  status: "EXISTING" },
  { id: "PLT_PUNE",      name: "Pune Plant",       city: "Pune",       state: "Maharashtra",      lat: 18.52, lng: 73.86, capacity: 10000, throughput: 7800,  region: "West",   status: "EXISTING" },
  { id: "PLT_HYDERABAD", name: "Hyderabad Plant",  city: "Hyderabad",  state: "Telangana",        lat: 17.38, lng: 78.49, capacity: 8000,  throughput: 6100,  region: "South",  status: "EXISTING" },
  { id: "PLT_KOLKATA",   name: "Kolkata Plant",    city: "Kolkata",    state: "West Bengal",       lat: 22.57, lng: 88.36, capacity: 6000,  throughput: 4200,  region: "East",   status: "EXISTING" },
];

export const DCS = [
  { id: "DC_DELHI",     name: "Delhi NCR DC",    city: "Delhi NCR",   state: "Delhi",          lat: 28.61, lng: 77.21, capacity: 10000, throughput: 9400,  fixedCost: 120, handlingCost: 4.2, region: "North",     status: "EXISTING", utilPct: 94.0 },
  { id: "DC_MUMBAI",    name: "Mumbai DC",       city: "Mumbai",      state: "Maharashtra",    lat: 19.08, lng: 72.88, capacity: 9000,  throughput: 6800,  fixedCost: 140, handlingCost: 4.8, region: "West",      status: "EXISTING", utilPct: 75.6 },
  { id: "DC_BENGALURU", name: "Bengaluru DC",    city: "Bengaluru",   state: "Karnataka",      lat: 12.97, lng: 77.59, capacity: 7500,  throughput: 5600,  fixedCost: 110, handlingCost: 4.0, region: "South",     status: "EXISTING", utilPct: 74.7 },
  { id: "DC_KOLKATA",   name: "Kolkata DC",      city: "Kolkata",     state: "West Bengal",    lat: 22.57, lng: 88.36, capacity: 6000,  throughput: 3200,  fixedCost: 85,  handlingCost: 3.5, region: "East",      status: "EXISTING", utilPct: 53.3 },
  { id: "DC_GUWAHATI",  name: "Guwahati DC",     city: "Guwahati",    state: "Assam",          lat: 26.14, lng: 91.74, capacity: 4000,  throughput: 2100,  fixedCost: 65,  handlingCost: 3.8, region: "Northeast", status: "EXISTING", utilPct: 52.5 },
];

export const MARKETS = [
  { id: "MKT_DELHI",     name: "Delhi",      lat: 28.70, lng: 77.10, demand: 4200, slaDays: 2, priority: "High",   region: "North" },
  { id: "MKT_MUMBAI",    name: "Mumbai",     lat: 19.08, lng: 72.88, demand: 3800, slaDays: 2, priority: "High",   region: "West" },
  { id: "MKT_BENGALURU", name: "Bengaluru",  lat: 12.97, lng: 77.59, demand: 3200, slaDays: 2, priority: "High",   region: "South" },
  { id: "MKT_CHENNAI",   name: "Chennai",    lat: 13.08, lng: 80.27, demand: 2400, slaDays: 3, priority: "Medium", region: "South" },
  { id: "MKT_HYDERABAD", name: "Hyderabad",  lat: 17.38, lng: 78.49, demand: 2800, slaDays: 3, priority: "Medium", region: "South" },
  { id: "MKT_KOLKATA",   name: "Kolkata",    lat: 22.57, lng: 88.36, demand: 2200, slaDays: 3, priority: "Medium", region: "East" },
  { id: "MKT_AHMEDABAD", name: "Ahmedabad",  lat: 23.03, lng: 72.57, demand: 1800, slaDays: 3, priority: "Medium", region: "West" },
  { id: "MKT_JAIPUR",    name: "Jaipur",     lat: 26.91, lng: 75.79, demand: 1500, slaDays: 3, priority: "Medium", region: "North" },
  { id: "MKT_LUCKNOW",   name: "Lucknow",    lat: 26.85, lng: 80.95, demand: 1400, slaDays: 3, priority: "Medium", region: "North" },
  { id: "MKT_GUWAHATI",  name: "Guwahati",   lat: 26.14, lng: 91.74, demand: 1100, slaDays: 4, priority: "Low",    region: "Northeast" },
];

// ─── LANES (key corridors with cost, distance, lead time) ───
export const LANES = [
  // Plant → DC
  { from: "PLT_BADDI",     to: "DC_DELHI",     cost: 12, distance: 310,  leadTime: 1.0, flow: 8200,  mode: "ROAD" },
  { from: "PLT_BADDI",     to: "DC_MUMBAI",    cost: 28, distance: 1420, leadTime: 2.5, flow: 1800,  mode: "ROAD" },
  { from: "PLT_BADDI",     to: "DC_KOLKATA",   cost: 32, distance: 1800, leadTime: 3.0, flow: 1200,  mode: "ROAD" },
  { from: "PLT_PUNE",      to: "DC_MUMBAI",    cost: 8,  distance: 150,  leadTime: 0.5, flow: 5000,  mode: "ROAD" },
  { from: "PLT_PUNE",      to: "DC_BENGALURU", cost: 18, distance: 840,  leadTime: 1.5, flow: 2800,  mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_BENGALURU", cost: 15, distance: 570,  leadTime: 1.0, flow: 2800,  mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_MUMBAI",    cost: 20, distance: 710,  leadTime: 1.5, flow: 1500,  mode: "ROAD" },
  { from: "PLT_HYDERABAD", to: "DC_KOLKATA",   cost: 25, distance: 1500, leadTime: 2.5, flow: 1300,  mode: "ROAD" },
  { from: "PLT_KOLKATA",   to: "DC_KOLKATA",   cost: 5,  distance: 30,   leadTime: 0.2, flow: 2700,  mode: "ROAD" },
  { from: "PLT_KOLKATA",   to: "DC_GUWAHATI",  cost: 22, distance: 1000, leadTime: 2.0, flow: 1500,  mode: "ROAD" },
  { from: "PLT_BADDI",     to: "DC_GUWAHATI",  cost: 38, distance: 2200, leadTime: 3.5, flow: 600,   mode: "ROAD" },
  // DC → Market (major flows)
  { from: "DC_DELHI",     to: "MKT_DELHI",     cost: 4,  distance: 40,   leadTime: 0.3, flow: 4200, mode: "ROAD" },
  { from: "DC_DELHI",     to: "MKT_JAIPUR",    cost: 10, distance: 270,  leadTime: 0.8, flow: 1500, mode: "ROAD" },
  { from: "DC_DELHI",     to: "MKT_LUCKNOW",   cost: 14, distance: 500,  leadTime: 1.2, flow: 1400, mode: "ROAD" },
  { from: "DC_DELHI",     to: "MKT_AHMEDABAD", cost: 22, distance: 950,  leadTime: 1.8, flow: 800,  mode: "ROAD" },
  { from: "DC_MUMBAI",    to: "MKT_MUMBAI",    cost: 3,  distance: 25,   leadTime: 0.2, flow: 3800, mode: "ROAD" },
  { from: "DC_MUMBAI",    to: "MKT_AHMEDABAD", cost: 12, distance: 530,  leadTime: 1.0, flow: 1000, mode: "ROAD" },
  { from: "DC_MUMBAI",    to: "MKT_HYDERABAD", cost: 18, distance: 710,  leadTime: 1.5, flow: 1200, mode: "ROAD" },
  { from: "DC_BENGALURU", to: "MKT_BENGALURU", cost: 3,  distance: 20,   leadTime: 0.2, flow: 3200, mode: "ROAD" },
  { from: "DC_BENGALURU", to: "MKT_CHENNAI",   cost: 10, distance: 350,  leadTime: 0.8, flow: 2400, mode: "ROAD" },
  { from: "DC_KOLKATA",   to: "MKT_KOLKATA",   cost: 3,  distance: 15,   leadTime: 0.2, flow: 2200, mode: "ROAD" },
  { from: "DC_KOLKATA",   to: "MKT_HYDERABAD", cost: 22, distance: 1500, leadTime: 2.5, flow: 600,  mode: "ROAD" },
  { from: "DC_GUWAHATI",  to: "MKT_GUWAHATI",  cost: 4,  distance: 30,   leadTime: 0.3, flow: 1100, mode: "ROAD" },
  { from: "DC_DELHI",     to: "MKT_HYDERABAD", cost: 28, distance: 1500, leadTime: 2.5, flow: 500,  mode: "ROAD" },
  { from: "DC_GUWAHATI",  to: "MKT_KOLKATA",   cost: 18, distance: 1000, leadTime: 2.0, flow: 600,  mode: "ROAD" },
  { from: "DC_MUMBAI",    to: "MKT_CHENNAI",   cost: 24, distance: 1330, leadTime: 2.2, flow: 500,  mode: "ROAD" },
];

// ─── DEMAND HISTORY (24 months) & FORECAST ──────────────────
export const DEMAND_HISTORY = {
  months: [
    "Jan'25","Feb'25","Mar'25","Apr'25","May'25","Jun'25",
    "Jul'25","Aug'25","Sep'25","Oct'25","Nov'25","Dec'25",
    "Jan'26","Feb'26","Mar'26","Apr'26","May'26","Jun'26",
    "Jul'26","Aug'26","Sep'26","Oct'26","Nov'26","Dec'26",
  ],
  // North India aggregate demand (units/day)
  northIndia: [
    7100,7000,7300,7200,7400,7100,
    7500,7600,7800,8000,8200,8800,
    8400,8300,8600,8500,8800,8600,
    9000,9200,9400,9600,9800,10800,
  ],
  // Baddi DC capacity line (constant)
  baddiCapacity: 10000,
};

export const FORECAST = {
  months: ["Jan'27","Feb'27","Mar'27","Apr'27","May'27","Jun'27"],
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
    { id: "DQ_01", type: "Unit Inconsistency",   field: "capacity",      facility: "DC_GUWAHATI",  detail: "Capacity reported in tonnes, expected units/day", severity: "warning",  status: "needs_review" },
    { id: "DQ_02", type: "Unit Inconsistency",   field: "demand",        market: "MKT_LUCKNOW",    detail: "Q3 demand in cases, rest in units",              severity: "warning",  status: "needs_review" },
    { id: "DQ_03", type: "Missing Value",        field: "capacity",      facility: "DC_GUWAHATI",  detail: "Max capacity not specified for expansion scenario", severity: "info",    status: "needs_review" },
    { id: "DQ_04", type: "AI Mapping Uncertain", field: "Qty → Demand",  source: "Distributor A",  detail: "Field 'Qty' mapped to Demand_Units (87% confidence)", severity: "warning", status: "needs_review" },
    { id: "DQ_05", type: "AI Mapping Uncertain", field: "Vol → Demand",  source: "Distributor B",  detail: "Field 'Volume' mapped to Demand_Units (91% confidence)", severity: "info",  status: "needs_review" },
    { id: "DQ_06", type: "AI Mapping Uncertain", field: "Units Shipped", source: "Distributor C",  detail: "Field 'Units Shipped' mapped to Demand_Units (95% confidence)", severity: "info", status: "auto_mapped" },
    { id: "DQ_07", type: "Outlier",              field: "transport_cost", lane: "PLT_BADDI→DC_GUWAHATI", detail: "Cost ₹38/unit is 2.1σ above corridor average", severity: "info", status: "reviewed" },
    { id: "DQ_08", type: "Stale Data",           field: "demand",        market: "MKT_GUWAHATI",   detail: "Last updated 45 days ago",                       severity: "info",    status: "needs_review" },
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
      { field: "Base Rate",               value: "₹10/kg",         confidence: "HIGH" },
      { field: "Fuel Surcharge",          value: "₹2/kg",          confidence: "HIGH" },
      { field: "Non-Serviceable Surcharge", value: "₹5/kg for 12 pin codes in NE India", confidence: "MEDIUM" },
      { field: "Minimum Volume",          value: "500 kg/shipment", confidence: "HIGH" },
      { field: "Penalty (Late Pickup)",   value: "₹500/incident",  confidence: "MEDIUM" },
      { field: "Effective Date",          value: "2026-04-01",      confidence: "HIGH" },
    ],
    effectiveCost: "₹12–17/kg (depending on route)",
    hiddenCostAlert: true,
  },
  vendorB: {
    name: "SpeedFreight India",
    headlineRate: "₹12/kg",
    extractedTerms: [
      { field: "Base Rate",      value: "₹12/kg (all-inclusive)", confidence: "HIGH" },
      { field: "Fuel Surcharge", value: "Included",               confidence: "HIGH" },
      { field: "Coverage",       value: "All pin codes",          confidence: "HIGH" },
      { field: "Minimum Volume", value: "200 kg/shipment",        confidence: "HIGH" },
      { field: "Effective Date", value: "2026-04-01",              confidence: "HIGH" },
    ],
    effectiveCost: "₹12/kg (flat)",
    hiddenCostAlert: false,
  },
};

// ─── SCHEMA MAPPING DEMO ────────────────────────────────────
export const SCHEMA_MAPPING = {
  distributors: [
    { name: "Distributor A", field: "Qty",           mappedTo: "Demand_Units", confidence: 87 },
    { name: "Distributor B", field: "Volume",        mappedTo: "Demand_Units", confidence: 91 },
    { name: "Distributor C", field: "Units Shipped", mappedTo: "Demand_Units", confidence: 95 },
    { name: "Distributor D", field: "Order_Qty",     mappedTo: "Demand_Units", confidence: 98 },
  ],
  canonicalField: "Demand_Units",
  sourceTypes: ["ERP", "WMS", "TMS", "Facility Master", "Demand Data", "Lane Data"],
};

// ─── SCENARIO RESULTS (pre-computed mock MILP outputs) ──────
export const SCENARIOS = [
  {
    id: "SCN_ACTUAL",
    name: "Actual (As-Is)",
    type: "BASELINE",
    description: "Current network configuration and observed flows",
    status: "evaluated",
    totalCost: 1284000,
    transportCost: 892000,
    fixedCost: 312000,
    inventoryCost: 80000,
    sla: 91.2,
    avgUtil: 72.4,
    maxUtil: 94.0,
    carbonKg: 142000,
    implementationCost: 0,
    resilience: "Medium",
    feasible: true,
    changes: [],
    stressTestResult: null,
  },
  {
    id: "SCN_OPTIMISED_BASE",
    name: "Optimised Base Case",
    type: "OPTIMISED_BASE",
    description: "Optimise routing within current facility footprint",
    status: "evaluated",
    totalCost: 1198000,
    transportCost: 826000,
    fixedCost: 312000,
    inventoryCost: 60000,
    sla: 94.8,
    avgUtil: 68.1,
    maxUtil: 88.2,
    carbonKg: 131000,
    implementationCost: 15000,
    resilience: "Medium",
    feasible: true,
    changes: ["Rebalance flows from Baddi→Delhi NCR corridor", "Shift 800 units/day to Kolkata DC"],
    stressTestResult: { demandSurge15: "PASS", laneDisruption: "FAIL — single point of failure on Baddi corridor" },
  },
  {
    id: "SCN_REBALANCE",
    name: "Flow Rebalancing",
    type: "SCENARIO",
    description: "Rebalance 12% of Baddi volume to Delhi NCR and Kolkata DCs",
    status: "evaluated",
    totalCost: 1184000,
    transportCost: 818000,
    fixedCost: 312000,
    inventoryCost: 54000,
    sla: 96.7,
    avgUtil: 65.3,
    maxUtil: 82.1,
    carbonKg: 136000,
    implementationCost: 25000,
    resilience: "High",
    feasible: true,
    changes: [
      "Redirect 1,200 units/day from Baddi → Delhi NCR DC",
      "Increase Kolkata DC throughput by 800 units/day",
      "Adjust PLT_PUNE → DC_MUMBAI allocation +400 units/day",
    ],
    stressTestResult: { demandSurge15: "PASS", laneDisruption: "PASS", costSensitivity: "Robust to +12% transport cost" },
  },
  {
    id: "SCN_EXPAND_BADDI",
    name: "Baddi Capacity Expansion",
    type: "SCENARIO",
    description: "Expand Delhi NCR DC capacity by 3,000 units/day",
    status: "evaluated",
    totalCost: 1246000,
    transportCost: 835000,
    fixedCost: 351000,
    inventoryCost: 60000,
    sla: 97.1,
    avgUtil: 62.4,
    maxUtil: 76.0,
    carbonKg: 128000,
    implementationCost: 180000,
    resilience: "High",
    feasible: true,
    changes: ["Expand Delhi NCR DC capacity from 10,000 to 13,000 units/day", "CapEx: ₹18L for warehouse expansion"],
    stressTestResult: { demandSurge15: "PASS", laneDisruption: "PASS", costSensitivity: "High CapEx reduces ROI if demand < forecast" },
  },
  {
    id: "SCN_CONSOLIDATE",
    name: "DC Consolidation (Close Guwahati)",
    type: "SCENARIO",
    description: "Close Guwahati DC, redirect NE volume through Kolkata",
    status: "evaluated",
    totalCost: 1142000,
    transportCost: 812000,
    fixedCost: 247000,
    inventoryCost: 83000,
    sla: 88.4,
    avgUtil: 78.2,
    maxUtil: 96.8,
    carbonKg: 155000,
    implementationCost: 35000,
    resilience: "Low",
    feasible: true,
    changes: ["Close Guwahati DC (save ₹65L/yr fixed cost)", "Redirect 2,100 units/day to Kolkata DC"],
    stressTestResult: { demandSurge15: "FAIL — Kolkata DC exceeds capacity at 112%", laneDisruption: "FAIL", costSensitivity: "Fragile" },
  },
  {
    id: "SCN_NEW_DC",
    name: "New North India DC (Lucknow)",
    type: "SCENARIO",
    description: "Open candidate DC in Lucknow to serve North/East demand",
    status: "evaluated",
    totalCost: 1215000,
    transportCost: 795000,
    fixedCost: 360000,
    inventoryCost: 60000,
    sla: 97.8,
    avgUtil: 58.6,
    maxUtil: 72.0,
    carbonKg: 118000,
    implementationCost: 250000,
    resilience: "Very High",
    feasible: true,
    changes: ["Open new DC in Lucknow (capacity 5,000 units/day)", "Redistribute North India demand across 2 DCs", "CapEx: ₹25L for new facility"],
    stressTestResult: { demandSurge15: "PASS", laneDisruption: "PASS", costSensitivity: "Robust but high upfront investment" },
  },
];

// ─── AI AGENT STATE ─────────────────────────────────────────
export const AGENT_STATE = {
  status: "active",
  currentObjective: "Reduce network cost by at least 8% while maintaining SLA above 95%",
  activityTrace: [
    { step: 1, action: "Analysed the current network",              status: "done",    detail: "42 facilities, 120 demand zones, 380 lanes" },
    { step: 2, action: "Identified 2 capacity bottlenecks",         status: "done",    detail: "Delhi NCR DC at 94% utilisation, projected 108% in December" },
    { step: 3, action: "Identified 1 high-cost corridor",           status: "done",    detail: "Baddi → Guwahati corridor at ₹38/unit (2.1σ above average)" },
    { step: 4, action: "Generated 4 candidate interventions",       status: "done",    detail: "Flow rebalancing, capacity expansion, DC consolidation, new DC" },
    { step: 5, action: "Tested 4 scenarios through MILP optimiser", status: "done",    detail: "All 4 scenarios evaluated — 3 feasible, 1 fails SLA target" },
    { step: 6, action: "Rejected DC Consolidation on SLA grounds",  status: "done",    detail: "SLA drops to 88.4% — below 95% threshold" },
    { step: 7, action: "Stress-tested leading options under +15% demand", status: "done", detail: "Flow Rebalancing passes all stress tests" },
    { step: 8, action: "Found the most robust configuration",       status: "done",    detail: "Flow Rebalancing: −7.8% cost, 96.7% SLA, robust under stress" },
  ],
  toolCalls: [
    { tool: "get_network_summary()",       result: "42 facilities, 380 lanes, total demand 24,400 units/day" },
    { tool: "get_bottlenecks()",           result: "DC_DELHI at 94% util, projected breach Dec'26" },
    { tool: "get_forecast(region='North')",result: "+14% demand growth, breach at 10,800 units in Dec" },
    { tool: "run_baseline()",              result: "Total cost ₹12.84L/day, SLA 91.2%" },
    { tool: "optimize_current_footprint()",result: "Optimised to ₹11.98L/day, SLA 94.8%" },
    { tool: "run_scenario('rebalance')",   result: "₹11.84L/day, SLA 96.7% — FEASIBLE" },
    { tool: "run_scenario('expand')",      result: "₹12.46L/day, SLA 97.1% — FEASIBLE (high CapEx)" },
    { tool: "run_scenario('consolidate')", result: "₹11.42L/day, SLA 88.4% — REJECTED (SLA < 95%)" },
    { tool: "run_scenario('new_dc')",      result: "₹12.15L/day, SLA 97.8% — FEASIBLE (high CapEx)" },
    { tool: "run_sensitivity('demand', +15%)", result: "Rebalance: PASS | Consolidate: FAIL" },
    { tool: "run_resilience('DC_DELHI')",  result: "Rebalance handles facility disruption — reroutes to Kolkata" },
    { tool: "compare_scenarios()",         result: "Rebalance is optimal on cost-adjusted-risk basis" },
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
  { tier: 1, label: "INFORM",        description: "Low-risk informational insight. No approval required.",       criteria: "Value at stake < ₹5L, fully reversible, no SLA impact", color: "#22c55e" },
  { tier: 2, label: "PROPOSE",       description: "AI recommends and prepares action. Human approval required.", criteria: "Value at stake ₹5L–₹50L, or SLA impact, or partial reversibility", color: "#f59e0b" },
  { tier: 3, label: "HUMAN DECISION", description: "High-impact structural decision. AI analyses, cannot execute.", criteria: "Close/open DC, major contract change, CapEx > ₹50L", color: "#dc2626" },
];

// ─── SYSTEM STATUS ──────────────────────────────────────────
export const SYSTEM_STATUS = {
  data: { facilities: 42, demandZones: 120, lanes: 380, historicalPeriods: 24, qualityPct: 98.4 },
  forecast: { model: "Enhanced Demand Forecast (ARIMA + external signals)", horizon: "6 months", lastUpdated: "2026-08-15" },
  optimisation: { solver: "PuLP / HiGHS (MILP)", status: "Optimal", lastRun: "2026-08-18T14:22:00" },
  ai: { agentStatus: "Active", model: "Configured LLM Provider", lastAction: "Stress-tested leading option" },
};


// ─── PERIODS ────────────────────────────────────────────────
export const PERIODS = [
  { id: "AUG_2026", label: "1 Aug – 31 Aug 2026", short: "Aug 2026", prevId: "JUL_2026" },
  { id: "JUL_2026", label: "1 Jul – 31 Jul 2026", short: "Jul 2026", prevId: "JUN_2026" },
  { id: "Q3_2026",  label: "Q3 2026 (Jul – Sep)", short: "Q3 2026",  prevId: "Q2_2026" },
  { id: "Q2_2026",  label: "Q2 2026 (Apr – Jun)", short: "Q2 2026",  prevId: "Q1_2026" },
];

// ─── FACILITY KPIs BY PERIOD ────────────────────────────────
// Each facility + period combination has its own KPI snapshot.
// "prev" values are for the comparison period.
export const FACILITY_KPIS = {
  DC_DELHI: {
    AUG_2026: {
      utilisation: { value: 94.0, capacity: 10000, unit: "units/day", prev: 88.0, status: "critical" },
      sla:         { value: 96.7, target: 95, prev: 94.9, status: "normal" },
      totalCost:   { value: 1184000, prev: 1223000, status: "normal" },
      inventoryDays: { value: 11.2, prev: 12.1, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 88.0, capacity: 10000, unit: "units/day", prev: 84.0, status: "warning" },
      sla:         { value: 94.9, target: 95, prev: 95.1, status: "warning" },
      totalCost:   { value: 1223000, prev: 1198000, status: "warning" },
      inventoryDays: { value: 12.1, prev: 11.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_MUMBAI: {
    AUG_2026: {
      utilisation: { value: 75.6, capacity: 9000, unit: "units/day", prev: 72.1, status: "normal" },
      sla:         { value: 95.8, target: 95, prev: 94.2, status: "normal" },
      totalCost:   { value: 980000, prev: 1010000, status: "normal" },
      inventoryDays: { value: 9.8, prev: 10.4, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 72.1, capacity: 9000, unit: "units/day", prev: 70.5, status: "normal" },
      sla:         { value: 94.2, target: 95, prev: 94.8, status: "warning" },
      totalCost:   { value: 1010000, prev: 995000, status: "normal" },
      inventoryDays: { value: 10.4, prev: 10.1, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_BENGALURU: {
    AUG_2026: {
      utilisation: { value: 74.7, capacity: 7500, unit: "units/day", prev: 71.2, status: "normal" },
      sla:         { value: 96.2, target: 95, prev: 95.5, status: "normal" },
      totalCost:   { value: 840000, prev: 860000, status: "normal" },
      inventoryDays: { value: 10.5, prev: 11.0, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 71.2, capacity: 7500, unit: "units/day", prev: 69.8, status: "normal" },
      sla:         { value: 95.5, target: 95, prev: 95.0, status: "normal" },
      totalCost:   { value: 860000, prev: 845000, status: "normal" },
      inventoryDays: { value: 11.0, prev: 10.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_KOLKATA: {
    AUG_2026: {
      utilisation: { value: 53.3, capacity: 6000, unit: "units/day", prev: 51.0, status: "normal" },
      sla:         { value: 93.1, target: 95, prev: 92.4, status: "warning" },
      totalCost:   { value: 520000, prev: 530000, status: "normal" },
      inventoryDays: { value: 14.3, prev: 14.8, status: "warning" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 51.0, capacity: 6000, unit: "units/day", prev: 49.5, status: "normal" },
      sla:         { value: 92.4, target: 95, prev: 93.0, status: "warning" },
      totalCost:   { value: 530000, prev: 525000, status: "normal" },
      inventoryDays: { value: 14.8, prev: 14.5, status: "warning" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  DC_GUWAHATI: {
    AUG_2026: {
      utilisation: { value: 52.5, capacity: 4000, unit: "units/day", prev: 50.0, status: "normal" },
      sla:         { value: 91.4, target: 95, prev: 90.8, status: "warning" },
      totalCost:   { value: 310000, prev: 320000, status: "normal" },
      inventoryDays: { value: 16.1, prev: 16.8, status: "warning" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 50.0, capacity: 4000, unit: "units/day", prev: 48.2, status: "normal" },
      sla:         { value: 90.8, target: 95, prev: 91.2, status: "warning" },
      totalCost:   { value: 320000, prev: 315000, status: "normal" },
      inventoryDays: { value: 16.8, prev: 16.5, status: "warning" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_BADDI: {
    AUG_2026: {
      utilisation: { value: 93.3, capacity: 12000, unit: "units/day", prev: 88.0, status: "critical" },
      sla:         { value: 97.1, target: 95, prev: 96.2, status: "normal" },
      totalCost:   { value: 1420000, prev: 1380000, status: "warning" },
      inventoryDays: { value: 8.5, prev: 9.0, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 88.0, capacity: 12000, unit: "units/day", prev: 85.0, status: "warning" },
      sla:         { value: 96.2, target: 95, prev: 95.8, status: "normal" },
      totalCost:   { value: 1380000, prev: 1350000, status: "normal" },
      inventoryDays: { value: 9.0, prev: 8.8, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_PUNE: {
    AUG_2026: {
      utilisation: { value: 78.0, capacity: 10000, unit: "units/day", prev: 75.0, status: "normal" },
      sla:         { value: 96.8, target: 95, prev: 96.0, status: "normal" },
      totalCost:   { value: 1100000, prev: 1120000, status: "normal" },
      inventoryDays: { value: 10.0, prev: 10.5, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 75.0, capacity: 10000, unit: "units/day", prev: 73.0, status: "normal" },
      sla:         { value: 96.0, target: 95, prev: 95.5, status: "normal" },
      totalCost:   { value: 1120000, prev: 1100000, status: "normal" },
      inventoryDays: { value: 10.5, prev: 10.2, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_HYDERABAD: {
    AUG_2026: {
      utilisation: { value: 76.3, capacity: 8000, unit: "units/day", prev: 73.0, status: "normal" },
      sla:         { value: 95.9, target: 95, prev: 95.2, status: "normal" },
      totalCost:   { value: 890000, prev: 910000, status: "normal" },
      inventoryDays: { value: 11.8, prev: 12.3, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 73.0, capacity: 8000, unit: "units/day", prev: 71.5, status: "normal" },
      sla:         { value: 95.2, target: 95, prev: 95.0, status: "normal" },
      totalCost:   { value: 910000, prev: 895000, status: "normal" },
      inventoryDays: { value: 12.3, prev: 12.0, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
  PLT_KOLKATA: {
    AUG_2026: {
      utilisation: { value: 70.0, capacity: 6000, unit: "units/day", prev: 67.0, status: "normal" },
      sla:         { value: 94.5, target: 95, prev: 93.8, status: "warning" },
      totalCost:   { value: 580000, prev: 590000, status: "normal" },
      inventoryDays: { value: 13.2, prev: 13.8, status: "normal" },
      prevLabel: "1 Jul – 31 Jul 2026",
    },
    JUL_2026: {
      utilisation: { value: 67.0, capacity: 6000, unit: "units/day", prev: 65.0, status: "normal" },
      sla:         { value: 93.8, target: 95, prev: 94.0, status: "warning" },
      totalCost:   { value: 590000, prev: 580000, status: "normal" },
      inventoryDays: { value: 13.8, prev: 13.5, status: "normal" },
      prevLabel: "1 Jun – 30 Jun 2026",
    },
  },
};

// ─── HOME INSIGHTS BY FACILITY ──────────────────────────────
// Each facility has context-specific insights. The recommendation insight is shared.
export const HOME_INSIGHTS = {
  DC_DELHI: [
    {
      id: "INS_CAP_RISK",
      icon: "⚠️", iconBg: "#fef2f2", iconColor: "#dc2626",
      title: "Capacity risk at Delhi NCR DC",
      subtitle: "Utilisation is projected to reach 108% in December.",
      impact: "High Impact", impactColor: "#dc2626",
      why: "Demand forecast shows 14.2% growth over the next 4 months, exceeding available capacity starting October.",
      action: "Investigate", provenance: "FORECAST",
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
        nextAction: "Review scenario",
      },
    },
    {
      id: "INS_COST_CONC",
      icon: "💰", iconBg: "#fffbeb", iconColor: "#d97706",
      title: "Cost opportunity identified",
      subtitle: "18% of transport cost is concentrated on 2 lanes.",
      impact: "Medium Impact", impactColor: "#d97706",
      why: "High concentration of spend on Baddi–Delhi NCR and Baddi–Jaipur lanes vs. network average.",
      action: "See why", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Two corridors (Baddi→Delhi NCR at ₹12/unit and Baddi→Jaipur at ₹22/unit via Delhi) account for 18% of total network transport cost.",
        whyItMatters: "Cost concentration creates risk if either corridor is disrupted and limits negotiation leverage with carriers.",
        evidence: [
          { label: "Baddi→Delhi NCR cost share", value: "12.4%", provenance: "MODEL FACT" },
          { label: "Baddi→Jaipur cost share", value: "5.6%", provenance: "MODEL FACT" },
          { label: "Network average lane share", value: "4.0%", provenance: "MODEL FACT" },
          { label: "Corridor concentration ratio", value: "4.5x above average", provenance: "MODEL FACT" },
        ],
        whatITested: ["Alternative routing via Kolkata DC", "Direct plant-to-market for Jaipur"],
        recommendation: "Consider flow rebalancing to reduce corridor concentration.",
        nextAction: "Explore scenarios",
      },
    },
    {
      id: "INS_SPARE_CAP",
      icon: "📈", iconBg: "#f0fdf4", iconColor: "#16a34a",
      title: "Underutilised capacity available",
      subtitle: "Kolkata DC has 41% spare capacity.",
      impact: "Opportunity", impactColor: "#16a34a",
      why: "Current throughput is 5,900 units/day vs capacity of 10,000 units/day.",
      action: "Explore", provenance: "MODEL FACT",
      detail: {
        whatIFound: "Kolkata DC is operating at only 53.3% utilisation with 2,800 units/day of spare capacity.",
        whyItMatters: "This spare capacity can absorb volume from overloaded facilities without requiring capital investment.",
        evidence: [
          { label: "Kolkata capacity", value: "6,000 u/d", provenance: "MODEL FACT" },
          { label: "Current throughput", value: "3,200 u/d", provenance: "MODEL FACT" },
          { label: "Spare capacity", value: "2,800 u/d (46.7%)", provenance: "MODEL FACT" },
          { label: "Handling cost", value: "₹3.5/unit (lowest)", provenance: "MODEL FACT" },
        ],
        whatITested: ["Redirect 800 units/day from Delhi NCR overflow"],
        recommendation: "Include Kolkata DC in flow rebalancing scenario.",
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
      detail: null, // Uses the existing RECOMMENDATION object
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

export function getInsightsForFacility(facilityId) {
  return HOME_INSIGHTS[facilityId] || HOME_INSIGHTS._default;
}

export function getKpisForFacility(facilityId, periodId) {
  const facilityKpis = FACILITY_KPIS[facilityId];
  if (!facilityKpis) return null;
  return facilityKpis[periodId] || facilityKpis["AUG_2026"] || null;
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
  if (value >= 1000)   return "₹" + (value / 1000).toFixed(1) + "K";
  return "₹" + value.toFixed(decimals);
}

export function formatNumber(value) {
  return value.toLocaleString("en-IN");
}

export function getUtilColor(pct) {
  if (pct >= 90) return "#dc2626";
  if (pct >= 75) return "#f59e0b";
  return "#22c55e";
}

export function getUtilLabel(pct) {
  if (pct >= 90) return "Critical";
  if (pct >= 75) return "Moderate";
  return "Healthy";
}

