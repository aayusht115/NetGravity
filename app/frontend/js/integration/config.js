/**
 * NetGravity — Integration Environment Configuration
 * ==================================================
 * Central configuration for API endpoints, timeouts, and feature flags.
 */

export const CONFIG = {
  // Use relative root in browser or VITE_API_BASE_URL if configured
  API_BASE_URL: (typeof window !== 'undefined' && window.ENV_API_BASE_URL)
    ? window.ENV_API_BASE_URL
    : (typeof window !== 'undefined' && window.location && window.location.origin)
      ? window.location.origin
      : 'http://localhost:5050',
  REQUEST_TIMEOUT_MS: 30000,

  /**
   * Timeout for a request that may be waiting on a MILP solve.
   *
   * KPI, baseline and scenario endpoints run the optimiser. On a real client
   * network that is twenty to forty seconds and can be minutes on a large one,
   * so the 30-second default aborted them — and aborting the fetch does not
   * stop the solve. The user was shown "Analysis unavailable: request timed
   * out" for work that succeeded, and the server paid for it anyway.
   *
   * Raised from 300s after measuring the FIRST scenario on a 20-facility
   * network at 5m19s. A scenario run assesses facility resilience, which is
   * one MILP solve per facility; the batch is cached by material fingerprint,
   * so the second scenario on that network took 31s — but nothing before it
   * asks for that batch, and the run that pays for it is the first one a user
   * makes. 300s aborted it with about twenty seconds to go.
   *
   * This is patience, not correctness. Whatever the number, a solve can
   * outlast it, so the scenario path also RECOVERS from an abort rather than
   * calling it a failure — see `findScenarioCreatedSince`.
   */
  SOLVE_TIMEOUT_MS: 600000,

  /**
   * Timeout for reading an uploaded file.
   *
   * `uploadAndParse` used to inherit REQUEST_TIMEOUT_MS, which is the budget
   * for `GET /api/projects`. The server reads every sheet, classifies every
   * column, assembles the network structure and measures quality before it
   * answers: on a 1.4 MB, 17-sheet, 51,670-row workbook that is about three
   * seconds here, and it scales with the file rather than with anything the
   * 30-second default was chosen for.
   *
   * Like SOLVE_TIMEOUT_MS this is patience, not correctness — the parse can
   * outlast any number, so an abort is answered by looking for the preview
   * the server stored rather than by declaring the file unreadable.
   */
  PARSE_TIMEOUT_MS: 180000,
  DEMO_MODE_FALLBACK: false, // Strict: never silently fall back to mock data in production path

  /**
   * Basemap tile URL. EMPTY BY DEFAULT, and deliberately so.
   *
   * The 2D map used to load its basemap unconditionally from
   * `basemaps.cartocdn.com`. That is a third-party service with an anonymous
   * quota, and when a network or a quota refuses it the service does not fail —
   * it serves a valid image with "API key required" printed across it. Leaflet
   * has nothing to detect: the HTTP status is 200 and the tile decodes. The
   * result is a map that renders the client's own facilities on top of a
   * watermark telling them their software is misconfigured.
   *
   * The map now draws vector country outlines bundled with the application
   * (`js/world-basemap.js` — Natural Earth 110m, public domain, the same rings
   * the 3D twin triangulates its ground from), so it needs no external
   * service, no key and no internet connection, it covers every network
   * anywhere rather than one country, and it cannot change under the
   * application's feet.
   *
   * Set this to a tile template — your own tile server, or a keyed provider
   * with the key already in the URL — to use live tiles instead. Nothing else
   * needs to change.
   *
   *   window.ENV_MAP_TILE_URL = 'https://tiles.example.com/{z}/{x}/{y}.png';
   */
  MAP_TILE_URL: (typeof window !== 'undefined' && window.ENV_MAP_TILE_URL) || '',
  MAP_TILE_ATTRIBUTION:
    (typeof window !== 'undefined' && window.ENV_MAP_TILE_ATTRIBUTION) || '',
};
