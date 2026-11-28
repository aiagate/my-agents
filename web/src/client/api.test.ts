import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getTimeline, listConversations, sendChat } from "./api";
import type { ChatResponse } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(body: unknown, status = 200): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => jsonResponse(body, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("requestJson", () => {
  it("throws ApiError with the kind, message, and status of an error payload", async () => {
    stubFetch({ error: "validation", message: "message is required" }, 422);

    const cause = await getTimeline("default").catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(ApiError);
    expect(cause).toMatchObject({
      name: "ApiError",
      kind: "validation",
      message: "message is required",
      status: 422,
    });
  });

  it("defaults the kind to http_error when the payload has none", async () => {
    stubFetch(null, 503);

    await expect(listConversations()).rejects.toMatchObject({
      kind: "http_error",
      message: "Request failed with status 503",
      status: 503,
    });
  });

  it("returns the parsed chat response", async () => {
    const body: ChatResponse = {
      conversation_id: "default",
      reply: "Sora の返信",
      message_id: "01a2204a-0000-7000-8000-000000000000",
      surfaced_event_ids: [],
      memory_updates: [],
    };
    stubFetch(body);

    await expect(sendChat("default", "こんにちは")).resolves.toEqual(body);
  });
});

describe("payload validation", () => {
  it("rejects a malformed timeline payload as ApiError", async () => {
    stubFetch({ items: "not-an-array" });

    await expect(getTimeline("default")).rejects.toMatchObject({
      name: "ApiError",
      kind: "invalid_response",
    });
  });

  it("rejects a malformed conversation list payload as ApiError", async () => {
    stubFetch({ conversations: null });

    await expect(listConversations()).rejects.toBeInstanceOf(ApiError);
  });
});
