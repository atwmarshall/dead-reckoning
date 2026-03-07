"""Application entry point."""

from config import get_config
from models import Item, User
from utils import paginate, slugify


def create_user(user_id: int, username: str, email: str) -> User:
    """Factory for creating new User instances."""
    return User(id=user_id, username=username, email=email)


def display_items(items: list[Item], page: int = 1) -> None:
    """Print a paginated list of items to stdout."""
    page_items = paginate(items, page=page)
    for item in page_items:
        slug = slugify(item.name)
        print(f"  [{item.id}] {item.name} (slug: {slug})")


def run() -> None:
    """Bootstrap and run the application."""
    config = get_config()
    print(f"Starting app (debug={config['debug']})")

    user = create_user(1, "alice", "alice@example.com")
    print(f"Created user: {user.username}")

    items = [Item(id=i, name=f"Widget {i}", owner_id=user.id) for i in range(1, 5)]
    display_items(items)


if __name__ == "__main__":
    run()
