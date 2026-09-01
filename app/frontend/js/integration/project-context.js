/**
 * NetGravity — Active Project Context
 * ===================================
 * The single place the frontend records which project the user is working in.
 *
 * Every authoritative request is scoped by this id. Before Phase 10.0 there was
 * no such concept on either side: the backend served one process-global
 * orchestrator bound to a synthetic network, so no screen ever needed to say
 * which project it was asking about.
 */

const STORAGE_KEY = 'ng_active_project_id';

let _activeProjectId = null;
const _listeners = new Set();

try {
  _activeProjectId = localStorage.getItem(STORAGE_KEY);
} catch (e) {
  // Private mode / storage disabled — an in-memory context still works.
}

export function getActiveProjectId() {
  return _activeProjectId;
}

/**
 * The snapshot the active project is bound to.
 *
 * Held here because more than one caller needs it and only hydration learns
 * it. The assistant in particular was posting to /orchestrator/chat with no
 * snapshot, so the orchestrator answered from the bundled synthetic network —
 * replies named DC_CENTRAL, DC_EAST and DC_NORTH_NEW to users whose facilities
 * are nothing of the sort.
 */
let _activeSnapshotId = null;

export function getActiveSnapshotId() {
  return _activeSnapshotId;
}

export function setActiveSnapshotId(snapshotId) {
  _activeSnapshotId = snapshotId || null;
}

export function hasActiveProject() {
  return Boolean(_activeProjectId);
}

/**
 * Switch project. Notifies subscribers so every screen can clear and refetch —
 * showing one project's numbers under another project's name is exactly the
 * stale-data failure the production brief forbids.
 */
export function setActiveProject(projectId) {
  if (_activeProjectId === projectId) return;
  _activeProjectId = projectId || null;
  // Belongs to the project that was just left.
  _activeSnapshotId = null;
  try {
    if (_activeProjectId) localStorage.setItem(STORAGE_KEY, _activeProjectId);
    else localStorage.removeItem(STORAGE_KEY);
  } catch (e) {
    // Non-fatal.
  }
  _listeners.forEach((fn) => {
    try {
      fn(_activeProjectId);
    } catch (err) {
      console.error('project change listener failed:', err);
    }
  });
}

export function onProjectChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}
