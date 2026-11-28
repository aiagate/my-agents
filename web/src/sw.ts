import { precacheAndRoute } from "workbox-precaching";
import type { PrecacheEntry } from "workbox-precaching";
declare let self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<PrecacheEntry | string>;
};
precacheAndRoute(self.__WB_MANIFEST, { ignoreURLParametersMatching: [/^c$/] });
const DEFAULT_TITLE = "Sora";
const ICON = "/icons/icon-192.png";
interface PushPayload {
  title: string;
  body: string;
  url: string;
  tag: string | undefined;
  conversationId: string;
}
interface NotificationData {
  url: string;
  conversationId: string;
}
const asString = (value: unknown): string | undefined =>
  typeof value === "string" ? value : undefined;
function parsePayload(data: PushMessageData | null): PushPayload {
  const text = data?.text() ?? "";
  let record: Record<string, unknown> = { body: text };
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === "object" && parsed !== null) {
      record = parsed as Record<string, unknown>;
    }
  } catch {
    // Non-JSON pushes still surface their raw text as the body.
  }
  const conversationId = asString(record["conversation_id"]) ?? "default";
  return {
    title: asString(record["title"]) ?? DEFAULT_TITLE,
    body: asString(record["body"]) ?? "",
    url: asString(record["url"]) ?? `/?c=${encodeURIComponent(conversationId)}`,
    tag: asString(record["tag"]),
    conversationId,
  };
}
async function openConversation(
  targetUrl: string,
  conversationId: string,
): Promise<void> {
  const windows = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  const client = windows.find((candidate) =>
    candidate.url.startsWith(self.registration.scope),
  );
  if (client === undefined) {
    await self.clients.openWindow(targetUrl);
    return;
  }
  await client.focus();
  client.postMessage({ type: "OPEN_CONVERSATION", conversationId });
}
self.addEventListener("install", () => {
  void self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener("push", (event) => {
  const payload = parsePayload(event.data);
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      if (
        windows.some(
          (client) => client.visibilityState === "visible" && client.focused,
        )
      ) {
        return;
      }
      await self.registration.showNotification(payload.title || DEFAULT_TITLE, {
        body: payload.body,
        tag: payload.tag,
        icon: ICON,
        badge: ICON,
        data: {
          url: payload.url,
          conversationId: payload.conversationId,
        } satisfies NotificationData,
      });
    })(),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = (event.notification.data ?? { url: "/", conversationId: "default" }) as NotificationData;
  event.waitUntil(openConversation(data.url, data.conversationId));
});
