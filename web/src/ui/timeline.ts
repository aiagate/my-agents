import type { TimelineItem, TimelineResponse } from "../client/types";

const OPTIMISTIC_PREFIX = "optimistic:";

const compareItemIds = (left: string, right: string): number => {
  if (left === right) return 0;
  return left < right ? -1 : 1;
};

export function insertTimelineItem(
  previous: TimelineResponse | undefined,
  item: TimelineItem,
): TimelineResponse {
  const items = previous?.items ?? [];
  const existingIndex = items.findIndex(
    (existing) => existing.item_id === item.item_id,
  );
  if (existingIndex !== -1) {
    return {
      items: items.map((existing, index) =>
        index === existingIndex ? item : existing,
      ),
    };
  }
  const hasOptimistic =
    item.item_id.startsWith(OPTIMISTIC_PREFIX) ||
    items.some((existing) => existing.item_id.startsWith(OPTIMISTIC_PREFIX));
  if (hasOptimistic) {
    // A reply that lands while the user's optimistic prompt is still pending
    // must stay after it until the canonical /timeline refetch reconciles.
    return { items: [...items, item] };
  }
  return {
    items: [...items, item].sort((left, right) =>
      compareItemIds(left.item_id, right.item_id),
    ),
  };
}
