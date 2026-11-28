import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import type { MessageState, ThreadMessageLike } from "@assistant-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Menu, TriangleAlert } from "lucide-react";
import { useMemo } from "react";
import { ApiError, getTimeline, sendChat } from "../client/api";
import type { ConnectionStatus, TimelineItem, TimelineResponse } from "../client/types";
import { MarkdownMessage } from "./Markdown";
import { insertTimelineItem } from "./timeline";

interface ChatShellProps {
  conversationId: string; connectionStatus: ConnectionStatus; onOpenSidebar: () => void;
}

let optimisticSequence = 0;
function nextOptimisticId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return `optimistic:${crypto.randomUUID()}`;
  }
  optimisticSequence += 1;
  return `optimistic:${Date.now()}-${optimisticSequence}`;
}
function describeSendError(cause: unknown): string {
  if (cause instanceof ApiError) {
    return `送信に失敗しました。${cause.message}（${cause.kind} / ${cause.status}）`;
  }
  return "送信に失敗しました。もう一度お試しください。";
}

const STATUS: Record<ConnectionStatus, { label: string; dot: string }> = {
  connecting: { label: "接続中", dot: "bg-amber-400" },
  live: { label: "ライブ", dot: "bg-teal-400" },
  reconnecting: { label: "再接続中", dot: "bg-amber-400 status-pulse" },
  offline: { label: "オフライン", dot: "bg-rose-400" },
};
const roleFor = (item: TimelineItem): ThreadMessageLike["role"] =>
  item.kind === "event_failed" ? "system" : item.origin === "user" ? "user" : "assistant";
const UserText = ({ text }: { text: string }) => (
  <p className="whitespace-pre-wrap break-words">{text}</p>
);
const UserMessage = () => (
  <MessagePrimitive.Root className="flex justify-end">
    <div className="max-w-[85%] rounded-2xl rounded-br-md border border-violet-glow/25 bg-violet-glow/10 px-4 py-2.5 text-[0.9375rem] leading-relaxed text-ink shadow-[0_1px_0_rgba(255,255,255,0.04)_inset]">
      <MessagePrimitive.Parts components={{ Text: UserText }} />
    </div>
  </MessagePrimitive.Root>
);
const AssistantMessage = () => (
  <MessagePrimitive.Root className="flex justify-start">
    <div className="max-w-[92%] rounded-2xl rounded-bl-md border border-line bg-panel-raised px-4 py-3">
      <MessagePrimitive.Parts components={{ Text: ({ text }) => <MarkdownMessage text={text} /> }} />
    </div>
  </MessagePrimitive.Root>
);
const SystemNotice = ({ content }: { content: string }) => (
  <div className="mx-auto flex max-w-[92%] items-start gap-2 rounded-xl border border-amber-400/25 bg-amber-400/5 px-3 py-2 text-xs text-amber-200/90">
    <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
    <span className="whitespace-pre-wrap break-words">{content}</span>
  </div>
);
const textOf = (message: MessageState): string =>
  message.content.map((part) => (part.type === "text" ? part.text : "")).join("");
const MessageRow = ({ message }: { message: MessageState }) => {
  if (message.role === "user") return <UserMessage />;
  if (message.role === "system") return <SystemNotice content={textOf(message)} />;
  return <AssistantMessage />;
};
const EmptyState = ({ conversationId }: { conversationId: string }) => (
  <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
    <div
      aria-hidden="true"
      className="h-16 w-16 rounded-full bg-[radial-gradient(circle_at_35%_30%,#cfc4ff_0%,#8f7bff_38%,#3b2a86_72%,#0d0f16_100%)] shadow-[0_0_50px_-8px_rgba(143,123,255,0.55)]"
    />
    <div className="space-y-1">
      <p className="text-sm font-medium text-ink">
        {conversationId === "default" ? "Sora との会話を始めましょう" : `「${conversationId}」の会話を始めましょう`}
      </p>
      <p className="text-xs text-ink-muted">
        メッセージを送るか、Sora からのプロアクティブな声を待ってください。
      </p>
    </div>
  </div>
);

