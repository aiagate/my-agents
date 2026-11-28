import type { ConnectionStatus, TimelineItem } from "./types";
export interface ConversationStreamOptions {
  conversationId: string;
  onItem: (item: TimelineItem) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
  onReconnect?: () => void;
}
export class ConversationStream {
  private readonly source: EventSource;
  private opened = false;
  private closed = false;
  constructor(private readonly options: ConversationStreamOptions) {
    options.onStatusChange?.("connecting");
    const query = new URLSearchParams({ conversation_id: options.conversationId });
    this.source = new EventSource(`/stream?${query.toString()}`);
    this.source.onopen = () => {
      if (this.closed) {
        return;
      }
      this.options.onStatusChange?.("live");
      if (this.opened) {
        this.options.onReconnect?.();
      }
      this.opened = true;
    };
    this.source.onmessage = (event) => {
      this.handleMessage(event);
    };
    this.source.onerror = () => {
      if (this.closed) {
        return;
      }
      const isClosed =
        typeof EventSource !== "undefined" &&
        this.source.readyState === EventSource.CLOSED;
      if (isClosed) {
        this.options.onStatusChange?.("offline");
        return;
      }
      const offline =
        typeof navigator !== "undefined" && navigator.onLine === false;
      this.options.onStatusChange?.(offline ? "offline" : "reconnecting");
    };
  }
  close(): void {
    this.closed = true;
    this.source.close();
  }
  private handleMessage(event: MessageEvent<string>): void {
    if (this.closed) {
      return;
    }
    try {
      this.options.onItem(JSON.parse(event.data) as TimelineItem);
    } catch {
      return;
    }
  }
}
