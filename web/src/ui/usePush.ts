import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  detectPushCapabilities,
  disablePush,
  enablePush,
  getCurrentSubscription,
  subscriptionMatchesVapidKey,
  syncPushSubscription,
} from "../client/notifications";
interface PushError {
  detail: string;
  failed: "enable" | "disable";
}
export type PushUiState =
  | { kind: "loading" }
  | { kind: "blocked"; reason: "unsupported" | "ios-install" | "denied" }
  | { kind: "off"; canWebPush: boolean }
  | { kind: "on"; webPush: boolean }
  | { kind: "error"; detail: string; failed: "enable" | "disable" };
export interface ProactivePush {
  state: PushUiState;
  busy: boolean;
  enable: () => void;
  disable: () => void;
}
const messageOf = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);
export function useProactivePush(
  vapid: { public_key: string | null } | undefined,
): ProactivePush {
  const [capabilities, setCapabilities] = useState(detectPushCapabilities);
  const [subscription, setSubscription] = useState<PushSubscription | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PushError | null>(null);
  const syncedRef = useRef<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void getCurrentSubscription()
      .then((current) => {
        if (!cancelled) setSubscription(current);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) {
          setCapabilities(detectPushCapabilities());
          setReady(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    if (!ready || subscription === null) return;
    const publicKey = vapid?.public_key ?? null;
    if (publicKey === null || publicKey === "") return;
    const identity = `${subscription.endpoint}\n${publicKey}`;
    if (syncedRef.current === identity) return;
    syncedRef.current = identity;
    if (!subscriptionMatchesVapidKey(subscription, publicKey)) {
      void subscription.unsubscribe().catch(() => undefined);
      setSubscription(null);
      return;
    }
    // A wiped backend database loses subscriptions the browser still holds;
    // re-registering on load restores push delivery without user action.
    void syncPushSubscription(subscription).catch(() => undefined);
  }, [ready, subscription, vapid?.public_key]);
  const state = useMemo<PushUiState>(() => {
    if (error !== null) {
      return { kind: "error", detail: error.detail, failed: error.failed };
    }
    if (capabilities.iosNeedsInstall) {
      return { kind: "blocked", reason: "ios-install" };
    }
    if (!capabilities.serviceWorker || !capabilities.notification) {
      return { kind: "blocked", reason: "unsupported" };
    }
    if (capabilities.permission === "denied") {
      return { kind: "blocked", reason: "denied" };
    }
    if (!ready || vapid === undefined) return { kind: "loading" };
    if (subscription !== null) return { kind: "on", webPush: true };
    const canWebPush =
      capabilities.pushManager &&
      vapid.public_key !== null &&
      vapid.public_key !== "";
    if (canWebPush) return { kind: "off", canWebPush: true };
    if (capabilities.permission === "granted") {
      return { kind: "on", webPush: false };
    }
    return { kind: "off", canWebPush: false };
  }, [capabilities, error, ready, subscription, vapid]);
  const run = useCallback((op: "enable" | "disable", task: () => Promise<void>) => {
    setError(null);
    setBusy(true);
    void task()
      .catch((cause: unknown) => setError({ detail: messageOf(cause), failed: op }))
      .finally(() => setBusy(false));
  }, []);
  const enable = useCallback(() => {
    run("enable", async () => {
      const publicKey = vapid?.public_key ?? null;
      const next = await enablePush(publicKey);
      setSubscription(next);
      if (next !== null && publicKey !== null) {
        syncedRef.current = `${next.endpoint}\n${publicKey}`;
      }
      setCapabilities(detectPushCapabilities());
    });
  }, [run, vapid?.public_key]);
  const disable = useCallback(() => {
    run("disable", async () => {
      await disablePush();
      setSubscription(null);
      syncedRef.current = null;
      setCapabilities(detectPushCapabilities());
    });
  }, [run]);
  return { state, busy, enable, disable };
}