export function ChatShell({ conversationId, connectionStatus, onOpenSidebar }: ChatShellProps) {
  const queryClient = useQueryClient();
  const timelineQuery = useQuery({
    queryKey: ["timeline", conversationId], queryFn: () => getTimeline(conversationId), retry: false,
  });
  const mutation = useMutation({
    mutationFn: (text: string) => sendChat(conversationId, text),
  });
  const entries = useMemo(
    () => (timelineQuery.data?.items ?? []).filter((item) => item.kind === "turn" || item.kind === "event_failed"),
    [timelineQuery.data],
  );
  const runtime = useExternalStoreRuntime<TimelineItem>({
    messages: entries,
    isRunning: mutation.isPending,
    onNew: async (message) => {
      const text = message.content.map((part) => (part.type === "text" ? part.text : "")).join("").trim();
      if (text === "") return;
      const targetConversation = conversationId;
      const optimistic: TimelineItem = {
        item_id: nextOptimisticId(), kind: "turn", conversation_id: targetConversation,
        content: text, created_at: new Date().toISOString(), event_id: null, origin: "user",
      };
      const setTimeline = (
        update: (previous: TimelineResponse | undefined) => TimelineResponse,
      ): void => {
        queryClient.setQueryData<TimelineResponse>(
          ["timeline", targetConversation],
          update,
        );
      };
      setTimeline((previous) => insertTimelineItem(previous, optimistic));
      try {
        const outcome = await mutation.mutateAsync(text);
        const reply: TimelineItem = {
          item_id: outcome.message_id,
          kind: "turn",
          conversation_id: targetConversation,
          content: outcome.reply,
          created_at: new Date().toISOString(),
          event_id: null,
          origin: "reply",
        };
        setTimeline((previous) => insertTimelineItem(previous, reply));
        // The optimistic prompt stays until the canonical refetch replaces it:
        // if that refetch fails offline, both the prompt and the reply remain.
        void queryClient.invalidateQueries({ queryKey: ["timeline", targetConversation] });
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      } catch (cause) {
        setTimeline((previous) => ({
          items: (previous?.items ?? []).filter(
            (item) => item.item_id !== optimistic.item_id,
          ),
        }));
        void queryClient.invalidateQueries({ queryKey: ["timeline", targetConversation] });
        throw cause;
      }
    },
    convertMessage: (item): ThreadMessageLike => ({
      id: item.item_id,
      role: roleFor(item),
      content: [{ type: "text", text: item.content }],
      createdAt: new Date(item.created_at),
    }),
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-full min-h-0 flex-col">
        <header
          className="flex items-center gap-3 border-b border-line/80 bg-void/80 px-4 pb-3 backdrop-blur-md"
          style={{ paddingTop: "calc(0.75rem + env(safe-area-inset-top))" }}
        >
          <button
            type="button" onClick={onOpenSidebar} aria-label="会話一覧を開く"
            className="rounded-lg border border-line bg-panel p-1.5 text-ink-muted transition hover:text-ink md:hidden"
          >
            <Menu size={18} aria-hidden="true" />
          </button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">
              {conversationId === "default" ? "Sora" : conversationId}
            </p>
            <p className="flex items-center gap-1.5 text-[0.68rem] text-ink-muted">
              <span aria-hidden="true" className={`inline-block h-1.5 w-1.5 rounded-full ${STATUS[connectionStatus].dot}`} />
              {STATUS[connectionStatus].label}
            </p>
          </div>
        </header>

        <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
          <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-4 py-5">
            {entries.length === 0 && !timelineQuery.isLoading ? <EmptyState conversationId={conversationId} /> : null}
            {timelineQuery.isLoading ? <p className="text-center text-xs text-ink-muted">読み込み中…</p> : null}
            {timelineQuery.isError ? (
              <p className="text-center text-xs text-rose-300">タイムラインを取得できませんでした。</p>
            ) : null}
            <div className="mx-auto flex w-full max-w-3xl flex-col gap-4">
              <ThreadPrimitive.Messages>
                {({ message }) => <MessageRow key={message.id} message={message} />}
              </ThreadPrimitive.Messages>
            </div>
            <ThreadPrimitive.ViewportFooter />
          </ThreadPrimitive.Viewport>

          <div
            className="border-t border-line/80 bg-void/85 px-3 pt-3 backdrop-blur-md"
            style={{
              paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom))",
              paddingLeft: "calc(0.75rem + env(safe-area-inset-left))",
              paddingRight: "calc(0.75rem + env(safe-area-inset-right))",
            }}
          >
            <ComposerPrimitive.Root className="mx-auto flex w-full max-w-3xl items-end gap-2 rounded-2xl border border-line bg-panel px-3 py-2 focus-within:border-violet-glow/45">
              <ComposerPrimitive.Input
                aria-label="メッセージ" placeholder="メッセージを送信…" rows={1}
                className="max-h-40 min-h-8 flex-1 resize-none bg-transparent py-1 text-[0.9375rem] leading-relaxed text-ink outline-none placeholder:text-ink-muted/70"
              />
              <ComposerPrimitive.Send
                aria-label="送信"
                className="mb-0.5 rounded-xl bg-violet-glow p-2 text-[#0b0716] transition hover:bg-[#a294ff] disabled:cursor-not-allowed disabled:opacity-35"
              >
                <ArrowUp size={17} strokeWidth={2.5} aria-hidden="true" />
              </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
            {mutation.isPending ? (
              <p className="mx-auto mt-1.5 w-full max-w-3xl text-[0.68rem] text-ink-muted">Sora が考えています…</p>
            ) : null}
            {mutation.isError ? (
              <p className="mx-auto mt-1.5 w-full max-w-3xl text-[0.68rem] text-rose-300">
                {describeSendError(mutation.error)}
              </p>
            ) : null}
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}
