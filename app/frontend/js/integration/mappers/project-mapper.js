/**
 * NetGravity — Project Workspace Mapper
 * =====================================
 * Backend `ProjectRecord` → the shape the project screens render.
 */

/** Human-readable relative time from an epoch-seconds timestamp. */
function relativeTime(epochSeconds) {
  if (!epochSeconds) return 'Recently';
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 60) return 'Just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
  const weeks = Math.floor(days / 7);
  return `${weeks} week${weeks === 1 ? '' : 's'} ago`;
}

/** Who owns this workspace, from the signed-in account rather than a guess. */
function ownerLabel(p) {
  if (p.is_demo || p.owner_id === '__system__') return 'Sample';
  const me = (typeof window !== 'undefined' && typeof window.getCurrentUser === 'function')
    ? window.getCurrentUser() : null;
  // `/api/auth/me` projects the account as `id`; the project record names its
  // owner `owner_id`. Both are accepted so the comparison does not silently
  // fail and label the user's own workspace "Shared".
  const myId = me && (me.id || me.user_id);
  if (myId && p.owner_id && myId === p.owner_id) return 'You';
  if (!p.owner_id) return 'Unknown';
  return myId ? 'Shared' : 'You';
}

export function mapProjectRecord(p) {
  if (!p) return null;
  return {
    id: p.id,
    name: p.name || 'Untitled Project',
    // The server's answer, whatever it is. This defaulted to 'India',
    // which put an India label back onto a project the backend had
    // deliberately left unstated — the last of four places that made
    // an unanswered question into a stated fact.
    region: p.region || '',
    // How the region was arrived at: 'user', 'inferred', or '' when it
    // is not yet known. A derived label is shown as derived.
    regionSource: p.region_source || '',
    client: p.client || '',
    description: p.description || '',
    updated: p.updated || relativeTime(p.updated_at),
    rank: p.rank || 1,
    // `p.owner_id ? 'You' : 'You'` — both branches said "You", so the bundled
    // demo workspace, owned by `__system__` and shared by every account, was
    // listed as belonging to whoever was looking at it. The signed-in user's
    // own id decides, and anything else is named for what it is.
    owner: ownerLabel(p),
    // The server's own status: "Awaiting data" until a network is bound,
    // "Analysis ready" afterwards. Previously this defaulted to
    // "In progress", which made an empty workspace look like a live one.
    status: p.status || (p.has_network ? 'Analysis ready' : 'Awaiting data'),
    // null, not the string 'default'. A project without a bound network has no
    // snapshot, and the UI needs to be able to tell the difference.
    snapshotId: p.snapshot_id || null,
    hasNetwork: Boolean(p.has_network),
    isDemo: Boolean(p.is_demo),
  };
}
