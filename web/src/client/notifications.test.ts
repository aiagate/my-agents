import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ForegroundNotificationManager,
  detectPushCapabilities,
  disablePush,
  enablePush,
  getCurrentSubscription,
  isIosBrowserNeedingInstall,
  isProactiveTurn,
  subscriptionMatchesVapidKey,
  syncPushSubscription,
  urlBase64ToUint8Array,
  waitForServiceWorkerReady,
} from "./notifications";
import type { TimelineItem } from "./types";

class FakeNotification {
  static permission: NotificationPermission = "granted";
  static requestPermission = vi.fn<() => Promise<NotificationPermission>>(
    async () => FakeNotification.permission,
  );
}

const P256DH = new Uint8Array([1, 2, 3]).buffer;
const AUTH = new Uint8Array([4, 5, 6]).buffer;

function fakeSubscription(
  applicationServerKey?: ArrayBuffer,
): PushSubscription {
  return {
    endpoint: "https://push.example.test/subscription",
    options: {
      userVisibleOnly: true,
      applicationServerKey: applicationServerKey ?? null,
    },
    getKey: (name: string) => (name === "p256dh" ? P256DH : AUTH),
    unsubscribe: vi.fn(async () => true),
  } as unknown as PushSubscription;
}

function fakeRegistration(options: {
  existing?: PushSubscription | null;
  created?: PushSubscription;
  showNotification?: ReturnType<typeof vi.fn>;
} = {}): ServiceWorkerRegistration {
  const subscription = options.created ?? fakeSubscription();
  return {
    showNotification: options.showNotification ?? vi.fn(async () => undefined),
    pushManager: {
      getSubscription: vi.fn(async () => options.existing ?? null),
      subscribe: vi.fn(async () => subscription),
    },
  } as unknown as ServiceWorkerRegistration;
}

function stubServiceWorker(
  registration: ServiceWorkerRegistration | undefined,
  ready = Promise.resolve(registration),
): { getRegistration: ReturnType<typeof vi.fn>; ready: Promise<unknown> } {
  const serviceWorker = {
    getRegistration: vi.fn(async () => registration),
    ready,
  };
  vi.stubGlobal("navigator", {
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    maxTouchPoints: 0,
    serviceWorker,
  });
  return serviceWorker;
}

