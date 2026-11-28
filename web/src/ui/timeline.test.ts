import { describe, expect, it } from "vitest";
import type { MessageOrigin, TimelineItem } from "../client/types";
import { insertTimelineItem } from "./timeline";

const FIRST = "01a2204a-0000-7000-8000-000000000001";
const SECOND = "01a2204a-0000-7000-8000-000000000002";
const THIRD = "01a2204a-0000-7000-8000-000000000003";

function item(
  itemId: string,
  origin: MessageOrigin = "reply",
  overrides: Partial<TimelineItem> = {},
): TimelineItem {
  return {
    item_id: itemId,
    kind: "turn",
    conversation_id: "default",
    content: itemId,
    created_at: "2025-01-01T00:00:00+00:00",
    event_id: null,
    origin,
    ...overrides,
  };
}

describe("insertTimelineItem", () => {
  it("inserts persisted items in ascending item_id order", () => {
    const withThird = insertTimelineItem(undefined, item(THIRD));
    const withFirst = insertTimelineItem(withThird, item(FIRST));
    const withSecond = insertTimelineItem(withFirst, item(SECOND));

    expect(withSecond.items.map((entry) => entry.item_id)).toEqual([
      FIRST,
      SECOND,
      THIRD,
    ]);
  });

  it("replaces an existing item in place", () => {
    const updated = item(FIRST, "reply", { content: "updated" });
    const response = insertTimelineItem(
      { items: [item(FIRST), item(THIRD)] },
      updated,
    );

    expect(response.items).toHaveLength(2);
    expect(response.items[0]).toBe(updated);
    expect(response.items[1]?.item_id).toBe(THIRD);
  });

  it("keeps a pending optimistic prompt before a reply that arrives early", () => {
    const pending = item("optimistic:0001", "user", { content: "質問" });
    const withPending = insertTimelineItem(undefined, pending);

    const withReply = insertTimelineItem(withPending, item(FIRST, "reply", {
      content: "返信",
    }));

    expect(withReply.items.map((entry) => entry.item_id)).toEqual([
      "optimistic:0001",
      FIRST,
    ]);
  });

  it("appends an optimistic prompt after persisted items", () => {
    const response = insertTimelineItem(
      { items: [item(FIRST)] },
      item("optimistic:0001", "user"),
    );

    expect(response.items.map((entry) => entry.item_id)).toEqual([
      FIRST,
      "optimistic:0001",
    ]);
  });
});
