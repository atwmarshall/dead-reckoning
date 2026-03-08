"""Simple HTTP-style API handlers."""

from typing import Any

from utils import paginate


def handle_get_items(
    store: list[dict],
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Return a paginated list of items from the store."""
    items = paginate(store, page=page, page_size=page_size)
    return {
        "items": items,
        "page": page,
        "total": len(store),
    }


def handle_create_item(store: list[dict], payload: dict) -> dict:
    """Append a new item to the store and return it."""
    item = {"id": len(store) + 1, **payload}
    store.append(item)
    return item


def handle_delete_item(store: list[dict], item_id: int) -> bool:
    for i, item in enumerate(store):
        if item.get("id") == item_id:
            store.pop(i)
            return True
    return False