function setVisibility(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

function proactiveItem(overrides: Partial<TimelineItem> = {}): TimelineItem {
  return {
    item_id: "item-1",
    kind: "turn",
    conversation_id: "default",
    content: "Sora からのお知らせ",
    created_at: "2025-01-01T00:00:00+00:00",
    event_id: "evt-1",
    origin: "proactive",
    ...overrides,
  };
}

beforeEach(() => {
  FakeNotification.permission = "granted";
  FakeNotification.requestPermission.mockClear();
  FakeNotification.requestPermission.mockImplementation(
    async () => FakeNotification.permission,
  );
  vi.stubGlobal("Notification", FakeNotification);
  setVisibility(false);
  document.title = "Sora";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setVisibility(false);
  document.title = "";
});

describe("isProactiveTurn", () => {
  it("matches only proactive turn messages", () => {
    expect(isProactiveTurn(proactiveItem())).toBe(true);
    expect(isProactiveTurn(proactiveItem({ origin: "reply" }))).toBe(false);
    expect(isProactiveTurn(proactiveItem({ kind: "event" }))).toBe(false);
  });
});

describe("ForegroundNotificationManager", () => {
  it("suppresses notifications while the tab is visible", async () => {
    const showNotification = vi.fn(async () => undefined);
    stubServiceWorker(fakeRegistration({ showNotification }));
    const manager = new ForegroundNotificationManager();
    manager.handleItem(proactiveItem());

    expect(showNotification).not.toHaveBeenCalled();
    expect(manager.unreadCount).toBe(0);
    expect(document.title).toBe("Sora");
    manager.dispose();
  });

  it("badges the title and uses registration.showNotification while hidden", async () => {
    setVisibility(true);
    const showNotification = vi.fn(
      async (_title: string, _options?: NotificationOptions) => undefined,
    );
    stubServiceWorker(fakeRegistration({ showNotification }));
    const manager = new ForegroundNotificationManager();
    manager.handleItem(proactiveItem());

    expect(document.title).toBe("(1) Sora");
    expect(manager.unreadCount).toBe(1);
    await vi.waitFor(() => {
      expect(showNotification).toHaveBeenCalledTimes(1);
    });
    const call = showNotification.mock.calls[0];
    expect(call?.[0]).toBe("Sora");
    const options = call?.[1];
    expect(options?.body).toBe("Sora からのお知らせ");
    expect(options?.icon).toBe("/icons/icon-192.png");
    expect(options?.badge).toBe("/icons/icon-192.png");
    expect(options?.tag).toBe("proactive:evt-1");
    expect(options?.data).toEqual({
      url: "/?c=default",
      conversationId: "default",
    });

    manager.handleItem(proactiveItem({ event_id: "evt-2", item_id: "item-2" }));
    expect(document.title).toBe("(2) Sora");
    manager.handleItem(proactiveItem({ event_id: null, item_id: "item-3" }));
    await vi.waitFor(() => {
      expect(showNotification).toHaveBeenCalledTimes(3);
    });
    expect(showNotification.mock.calls[2]?.[1]).toMatchObject({
      tag: "proactive:item-3",
    });

    setVisibility(false);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(document.title).toBe("Sora");
    expect(manager.unreadCount).toBe(0);
    manager.dispose();
  });

  it("resets the unread badge on window focus", () => {
    setVisibility(true);
    stubServiceWorker(fakeRegistration());
    const manager = new ForegroundNotificationManager();
    manager.handleItem(proactiveItem());
    expect(document.title).toBe("(1) Sora");

    window.dispatchEvent(new Event("focus"));
    expect(document.title).toBe("Sora");
    expect(manager.unreadCount).toBe(0);
    manager.dispose();
  });

  it("still badges the title when permission is denied", async () => {
    setVisibility(true);
    FakeNotification.permission = "denied";
    const showNotification = vi.fn(async () => undefined);
    stubServiceWorker(fakeRegistration({ showNotification }));
    const manager = new ForegroundNotificationManager();
    manager.handleItem(proactiveItem({ origin: "reply" }));
    expect(document.title).toBe("Sora");

    manager.handleItem(proactiveItem());
    expect(document.title).toBe("(1) Sora");
    await Promise.resolve();
    expect(showNotification).not.toHaveBeenCalled();
    manager.dispose();
  });
});

describe("urlBase64ToUint8Array", () => {
  it("decodes base64url without padding", () => {
    expect(Array.from(urlBase64ToUint8Array("AQID"))).toEqual([1, 2, 3]);
    expect(Array.from(urlBase64ToUint8Array("-_8"))).toEqual([251, 255]);
  });
});

describe("subscriptionMatchesVapidKey", () => {
  it("compares the decoded application key byte for byte", () => {
    const subscription = fakeSubscription(new Uint8Array([1, 2, 3]).buffer);

    expect(subscriptionMatchesVapidKey(subscription, "AQID")).toBe(true);
    expect(subscriptionMatchesVapidKey(subscription, "AQIE")).toBe(false);
    expect(subscriptionMatchesVapidKey(subscription, "AQIDBA")).toBe(false);
  });

  it("treats a subscription without options as matching", () => {
    const legacy = {
      endpoint: "https://push.example.test/legacy",
    } as unknown as PushSubscription;

    expect(subscriptionMatchesVapidKey(legacy, "AQID")).toBe(true);
  });
});

describe("syncPushSubscription", () => {
  it("posts the endpoint and keys without a conversation", async () => {
    const subscription = fakeSubscription();
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ status: "subscribed" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await syncPushSubscription(subscription);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/push/subscriptions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      endpoint: subscription.endpoint,
      keys: { p256dh: "AQID", auth: "BAUG" },
      conversation_id: null,
    });
  });
});

