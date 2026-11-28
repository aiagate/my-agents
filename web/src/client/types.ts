export type MessageOrigin = "user" | "reply" | "proactive";
export type TimelineKind = "turn" | "event" | "event_failed";
export interface TimelineItem {
  item_id: string;
  kind: TimelineKind;
  conversation_id: string;
  content: string;
  created_at: string;
  event_id: string | null;
  origin: MessageOrigin | null;
}
export interface ConversationSummary {
  conversation_id: string;
  epoch: number;
  created_at: string;
  updated_at: string;
  message_count: number;
  event_count: number;
  last_message_preview: string;
  last_message_origin: MessageOrigin | null;
}
export interface VapidPublicKeyResponse {
  public_key: string | null;
}
export interface PushSubscriptionKeys {
  p256dh: string;
  auth: string;
}
export interface PushSubscriptionRequest {
  endpoint: string;
  keys: PushSubscriptionKeys;
  conversation_id?: string | null;
}
export interface ChatResponse {
  conversation_id: string;
  reply: string;
  message_id: string;
  surfaced_event_ids: string[];
  memory_updates: string[];
}
export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "offline";
export interface ConversationListResponse {
  conversations: ConversationSummary[];
}
export interface TimelineResponse {
  items: TimelineItem[];
}
