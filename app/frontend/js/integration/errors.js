/**
 * NetGravity — Normalized Application Error Hierarchy
 * ===================================================
 * Defines typed errors covering network, auth, validation, and domain engine states.
 */

export const ErrorCode = {
  NETWORK_ERROR: 'NETWORK_ERROR',
  AUTH_ERROR: 'AUTH_ERROR',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  NOT_FOUND: 'NOT_FOUND',
  CONFLICT: 'CONFLICT',
  INFEASIBLE: 'INFEASIBLE',
  INSUFFICIENT_EVIDENCE: 'INSUFFICIENT_EVIDENCE',
  NOT_COMPUTABLE: 'NOT_COMPUTABLE',
  BACKEND_ERROR: 'BACKEND_ERROR',
  TIMEOUT: 'TIMEOUT',
};

export class ApplicationError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'ApplicationError';
    this.code = code;
    this.details = details;
    this.timestamp = new Date().toISOString();
  }

  static fromHttp(status, errorBody = {}) {
    const err = errorBody.error || errorBody;
    const code = err.code || (
      status === 401 ? ErrorCode.AUTH_ERROR :
      status === 403 ? ErrorCode.AUTH_ERROR :
      status === 404 ? ErrorCode.NOT_FOUND :
      status === 409 ? ErrorCode.INFEASIBLE :
      status === 422 ? ErrorCode.VALIDATION_ERROR :
      status >= 500 ? ErrorCode.BACKEND_ERROR :
      ErrorCode.BACKEND_ERROR
    );
    const message = err.message || `Request failed with status ${status}`;
    return new ApplicationError(code, message, err.details || {});
  }
}