describe("waitForServiceWorkerReady", () => {
  it("resolves with the ready registration", async () => {
    const registration = fakeRegistration();
    stubServiceWorker(registration);

    await expect(waitForServiceWorkerReady(1_000)).resolves.toBe(registration);
  });

  it("rejects when the registration never becomes ready", async () => {
    stubServiceWorker(
      undefined,
      new Promise<ServiceWorkerRegistration>(() => undefined),
    );
    vi.useFakeTimers();
    try {
      const ready = waitForServiceWorkerReady(1_000);
      const rejection = expect(ready).rejects.toThrow(
        "Service worker registration timed out.",
      );
      await vi.advanceTimersByTimeAsync(1_000);
      await rejection;
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("isIosBrowserNeedingInstall", () => {
  function stubNavigator(userAgent: string, maxTouchPoints: number, standalone = false): void {
    vi.stubGlobal("navigator", { userAgent, maxTouchPoints, standalone });
  }

  it("requires installation for an iOS Safari tab", () => {
    stubNavigator(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
      5,
    );
    expect(isIosBrowserNeedingInstall()).toBe(true);
  });

  it("detects iPadOS desktop-mode touch devices", () => {
    stubNavigator("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 5);
    expect(isIosBrowserNeedingInstall()).toBe(true);
  });

  it("accepts standalone iOS and non-iOS browsers", () => {
    stubNavigator(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
      5,
      true,
    );
    expect(isIosBrowserNeedingInstall()).toBe(false);

    stubNavigator("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 0);
    expect(isIosBrowserNeedingInstall()).toBe(false);
  });

  it("accepts the standalone display mode", () => {
    stubNavigator("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", 5);
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    expect(isIosBrowserNeedingInstall()).toBe(false);
  });
});

describe("push capabilities", () => {
  it("detects service worker, push manager, and permission state", () => {
    stubServiceWorker(fakeRegistration());
    vi.stubGlobal("PushManager", class {});
    expect(detectPushCapabilities()).toMatchObject({
      serviceWorker: true,
      pushManager: true,
      notification: true,
      permission: "granted",
      iosNeedsInstall: false,
    });
  });
});

describe("enablePush", () => {
  it("requests permission before touching the network and subscribes after", async () => {
    const subscription = fakeSubscription();
    const registration = fakeRegistration({ existing: null, created: subscription });
    stubServiceWorker(registration);
    const order: string[] = [];
    FakeNotification.requestPermission.mockImplementation(async () => {
      order.push("permission");
      return "granted";
    });
    registration.pushManager.getSubscription = vi.fn(async () => {
      order.push("getSubscription");
      return null;
    });
    registration.pushManager.subscribe = vi.fn(async () => {
      order.push("subscribe");
      return subscription;
    });
    const fetchMock = vi.fn(async () => {
      order.push("fetch");
      return new Response(
        JSON.stringify({ status: "subscribed", endpoint: subscription.endpoint }),
        { status: 201 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const created = await enablePush("AQID");

    expect(created).toBe(subscription);
    expect(order).toEqual(["permission", "getSubscription", "subscribe", "fetch"]);
    expect(registration.pushManager.subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: expect.any(Uint8Array),
    });
    const subscribeArgument = vi.mocked(registration.pushManager.subscribe).mock
      .calls[0]?.[0] as PushSubscriptionOptionsInit;
    expect(Array.from(subscribeArgument.applicationServerKey as Uint8Array)).toEqual([
      1, 2, 3,
    ]);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/push/subscriptions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      endpoint: subscription.endpoint,
      keys: { p256dh: "AQID", auth: "BAUG" },
      conversation_id: null,
    });
  });

  it("reuses a subscription created with the current VAPID key", async () => {
    const existing = fakeSubscription(new Uint8Array([1, 2, 3]).buffer);
    const registration = fakeRegistration({ existing });
    stubServiceWorker(registration);
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ status: "subscribed" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(enablePush("AQID")).resolves.toBe(existing);
    expect(existing.unsubscribe).not.toHaveBeenCalled();
    expect(registration.pushManager.subscribe).not.toHaveBeenCalled();
  });

  it("re-subscribes when the stored subscription used a different VAPID key", async () => {
    const stale = fakeSubscription(new Uint8Array([9, 9, 9]).buffer);
    const fresh = fakeSubscription(new Uint8Array([1, 2, 3]).buffer);
    const registration = fakeRegistration({ existing: stale, created: fresh });
    stubServiceWorker(registration);
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ status: "subscribed" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(enablePush("AQID")).resolves.toBe(fresh);
    expect(stale.unsubscribe).toHaveBeenCalledTimes(1);
    expect(registration.pushManager.subscribe).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/push/subscriptions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({
      endpoint: fresh.endpoint,
    });
  });

  it("returns null without subscribing when no VAPID key is configured", async () => {    const subscription = fakeSubscription();
    const registration = fakeRegistration({ existing: null, created: subscription });
    stubServiceWorker(registration);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(enablePush(null)).resolves.toBeNull();
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    expect(registration.pushManager.subscribe).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects when the notification permission is denied", async () => {
    const registration = fakeRegistration();
    stubServiceWorker(registration);
    FakeNotification.requestPermission.mockResolvedValue("denied");

    await expect(enablePush("AQID")).rejects.toThrow(
      "Notification permission was denied.",
    );
    expect(registration.pushManager.subscribe).not.toHaveBeenCalled();
  });
});

describe("disablePush", () => {
  it("unsubscribes and deletes the server subscription", async () => {
    const subscription = fakeSubscription();
    const registration = fakeRegistration({ existing: subscription });
    stubServiceWorker(registration);
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ deleted: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(disablePush()).resolves.toBe(true);
    expect(subscription.unsubscribe).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/push/subscriptions");
    expect(init.method).toBe("DELETE");
    expect(JSON.parse(String(init.body))).toEqual({
      endpoint: subscription.endpoint,
    });
  });
});

describe("getCurrentSubscription", () => {
  it("returns null immediately when no service worker is registered", async () => {
    stubServiceWorker(undefined, new Promise<ServiceWorkerRegistration>(() => undefined));
    await expect(getCurrentSubscription()).resolves.toBeNull();
  });

  it("returns the existing subscription from the registration", async () => {
    const subscription = fakeSubscription();
    stubServiceWorker(fakeRegistration({ existing: subscription }));
    await expect(getCurrentSubscription()).resolves.toBe(subscription);
  });
});
