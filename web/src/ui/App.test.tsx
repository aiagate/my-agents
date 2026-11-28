import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationSummary, TimelineItem } from "../client/types";
import { App } from "./App";

interface ServerState {
  conversations: ConversationSummary[];
  timelines: Map<string, TimelineItem[]>;
  posts: Array<{ conversation_id: string; message: string }>;
  vapidFailure: boolean;
  vapidPublicKey: string | null;
  chatFailure: boolean;
  timelineFailure: boolean;
  pushDeleteFailure: boolean;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function summary(
  conversationId: string,
  preview: string,
  overrides: Partial<ConversationSummary> = {},
): ConversationSummary {
  return {
    conversation_id: conversationId,
    epoch: 1,
    created_at: "2025-01-01T00:00:00+00:00",
    updated_at: "2025-01-02T00:00:00+00:00",
    message_count: 2,
    event_count: 1,
    last_message_preview: preview,
    last_message_origin: "reply",
    ...overrides,
  };
}

function turn(
  conversationId: string,
  origin: TimelineItem["origin"],
  content: string,
  overrides: Partial<TimelineItem> = {},
): TimelineItem {
  return {
    item_id: `${conversationId}-${origin}-${content.slice(0, 8)}`,
    kind: "turn",
    conversation_id: conversationId,
    content,
    created_at: "2025-01-01T00:00:00+00:00",
    event_id: null,
    origin,
    ...overrides,
  };
}

function installFetch(state: ServerState): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input.toString();
    const url = new URL(raw, "http://localhost");
    const method = init?.method ?? "GET";

