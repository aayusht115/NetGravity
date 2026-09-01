/**
 * NetGravity — Digital Twin State Mapper
 * ======================================
 * Transforms backend `NetworkStateView` snapshots into Leaflet and Three.js
 * visual node and corridor models.
 */

// City coordinate dictionary for canonical India facilities
const CITY_COORDS = {
  'BADDI': { lat: 30.96, lng: 76.79, city: 'Baddi', state: 'Himachal Pradesh', region: 'North' },
  'DELHI': { lat: 28.61, lng: 77.21, city: 'Delhi NCR', state: 'Delhi', region: 'North' },
  'PUNE': { lat: 18.52, lng: 73.86, city: 'Pune', state: 'Maharashtra', region: 'West' },
  'MUMBAI': { lat: 19.08, lng: 72.88, city: 'Mumbai', state: 'Maharashtra', region: 'West' },
  'HYDERABAD': { lat: 17.38, lng: 78.49, city: 'Hyderabad', state: 'Telangana', region: 'South' },
  'BENGALURU': { lat: 12.97, lng: 77.59, city: 'Bengaluru', state: 'Karnataka', region: 'South' },
  'CHENNAI': { lat: 13.08, lng: 80.27, city: 'Chennai', state: 'Tamil Nadu', region: 'South' },
  'KOLKATA': { lat: 22.57, lng: 88.36, city: 'Kolkata', state: 'West Bengal', region: 'East' },
  'GUWAHATI': { lat: 26.14, lng: 91.74, city: 'Guwahati', state: 'Assam', region: 'Northeast' },
  'AHMEDABAD': { lat: 23.03, lng: 72.57, city: 'Ahmedabad', state: 'Gujarat', region: 'West' },
  'JAIPUR': { lat: 26.91, lng: 75.79, city: 'Jaipur', state: 'Rajasthan', region: 'North' },
  'LUCKNOW': { lat: 26.85, lng: 80.95, city: 'Lucknow', state: 'Uttar Pradesh', region: 'North' },
};

export function deoverlapNodes(nodeArrays) {
  const groups = new Map();
  nodeArrays.flat().forEach((n) => {
    if (typeof n.lat !== 'number' || typeof n.lng !== 'number') return;
    const key = `${n.lat.toFixed(4)},${n.lng.toFixed(4)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  });
  const FAN_RADIUS = 0.9;
  groups.forEach((nodes) => {
    if (nodes.length < 2) return;
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / nodes.length;
      n.lat += FAN_RADIUS * Math.sin(angle);
      n.lng += FAN_RADIUS * Math.cos(angle);
    });
  });
}

export function mapTwinStateToFrontend(twinState) {
  if (!twinState) return null;

  const plants = [];
  const dcs = [];
  const markets = [];
  const lanes = [];

  // 1. Map Facilities
  const facilities = twinState.facilities || [];
  facilities.forEach((f) => {
    const fid = f.facility_id || f.id;
    const role = (f.role || '').toUpperCase();
    const cityKey = Object.keys(CITY_COORDS).find(k => fid.toUpperCase().includes(k)) || 'DELHI';
    const geo = CITY_COORDS[cityKey] || { lat: 28.61, lng: 77.21, city: fid, state: 'India', region: 'North' };

    const item = {
      id: fid,
      name: f.facility_name || f.name || fid,
      city: geo.city,
      state: geo.state,
      lat: f.lat || geo.lat,
      lng: f.lng || geo.lng,
      capacity: f.capacity_units || f.capacity || 10000,
      throughput: f.throughput_units || f.throughput || 0,
      region: geo.region,
      status: f.is_open ? 'EXISTING' : 'CLOSED',
      utilPct: f.utilization_pct || (f.capacity_units > 0 ? (f.throughput_units / f.capacity_units * 100) : 0),
    };

    if (role.includes('PLANT') || role.includes('SUPPLIER') || fid.startsWith('PLT')) {
      plants.push({ ...item, type: 'Plant' });
    } else {
      dcs.push({
        ...item,
        type: 'DC',
        fixedCost: f.fixed_cost || 120,
        handlingCost: f.handling_cost || 4.2,
      });
    }
  });

  // 2. Map Markets (if provided or extracted from flows/demands)
  const demands = twinState.demands || [];
  if (demands.length > 0) {
    demands.forEach((d) => {
      const mid = d.market_id || d.id;
      const cityKey = Object.keys(CITY_COORDS).find(k => mid.toUpperCase().includes(k)) || 'DELHI';
      const geo = CITY_COORDS[cityKey] || { lat: 28.70, lng: 77.10, city: mid, region: 'North' };
      markets.push({
        id: mid,
        name: geo.city,
        lat: d.lat || geo.lat,
        lng: d.lng || geo.lng,
        demand: d.quantity || d.demand || 0,
        slaDays: d.sla_days || 2,
        priority: d.priority || 'High',
        region: geo.region,
      });
    });
  }

  // 3. Map Flows
  const flows = twinState.flows || [];
  flows.forEach((fl) => {
    if ((fl.flow_units || fl.flow || 0) > 1e-4) {
      lanes.push({
        from: fl.origin_id || fl.from,
        to: fl.destination_id || fl.to,
        cost: fl.transport_cost || fl.cost || 10,
        distance: fl.distance_km || fl.distance || 300,
        leadTime: fl.lead_time_days || fl.leadTime || 1.0,
        flow: fl.flow_units || fl.flow || 0,
        mode: fl.mode || 'ROAD',
      });
    }
  });

  deoverlapNodes([plants, dcs, markets]);

  return {
    plants,
    dcs,
    markets,
    lanes,
    facilities: [...plants, ...dcs],
    stateId: twinState.state_id,
    snapshotId: twinState.snapshot_id,
  };
}
