import { Bell, BellOff, Plus, X } from "lucide-react";
import { useState } from "react";
import type { ConversationSummary } from "../client/types";
import type { PushUiState } from "./usePush";

interface ConversationSidebarProps {
  conversations: readonly ConversationSummary[] | undefined; conversationsLoading: boolean;
  activeId: string; open: boolean; onClose: () => void; onSelect: (id: string) => void;
  pushState: PushUiState; pushBusy: boolean; onEnablePush: () => void; onDisablePush: () => void;
}

const BLOCKED_LABEL = {
  unsupported: "このブラウザは通知に未対応",
  "ios-install": "ホーム画面に追加で有効化",
  denied: "通知がブロックされています",
} as const;

const ERROR_DETAIL: Record<string, string> = {
  "Notification permission was denied.": "通知の許可が拒否されました。",
  "Notifications are not available in this browser.":
    "このブラウザは通知に未対応です。",
  "Service workers are not available in this browser.":
    "このブラウザは Service Worker に未対応です。",
  "Service worker registration timed out.":
    "Service Worker の準備がタイムアウトしました。",
  "Push subscription is missing its encryption keys.":
    "Push 購読の暗号化キーが不足しています。",
};

const detailText = (detail: string): string => ERROR_DETAIL[detail] ?? detail;

function pushView(state: PushUiState): { label: string; tone: string; action: string; isDisable: boolean; disabled: boolean } {
  switch (state.kind) {
    case "loading":
      return { label: "通知を確認中…", tone: "text-ink-muted", action: "通知をオンにする", isDisable: false, disabled: true };
    case "blocked":
      return {
        label: BLOCKED_LABEL[state.reason],
        tone: state.reason === "denied" ? "text-rose-300" : state.reason === "ios-install" ? "text-amber-200" : "text-ink-muted",
        action: "通知をオンにする", isDisable: false, disabled: true,
      };
    case "off":
      return { label: "通知オフ", tone: "text-ink-muted", action: state.canWebPush ? "通知をオンにする" : "タブ通知をオンにする", isDisable: false, disabled: false };
    case "on":
      return state.webPush
        ? { label: "通知オン", tone: "text-teal-glow", action: "通知をオフにする", isDisable: true, disabled: false }
        : { label: "タブ通知のみ有効", tone: "text-teal-glow", action: "タブ通知が有効", isDisable: false, disabled: true };
    case "error":
      return { label: "通知の設定に失敗", tone: "text-rose-300", action: "再試行", isDisable: state.failed === "disable", disabled: false };
  }
}

export function ConversationSidebar({
  conversations, conversationsLoading, activeId, open, onClose, onSelect,
  pushState, pushBusy, onEnablePush, onDisablePush,
}: ConversationSidebarProps) {
  const [draft, setDraft] = useState("");
  const view = pushView(pushState);
  const submitDraft = (): void => {
    const next = draft.trim();
    if (next === "") return;
    setDraft("");
    onSelect(next);
  };

  return (
    <>
      <button
        type="button" aria-label="会話一覧を閉じる" onClick={onClose}
        className={`fixed inset-0 z-30 bg-black/60 backdrop-blur-[2px] transition-opacity md:hidden ${open ? "opacity-100" : "pointer-events-none opacity-0"}`}
      />
      <aside
        aria-label="会話一覧"
        className={`fixed inset-y-0 left-0 z-40 flex w-[18rem] shrink-0 flex-col border-r border-line bg-panel transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        <div className="flex items-center gap-2 px-4 pt-4 pb-3">
          <span
            aria-hidden="true"
            className="h-8 w-8 rounded-full bg-[radial-gradient(circle_at_35%_30%,#cfc4ff_0%,#8f7bff_40%,#33236f_75%,#0d0f16_100%)] shadow-[0_0_20px_-4px_rgba(143,123,255,0.8)]"
          />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold tracking-tight">my-agents</p>
            <p className="flex items-center gap-1 text-[0.62rem] text-ink-muted">proactive companion</p>
          </div>
          <button
            type="button" onClick={onClose} aria-label="会話一覧を閉じる"
            className="rounded-lg border border-line p-1 text-ink-muted transition hover:text-ink md:hidden"
          >
            <X size={15} aria-hidden="true" />
          </button>
        </div>

        <form
          className="mx-3 mb-3 flex items-center gap-1.5"
          onSubmit={(event) => { event.preventDefault(); submitDraft(); }}
        >
          <input
            value={draft} onChange={(event) => setDraft(event.target.value)}
            placeholder="新しい会話 ID" aria-label="新しい会話 ID"
            className="min-w-0 flex-1 rounded-lg border border-line bg-void/70 px-2.5 py-1.5 text-[0.75rem] text-ink outline-none placeholder:text-ink-muted/60 focus:border-violet-glow/50"
          />
          <button
            type="submit" aria-label="会話を作成"
            className="rounded-lg border border-line bg-panel-raised p-1.5 text-ink-muted transition hover:border-violet-glow/40 hover:text-ink"
          >
            <Plus size={14} aria-hidden="true" />
          </button>
        </form>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {conversationsLoading ? <p className="px-2 py-3 text-[0.7rem] text-ink-muted">読み込み中…</p> : null}
          {conversations !== undefined && conversations.length === 0 ? (
            <p className="px-2 py-3 text-[0.7rem] text-ink-muted">会話はまだありません。</p>
          ) : null}
          <div className="flex flex-col gap-0.5">
            {conversations?.map((conversation) => {
              const active = conversation.conversation_id === activeId;
              return (
                <button
                  key={conversation.conversation_id} type="button" aria-current={active ? "true" : undefined}
                  onClick={() => onSelect(conversation.conversation_id)}
                  className={active
                    ? "w-full rounded-xl border border-violet-glow/40 bg-violet-glow/10 px-3 py-2 text-left"
                    : "w-full rounded-xl border border-transparent px-3 py-2 text-left transition hover:border-line hover:bg-panel-raised"}
                >
                  <span className="block truncate text-[0.82rem] font-medium text-ink">{conversation.conversation_id}</span>
                  <span className="mt-0.5 block truncate text-[0.7rem] text-ink-muted">
                    {conversation.last_message_preview || "まだメッセージはありません"}
                  </span>
                  <span className="mt-1 block text-[0.6rem] tracking-wide text-ink-muted/70">
                    {conversation.message_count} msgs · {conversation.event_count} events
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="border-t border-line px-3 py-3">
          <div className="rounded-xl border border-line bg-void/60 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-[0.72rem] font-medium">
                {pushState.kind === "on" ? <Bell size={13} aria-hidden="true" /> : <BellOff size={13} aria-hidden="true" />}
                プロアクティブ通知
              </span>
              <span className={`text-right text-[0.62rem] ${view.tone}`}>{view.label}</span>
            </div>
            {pushState.kind === "blocked" && pushState.reason === "ios-install" ? (
              <p className="mt-1 text-[0.6rem] leading-snug text-amber-200/80">
                共有 → ホーム画面に追加 で起動すると Web Push を有効化できます。
              </p>
            ) : null}
            {pushState.kind === "error" ? (
              <p className="mt-1 text-[0.6rem] leading-snug text-rose-300/80">{detailText(pushState.detail)}</p>
            ) : null}
            <button
              type="button" disabled={view.disabled || pushBusy}
              onClick={view.isDisable ? onDisablePush : onEnablePush}
              className="mt-2 w-full rounded-lg border border-line bg-panel-raised px-2 py-1.5 text-[0.7rem] text-ink transition enabled:hover:border-violet-glow/45 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {view.action}
            </button>
          </div>
        </div>
      </aside>
    </>
  );
}
