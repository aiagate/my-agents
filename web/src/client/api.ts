import type {
  ChatResponse,
  ConversationListResponse,
  PushSubscriptionRequest,
  TimelineResponse,
  VapidPublicKeyResponse,
} from "./types";
export class ApiError extends Error {
  readonly kind: string;
  readonly status: number;
  constructor(kind: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}
async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const record: { error?: unknown; message?: unknown } =
      typeof body === "object" && body !== null
        ? (body as { error?: unknown; message?: unknown })
        : {};
    const kind =
      typeof record.error === "string" && record.error !== ""
        ? record.error
        : "http_error";
    const message =
      typeof record.message === "string" && record.message !== ""
        ? record.message
        : `Request failed with status ${response.status}`;
    throw new ApiError(kind, message, response.status);
  }
  return body as T;
}
function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
export async function listConversations(
  limit = 100,
  signal?: AbortSignal,
): Promise<ConversationListResponse> {
  const data = await requestJson<ConversationListResponse | null>(
    `/conversations?limit=${encodeURIComponent(String(limit))}`,
    { signal },
  );
  if (data === null || !Array.isArray(data.conversations)) {
    throw new ApiError(
      "invalid_response",
      "Conversation list response was malformed.",
      200,
    );
  }
  return data;
}
export async function getTimeline(
  conversationId: string,
  limit = 60,
  signal?: AbortSignal,
): Promise<TimelineResponse> {
  const query = new URLSearchParams({
    conversation_id: conversationId,
    limit: String(limit),
  });
  const data = await requestJson<TimelineResponse | null>(
    `/timeline?${query.toString()}`,
    { signal },
  );
  if (data === null || !Array.isArray(data.items)) {
    throw new ApiError("invalid_response", "Timeline response was malformed.", 200);
  }
  return data;
}
export function sendChat(
  conversationId: string,
  message: string,
): Promise<ChatResponse> {
  return requestJson<ChatResponse>(
    "/chat",
    jsonInit("POST", { conversation_id: conversationId, message }),
  );
}
export function getVapidPublicKey(): Promise<VapidPublicKeyResponse> {
  return requestJson<VapidPublicKeyResponse>("/push/vapid-public-key");
}
export async function registerPushSubscription(
  subscription: PushSubscriptionRequest,
): Promise<void> {
  await requestJson("/push/subscriptions", jsonInit("POST", subscription));
}
export async function unregisterPushSubscription(
  endpoint: string,
): Promise<void> {
  await requestJson("/push/subscriptions", jsonInit("DELETE", { endpoint }));
}
