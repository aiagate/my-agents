import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationStream } from "./stream";
import type { ConnectionStatus, TimelineItem } from "./types";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readyState = FakeEventSource.CONNECTING;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }

  fail(readyState = FakeEventSource.CONNECTING): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }

  message(item: TimelineItem, lastEventId = ""): void {
    this.onmessage?.({
      data: JSON.stringify(item),
      lastEventId,
    } as MessageEvent<string>);
  }
}

function item(overrides: Partial<TimelineItem> = {}): TimelineItem {
  return {
    item_id: "item-1",
    kind: "turn",
    conversation_id: "default",
    content: "hello",
    created_at: "2025-01-01T00:00:00+00:00",
    event_id: null,
    origin: "reply",
    ...overrides,
  };
}

function createStream(
  overrides: {
    onItem?: (value: TimelineItem) => void;
    onStatusChange?: (status: ConnectionStatus) => void;
    onReconnect?: () => void;
  } = {},
): ConversationStream {
  return new ConversationStream({
    conversationId: "default",
    onItem: overrides.onItem ?? (() => undefined),
    onStatusChange: overrides.onStatusChange,
    onReconnect: overrides.onReconnect,
  });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConversationStream", () => {
  it("opens the native stream with the conversation id and reports live", () => {
    const statuses: ConnectionStatus[] = [];
    createStream({ onStatusChange: (value) => statuses.push(value) });

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0]?.url).toBe(
      "/stream?conversation_id=default",
    );
    expect(statuses).toEqual(["connecting"]);

    FakeEventSource.instances[0]?.open();
    expect(statuses).toEqual(["connecting", "live"]);
  });

  it("parses SSE items and ignores malformed data", () => {
    const received: TimelineItem[] = [];
    createStream({ onItem: (value) => received.push(value) });
    const source = FakeEventSource.instances[0];
    source?.open();

    source?.message(item(), "2025-01-01T00:00:00+00:00|item-1");
    expect(received).toHaveLength(1);
    expect(received[0]?.content).toBe("hello");

    source?.onmessage?.({
      data: "not-json",
      lastEventId: "",
    } as MessageEvent<string>);
    expect(received).toHaveLength(1);
  });

  it("relies on the native reconnect and fires onReconnect on the next open", () => {
    const statuses: ConnectionStatus[] = [];
    const onReconnect = vi.fn();
    createStream({
      onStatusChange: (value) => statuses.push(value),
      onReconnect,
    });
    const source = FakeEventSource.instances[0];
    source?.open();
    source?.fail();

    expect(statuses).toEqual(["connecting", "live", "reconnecting"]);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source?.closed).toBe(false);

    source?.open();
    expect(statuses).toEqual(["connecting", "live", "reconnecting", "live"]);
    expect(onReconnect).toHaveBeenCalledTimes(1);

    source?.open();
    expect(onReconnect).toHaveBeenCalledTimes(2);
  });

  it("reports offline when the browser is offline", () => {
    const statuses: ConnectionStatus[] = [];
    createStream({ onStatusChange: (value) => statuses.push(value) });
    const source = FakeEventSource.instances[0];
    source?.open();
    vi.stubGlobal("navigator", { onLine: false });

    source?.fail();
    expect(statuses.at(-1)).toBe("offline");
  });

  it("reports offline when the native stream is permanently closed", () => {
    const statuses: ConnectionStatus[] = [];
    createStream({ onStatusChange: (value) => statuses.push(value) });
    const source = FakeEventSource.instances[0];
    source?.open();

    source?.fail(FakeEventSource.CLOSED);
    expect(statuses).toEqual(["connecting", "live", "offline"]);
  });

  it("does not refetch when a retry before the first open finally opens", () => {
    const onReconnect = vi.fn();
    createStream({ onReconnect });
    const source = FakeEventSource.instances[0];

    source?.fail();
    source?.open();
    expect(onReconnect).not.toHaveBeenCalled();

    source?.fail();
    source?.open();
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  it("stops handling events after close", () => {
    const received: TimelineItem[] = [];
    const statuses: ConnectionStatus[] = [];
    const stream = createStream({
      onItem: (value) => received.push(value),
      onStatusChange: (value) => statuses.push(value),
    });
    const source = FakeEventSource.instances[0];
    stream.close();

    expect(source?.closed).toBe(true);
    source?.open();
    source?.message(item());
    source?.fail();
    expect(received).toHaveLength(0);
    expect(statuses).toEqual(["connecting"]);
  });
});
