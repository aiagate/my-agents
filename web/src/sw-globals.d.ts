// lib.dom and lib.webworker declare overlapping globals (`self`, `location`,
// `addEventListener`, ...), so a project cannot load both. Only the worker
// globals actually used by src/sw.ts are declared here instead.
interface ExtendableEvent extends Event {
  waitUntil(promise: Promise<unknown>): void;
}
interface PushMessageData {
  json(): unknown;
  text(): string;
}
interface PushEvent extends ExtendableEvent {
  readonly data: PushMessageData | null;
}
interface NotificationEvent extends ExtendableEvent {
  readonly notification: Notification;
  readonly action: string;
}
interface ServiceWorkerClient {
  readonly id: string;
  readonly url: string;
  readonly type: string;
  readonly visibilityState?: DocumentVisibilityState;
  readonly focused?: boolean;
  postMessage(message: unknown, transfer?: readonly Transferable[]): void;
  focus(): Promise<ServiceWorkerClient>;
  navigate(url: string): Promise<ServiceWorkerClient | null>;
}
interface ServiceWorkerClients {
  matchAll(options?: {
    type?: "window" | "all";
    includeUncontrolled?: boolean;
  }): Promise<readonly ServiceWorkerClient[]>;
  openWindow(url: string): Promise<ServiceWorkerClient | null>;
  claim(): Promise<void>;
}
interface ServiceWorkerGlobalScope {
  readonly registration: ServiceWorkerRegistration;
  readonly clients: ServiceWorkerClients;
  skipWaiting(): Promise<void>;
  addEventListener(type: "install", listener: () => void): void;
  addEventListener(
    type: "activate",
    listener: (event: ExtendableEvent) => void,
  ): void;
  addEventListener(type: "push", listener: (event: PushEvent) => void): void;
  addEventListener(
    type: "notificationclick",
    listener: (event: NotificationEvent) => void,
  ): void;
}
