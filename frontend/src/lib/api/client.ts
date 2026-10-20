import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Generated from the FastAPI OpenAPI schema: `npm run gen:api`. */
export type Schemas = components["schemas"];
export type IncidentSummary = Schemas["IncidentSummary"];
export type IncidentDetail = Schemas["IncidentDetail"];
export type IncidentStatus = IncidentSummary["status"];
export type TraceEvent = Schemas["TraceEvent"];
export type RootCauseReport = Schemas["RootCauseReport"];
export type ProposedAction = Schemas["ProposedAction"];
export type PlanResult = Schemas["PlanResult"];
export type AuditEntry = Schemas["AuditEntry"];

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

export const api = createClient<paths>({ baseUrl: API_URL });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Turn an openapi-fetch result into data-or-throw with the backend's `detail` message. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined && result.response.ok) return result.data;
  throw new ApiError(result.response.status, errorMessage(result.error, result.response));
}

export function errorMessage(error: unknown, response?: Response): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d)))
        .join("; ");
    }
  }
  return response ? `Request failed (${response.status})` : "Request failed";
}

export function approverHeaders(key: string | null): Record<string, string> {
  return key ? { "X-API-Key": key } : {};
}
