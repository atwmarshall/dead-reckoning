"""Data models for the application."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    """Represents an application user."""

    id: int
    username: str
    email: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True

    def deactivate(self) -> None:
        """Mark the user as inactive."""
        self.is_active = False

    def to_dict(self) -> dict:
        """Serialize the user to a dictionary."""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "created_at": self.created_at.isoformat(),
            "is_active": self.is_active,
        }


@dataclass
class Item:
    """Represents a stored item."""

    id: int
    name: str
    owner_id: int
    description: Optional[str] = None

    def __repr__(self) -> str:
        return f"Item(id={self.id}, name={self.name!r})"