    if (method === "GET" && url.pathname === "/conversations") {
      return jsonResponse({ conversations: state.conversations });
    }
    if (method === "GET" && url.pathname === "/timeline") {
      if (state.timelineFailure) {
        return jsonResponse(
          { error: "storage", message: "timeline unavailable" },
          500,
        );
      }
      const id = url.searchParams.get("conversation_id") ?? "default";
      return jsonResponse({ items: state.timelines.get(id) ?? [] });
    }
    if (method === "GET" && url.pathname === "/push/vapid-public-key") {
      return state.vapidFailure
        ? jsonResponse({ error: "vapid_error", message: "vapid unavailable" }, 500)
        : jsonResponse({ public_key: state.vapidPublicKey });
    }
    if (method === "POST" && url.pathname === "/push/subscriptions") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        endpoint?: string;
      };
      return jsonResponse(
        { status: "subscribed", endpoint: body.endpoint ?? "" },
        201,
      );
    }
    if (method === "DELETE" && url.pathname === "/push/subscriptions") {
      return state.pushDeleteFailure
        ? jsonResponse({ error: "storage", message: "delete failed" }, 500)
        : jsonResponse({ deleted: true });
    }
    if (method === "POST" && url.pathname === "/chat") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        conversation_id?: string;
        message?: string;
      };
      const conversationId = body.conversation_id ?? "default";
      const message = body.message ?? "";
      state.posts.push({ conversation_id: conversationId, message });
      if (state.chatFailure) {
        return jsonResponse({ error: "chat_failed", message: "chat unavailable" }, 500);
      }
      const items = state.timelines.get(conversationId) ?? [];
      items.push(turn(conversationId, "user", message));
      items.push(
        turn(conversationId, "reply", "Sora の返信", {
          item_id: `${conversationId}-reply-${state.posts.length}`,
          created_at: "2025-01-02T00:00:00+00:00",
        }),
      );
      state.timelines.set(conversationId, items);
      return jsonResponse({
        conversation_id: conversationId,
        reply: "Sora の返信",
        message_id: `${conversationId}-reply-${state.posts.length}`,
        surfaced_event_ids: [],
        memory_updates: [],
      });
    }
    return jsonResponse({ error: "not_found", message: raw }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const PUSH_P256DH = new Uint8Array([1, 2, 3]).buffer;
const PUSH_AUTH = new Uint8Array([4, 5, 6]).buffer;

function fakePushSubscription(
  applicationServerKey: ArrayBuffer = new Uint8Array([1, 2, 3]).buffer,
): PushSubscription {
  return {
    endpoint: "https://push.example.test/subscription",
    options: { userVisibleOnly: true, applicationServerKey },
    getKey: (name: string) => (name === "p256dh" ? PUSH_P256DH : PUSH_AUTH),
    unsubscribe: vi.fn(async () => true),
  } as unknown as PushSubscription;
}

function stubBrowser(subscription?: PushSubscription): void {
  const registration =
    subscription === undefined
      ? undefined
      : { pushManager: { getSubscription: vi.fn(async () => subscription) } };
  vi.stubGlobal("navigator", {
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    maxTouchPoints: 0,
    onLine: true,
    serviceWorker: {
      getRegistration: vi.fn(async () => registration),
      ready: Promise.resolve(registration),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    },
  });
  vi.stubGlobal("Notification", {
    permission: "default",
    requestPermission: vi.fn(async () => "default"),
  });
  vi.stubGlobal("PushManager", class {});
}

let state: ServerState;

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  stubBrowser();
  state = {
    conversations: [
      summary("default", "hello there"),
      summary("work", "plan the week"),
    ],
    timelines: new Map([
      ["default", []],
      ["work", [turn("work", "reply", "work reply")]],
    ]),
    posts: [],
    vapidFailure: false,
    vapidPublicKey: null,
    chatFailure: false,
    timelineFailure: false,
    pushDeleteFailure: false,
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("App", () => {
  it("renders the conversation list from the API", async () => {
    installFetch(state);
    render(<App />);

    expect(await screen.findByText("hello there")).toBeInTheDocument();
    expect(screen.getByText("plan the week")).toBeInTheDocument();
    expect(screen.getByText(/Sora との会話を始めましょう/)).toBeInTheDocument();
  });

  it("renders user, reply, and proactive messages as safe Markdown", async () => {
    state.timelines.set("default", [
      turn("default", "user", "こんにちは"),
      turn("default", "reply", "**bold** reply with <script>bad()</script>"),
      turn("default", "proactive", "プロアクティブな声", {
        event_id: "evt-9",
        item_id: "proactive-1",
      }),
    ]);
    installFetch(state);
    render(<App />);

    expect(await screen.findByText("bold")).toBeInTheDocument();
    expect(screen.getByText("こんにちは")).toBeInTheDocument();
    expect(screen.getByText("プロアクティブな声")).toBeInTheDocument();
    expect(screen.queryByText("proactive")).toBeNull();
    expect(screen.getByText(/<script>bad\(\)<\/script>/)).toBeInTheDocument();
    expect(document.querySelector(".prose script")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it("renders no proactive badge and keeps the user message before the reply", async () => {
    installFetch(state);
    render(<App />);

    expect(await screen.findByText("plan the week")).toBeInTheDocument();
    expect(screen.queryByText("proactive")).toBeNull();

    const input = await screen.findByLabelText("メッセージ");
    fireEvent.change(input, { target: { value: "新しい質問" } });
    fireEvent.click(screen.getByLabelText("送信"));

    const replyEl = await screen.findByText("Sora の返信");
    const userEl = screen.getByText("新しい質問");
    expect(
      Boolean(
        userEl.compareDocumentPosition(replyEl) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
  });

  it("sends a message through the chat API and shows the reply", async () => {
    installFetch(state);
    render(<App />);

    const input = await screen.findByLabelText("メッセージ");
    fireEvent.change(input, { target: { value: "新しい質問" } });
    fireEvent.click(screen.getByLabelText("送信"));

    await waitFor(() => {
      expect(state.posts).toEqual([
        { conversation_id: "default", message: "新しい質問" },
      ]);
    });
    expect(await screen.findByText("Sora の返信")).toBeInTheDocument();
    expect(screen.getByText("新しい質問")).toBeInTheDocument();
  });

  it("rolls back the optimistic message when sending fails", async () => {
    state.chatFailure = true;
    installFetch(state);
    render(<App />);

    const input = await screen.findByLabelText("メッセージ");
    fireEvent.change(input, { target: { value: "失敗する質問" } });
    fireEvent.click(screen.getByLabelText("送信"));

    expect(await screen.findByText(/送信に失敗しました/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("失敗する質問")).not.toBeInTheDocument();
    });
    expect(state.posts).toEqual([
      { conversation_id: "default", message: "失敗する質問" },
    ]);
  });

  it("switches conversations and syncs the ?c= query", async () => {
    installFetch(state);
    render(<App />);

    await screen.findByText("plan the week");
    fireEvent.click(screen.getByRole("button", { name: /work/ }));

    await waitFor(() => {
      expect(window.location.search).toBe("?c=work");
    });
    expect(await screen.findByText("work reply")).toBeInTheDocument();
  });

  it("opens the conversation named by ?c= on first render", async () => {
    window.history.replaceState({}, "", "/?c=work");
    installFetch(state);
    render(<App />);

    expect(await screen.findByText("work reply")).toBeInTheDocument();
    expect(screen.getAllByText("work").length).toBeGreaterThan(0);
  });

  it("falls back to tab notifications when the VAPID fetch fails", async () => {
    state.vapidFailure = true;
    installFetch(state);
    render(<App />);

    expect(await screen.findByText("通知オフ")).toBeInTheDocument();
    expect(screen.queryByText("通知を確認中…")).not.toBeInTheDocument();
  });

  it("keeps the sent message and reply when the timeline refetch fails", async () => {
    state.timelines.set("default", [turn("default", "reply", "既存の返信")]);
    installFetch(state);
    render(<App />);
    await screen.findByText("既存の返信");
    state.timelineFailure = true;

    const input = await screen.findByLabelText("メッセージ");
    fireEvent.change(input, { target: { value: "オフラインの質問" } });
    fireEvent.click(screen.getByLabelText("送信"));

    expect(await screen.findByText("Sora の返信")).toBeInTheDocument();
    expect(
      await screen.findByText("タイムラインを取得できませんでした。"),
    ).toBeInTheDocument();
    expect(screen.getByText("オフラインの質問")).toBeInTheDocument();
    expect(screen.getByText("既存の返信")).toBeInTheDocument();
  });

  it("does not push a history entry when the active conversation is selected again", async () => {
    window.history.replaceState({}, "", "/?c=work");
    installFetch(state);
    const pushState = vi.spyOn(window.history, "pushState");
    render(<App />);

    await screen.findByText("work reply");
    fireEvent.click(screen.getByRole("button", { name: /work/ }));

    expect(pushState).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?c=work");
  });

  it("re-registers a matching push subscription on load", async () => {
    const subscription = fakePushSubscription();
    stubBrowser(subscription);
    state.vapidPublicKey = "AQID";
    const fetchMock = installFetch(state);

    render(<App />);
    expect(await screen.findByText("通知オン")).toBeInTheDocument();

    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        (call) =>
          String(call[0]) === "/push/subscriptions" &&
          call[1]?.method === "POST",
      );
      expect(posts).toHaveLength(1);
    });
    const post = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]) === "/push/subscriptions" &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      endpoint: subscription.endpoint,
      keys: { p256dh: "AQID", auth: "BAUG" },
      conversation_id: null,
    });
  });

  it("drops a subscription created with a stale VAPID key on load", async () => {
    const subscription = fakePushSubscription(new Uint8Array([9, 9, 9]).buffer);
    stubBrowser(subscription);
    state.vapidPublicKey = "AQID";
    installFetch(state);

    render(<App />);

    await waitFor(() => {
      expect(subscription.unsubscribe).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText("通知オフ")).toBeInTheDocument();
    expect(screen.queryByText("通知オン")).not.toBeInTheDocument();
  });

  it("retries a failed disable with DELETE instead of POST", async () => {
    const subscription = fakePushSubscription();
    stubBrowser(subscription);
    state.vapidPublicKey = "AQID";
    state.pushDeleteFailure = true;
    const fetchMock = installFetch(state);

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: "通知をオフにする" }),
    );

    expect(await screen.findByText("通知の設定に失敗")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "再試行" }));

    await waitFor(() => {
      const deletes = fetchMock.mock.calls.filter(
        (call) => call[1]?.method === "DELETE",
      );
      expect(deletes).toHaveLength(2);
    });
    const posts = fetchMock.mock.calls.filter(
      (call) =>
        String(call[0]) === "/push/subscriptions" && call[1]?.method === "POST",
    );
    expect(posts).toHaveLength(1);
  });
});
