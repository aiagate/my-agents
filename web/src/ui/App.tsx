import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { getVapidPublicKey, listConversations } from "../client/api";
import { ForegroundNotificationManager } from "../client/notifications";
import { ConversationStream } from "../client/stream";
import type { ConnectionStatus, TimelineResponse } from "../client/types";
import { ChatShell } from "./ChatShell";
import { ConversationSidebar } from "./ConversationSidebar";
import { insertTimelineItem } from "./timeline";
import { useProactivePush } from "./usePush";
export { insertTimelineItem } from "./timeline";
const DEFAULT_CONVERSATION = "default";
function readConversationId(): string {
  if (typeof window === "undefined") return DEFAULT_CONVERSATION;
  const value = new URLSearchParams(window.location.search).get("c");
  return value !== null && value.trim() !== "" ? value.trim() : DEFAULT_CONVERSATION;
}
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
  });
}
function AppContent() {
  const queryClient = useQueryClient();
  const [conversationId, setConversationId] = useState(readConversationId);
  const conversationRef = useRef(conversationId);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const foregroundRef = useRef<ForegroundNotificationManager | null>(null);
  const selectConversation = useCallback((rawId: string) => {
    const id = rawId.trim() === "" ? DEFAULT_CONVERSATION : rawId.trim();
    setConversationId(id);
    setDrawerOpen(false);
    if (typeof window === "undefined") return;
    if (conversationRef.current === id) return;
    conversationRef.current = id;
    const url = new URL(window.location.href);
    url.searchParams.set("c", id);
    window.history.pushState({ conversationId: id }, "", url);
  }, []);
  useEffect(() => {
    conversationRef.current = conversationId;
  }, [conversationId]);
  useEffect(() => {
    const onPopState = (): void => {
      const next = readConversationId();
      conversationRef.current = next;
      setConversationId(next);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    const manager = new ForegroundNotificationManager();
    foregroundRef.current = manager;
    return () => {
      manager.dispose();
      foregroundRef.current = null;
    };
  }, []);
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
      return undefined;
    }
    const serviceWorker = navigator.serviceWorker;
    const onMessage = (event: MessageEvent): void => {
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null) return;
      const record = data as Record<string, unknown>;
      if (record["type"] !== "OPEN_CONVERSATION") return;
      const id = record["conversationId"];
      if (typeof id === "string" && id !== "") selectConversation(id);
    };
    serviceWorker.addEventListener("message", onMessage);
    return () => serviceWorker.removeEventListener("message", onMessage);
  }, [selectConversation]);
  const conversationsQuery = useQuery({
    queryKey: ["conversations"],
    queryFn: () => listConversations(100),
  });
  // Fetched ahead of the click so Web Push can subscribe inside the tap task.
  const vapidQuery = useQuery({
    queryKey: ["push", "vapid-public-key"],
    queryFn: getVapidPublicKey,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  useEffect(() => {
    let stream: ConversationStream;
    try {
      stream = new ConversationStream({
        conversationId,
        onItem: (item) => {
          queryClient.setQueryData<TimelineResponse>(
            ["timeline", conversationId],
            (previous) => insertTimelineItem(previous, item),
          );
          foregroundRef.current?.handleItem(item);
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        },
        onStatusChange: setStatus,
        onReconnect: () => {
          void queryClient.invalidateQueries({
            queryKey: ["timeline", conversationId],
          });
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        },
      });
    } catch {
      setStatus("offline");
      return undefined;
    }
    return () => stream.close();
  }, [conversationId, queryClient]);
  const push = useProactivePush(
    vapidQuery.isLoading ? undefined : (vapidQuery.data ?? { public_key: null }),
  );
  return (
    <div className="flex h-full min-h-0 bg-void text-ink">
      <ConversationSidebar
        conversations={conversationsQuery.data?.conversations}
        conversationsLoading={conversationsQuery.isLoading}
        activeId={conversationId}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onSelect={selectConversation}
        pushState={push.state}
        pushBusy={push.busy}
        onEnablePush={push.enable}
        onDisablePush={push.disable}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        <ChatShell
          conversationId={conversationId}
          connectionStatus={status}
          onOpenSidebar={() => setDrawerOpen(true)}
        />
      </main>
    </div>
  );
}
export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <AppContent />
    </QueryClientProvider>
  );
}
