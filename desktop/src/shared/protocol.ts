export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 1_048_576;

export type RequestMessage = {
  type: "request";
  protocolVersion: number;
  requestId: string;
  method: string;
  params: Record<string, unknown>;
};

export type ResponseMessage = {
  type: "response";
  protocolVersion: number;
  requestId: string;
  result?: Record<string, unknown>;
  error?: { code: string; message: string };
};

export type EventMessage = {
  type: "event";
  protocolVersion: number;
  eventId: string;
  sequence: number;
  event: string;
  payload: Record<string, unknown>;
};

export type HostMessage = ResponseMessage | EventMessage;

export type HostState = "starting" | "ready" | "error" | "stopped";

export type RuntimeStatus = {
  state: HostState;
  runtimeVersion?: string;
  providerConfigured?: boolean;
  providerProfiles?: Array<{
    name: string;
    model: string | null;
    default: boolean;
    credentialAvailable: boolean;
    baseUrl?: string;
    streaming?: boolean;
    configuredCapabilities?: string[];
    protocol?: string;
    wireApi?: "chat_completions" | "responses";
  }>;
  error?: string;
  exitCode?: number | null;
  recoveredAfterCrash?: boolean;
  workspace?: string | null;
};

export type ProviderTestFailureKind =
  | "authentication_failed"
  | "access_denied"
  | "model_or_endpoint_not_found"
  | "rate_limited"
  | "provider_unavailable"
  | "http_error"
  | "network_error"
  | "invalid_response"
  | "empty_response";

export type ProviderTestResult = {
  reachable: boolean;
  model: string;
  checkType: "chat_completions" | "responses";
  statusCode: number | null;
  failureKind: ProviderTestFailureKind | null;
};

export type WorkspaceEntry = { path: string; name: string; openedAt?: string };
export type WorkspaceFile = { path: string; size: number };
export type SessionSummary = {
  session_id: string;
  modified_at?: string;
  message_count?: number;
  last_prompt?: string;
  error?: string;
};
export type SessionMessage = Record<string, unknown> & { role?: string; content?: string | null };
export type SessionEvent = {
  seq: number;
  kind: string;
  occurred_at: string;
  payload: Record<string, unknown>;
};
export type ProviderProfileInput = {
  name: string;
  baseUrl: string;
  model: string;
  wireApi: "chat_completions" | "responses";
  apiKey?: string;
  clearCredential?: boolean;
  default?: boolean;
};

export type PermissionRequestEvent = {
  approvalId: string;
  taskId: string;
  toolName: string;
  permission: string;
  arguments: Record<string, unknown>;
  workspace: string;
  operation?: string;
  operationId?: string;
  command?: string;
  targetFiles?: string[];
  scope?: string;
  networkRequired?: boolean;
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseHostMessage(line: string): HostMessage {
  const value: unknown = JSON.parse(line);
  if (!isRecord(value) || value.protocolVersion !== PROTOCOL_VERSION) {
    throw new Error("Runtime returned an unsupported protocol version.");
  }

  if (value.type === "response") {
    if (typeof value.requestId !== "string" || value.requestId.length === 0 || value.requestId.length > 128) {
      throw new Error("Runtime returned an invalid response identifier.");
    }
    const hasError = value.error !== undefined;
    const hasResult = value.result !== undefined;
    if (hasError === hasResult) {
      throw new Error("Runtime returned a malformed response payload.");
    }
    if (value.result !== undefined && !isRecord(value.result)) {
      throw new Error("Runtime returned an invalid result payload.");
    }
    if (
      value.error !== undefined &&
      (!isRecord(value.error) || typeof value.error.code !== "string" || typeof value.error.message !== "string")
    ) {
      throw new Error("Runtime returned an invalid error payload.");
    }
    return value as ResponseMessage;
  }

  if (
    value.type === "event" &&
    typeof value.eventId === "string" &&
    value.eventId.length > 0 &&
    Number.isSafeInteger(value.sequence) &&
    (value.sequence as number) > 0 &&
    typeof value.event === "string" &&
    value.event.length > 0 &&
    isRecord(value.payload)
  ) {
    return value as EventMessage;
  }

  throw new Error("Runtime returned a malformed protocol frame.");
}
