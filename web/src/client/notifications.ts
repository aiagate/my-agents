import { registerPushSubscription, unregisterPushSubscription } from "./api";
import type { TimelineItem } from "./types";
export const APP_TITLE = "Sora";
export const PERSONA_TITLE = "Sora";
const ICON = "/icons/icon-192.png";
export function isProactiveTurn(item: TimelineItem): boolean {
  return item.kind === "turn" && item.origin === "proactive";
}
export function urlBase64ToUint8Array(
  base64Url: string,
): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}
function toBase64Url(buffer: ArrayBuffer): string {
  const binary = String.fromCharCode(...new Uint8Array(buffer));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export class ForegroundNotificationManager {
  private unread = 0;
  private disposed = false;
  private readonly onVisibilityChange = (): void => {
    if (document.visibilityState !== "hidden") this.resetUnread();
  };
  private readonly onFocus = (): void => this.resetUnread();
  constructor() {
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    window.addEventListener("focus", this.onFocus);
    this.updateTitle();
  }
  get unreadCount(): number {
    return this.unread;
  }
  isHidden(): boolean {
    return document.visibilityState === "hidden";
  }
  handleItem(item: TimelineItem): void {
    if (this.disposed || !this.isHidden() || !isProactiveTurn(item)) return;
    this.unread += 1;
    this.updateTitle();
    void this.showNotification(item).catch(() => undefined);
  }
  resetUnread(): void {
    this.unread = 0;
    this.updateTitle();
  }
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
    window.removeEventListener("focus", this.onFocus);
  }
  private updateTitle(): void {
    document.title = this.unread > 0 ? `(${this.unread}) ${APP_TITLE}` : APP_TITLE;
  }
  private async showNotification(item: TimelineItem): Promise<void> {
    if (
      typeof Notification === "undefined" ||
      Notification.permission !== "granted" ||
      !("serviceWorker" in navigator)
    ) {
      return;
    }
    const registration = await navigator.serviceWorker.getRegistration();
    await registration?.showNotification(PERSONA_TITLE, {
      body: item.content,
      icon: ICON,
      badge: ICON,
      tag: `proactive:${item.event_id ?? item.item_id}`,
      data: {
        url: `/?c=${encodeURIComponent(item.conversation_id)}`,
        conversationId: item.conversation_id,
      },
    });
  }
}
export function detectPushCapabilities() {
  const nav = typeof navigator === "undefined" ? undefined : navigator;
  const notification = typeof Notification === "undefined" ? null : Notification;
  return {
    serviceWorker: nav !== undefined && "serviceWorker" in nav,
    pushManager: typeof window !== "undefined" && "PushManager" in window,
    notification: notification !== null,
    permission: notification?.permission ?? "unsupported",
    iosNeedsInstall: isIosBrowserNeedingInstall(),
  };
}
export function isIosBrowserNeedingInstall(): boolean {
  if (typeof navigator === "undefined") return false;
  const userAgent = navigator.userAgent;
  const isIos =
    /iPad|iPhone|iPod/.test(userAgent) ||
    (userAgent.includes("Macintosh") && navigator.maxTouchPoints > 1);
  if (!isIos || (navigator as Navigator & { standalone?: boolean }).standalone === true) {
    return false;
  }
  return !(
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(display-mode: standalone)").matches
  );
}
export async function getCurrentSubscription(): Promise<PushSubscription | null> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return null;
  }
  const registration = await navigator.serviceWorker.getRegistration();
  return (await registration?.pushManager.getSubscription()) ?? null;
}
function subscriptionKeys(subscription: PushSubscription): {
  p256dh: string;
  auth: string;
} {
  const p256dh = subscription.getKey("p256dh");
  const auth = subscription.getKey("auth");
  if (p256dh === null || auth === null) {
    throw new Error("Push subscription is missing its encryption keys.");
  }
  return { p256dh: toBase64Url(p256dh), auth: toBase64Url(auth) };
}
export async function syncPushSubscription(
  subscription: PushSubscription,
): Promise<void> {
  await registerPushSubscription({
    endpoint: subscription.endpoint,
    keys: subscriptionKeys(subscription),
    conversation_id: null,
  });
}
export function subscriptionMatchesVapidKey(
  subscription: PushSubscription,
  vapidPublicKey: string,
): boolean {
  const existingKey = subscription.options?.applicationServerKey;
  if (existingKey == null) return true;
  const expected = urlBase64ToUint8Array(vapidPublicKey);
  const actual = new Uint8Array(existingKey);
  if (actual.byteLength !== expected.byteLength) return false;
  for (let index = 0; index < expected.byteLength; index += 1) {
    if (actual[index] !== expected[index]) return false;
  }
  return true;
}
export async function waitForServiceWorkerReady(
  timeoutMs = 10_000,
): Promise<ServiceWorkerRegistration> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error("Service worker registration timed out.")),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
export async function enablePush(
  vapidPublicKey: string | null,
): Promise<PushSubscription | null> {
  if (typeof Notification === "undefined") {
    throw new Error("Notifications are not available in this browser.");
  }
  // The permission prompt must run in the button click task, before any
  // network call, or iOS Safari rejects the later subscribe() call.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was denied.");
  }
  if (vapidPublicKey === null || vapidPublicKey === "") return null;
  if (!("serviceWorker" in navigator)) {
    throw new Error("Service workers are not available in this browser.");
  }
  const registration = await waitForServiceWorkerReady();
  let existing = await registration.pushManager.getSubscription();
  if (
    existing !== null &&
    !subscriptionMatchesVapidKey(existing, vapidPublicKey)
  ) {
    await existing.unsubscribe();
    existing = null;
  }
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    }));
  await syncPushSubscription(subscription);
  return subscription;
}
export async function disablePush(): Promise<boolean> {
  if (!("serviceWorker" in navigator)) return false;
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription === null || subscription === undefined) return false;
  await subscription.unsubscribe();
  await unregisterPushSubscription(subscription.endpoint);
  return true;
}
