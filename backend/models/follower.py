"""Platform-neutral input model. None means unknown, never zero or false."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FollowerProfile:
    username: str
    followers_count: int | None = None
    following_count: int | None = None
    posts_count: int | None = None
    created_at: datetime | None = None
    is_bot: bool | None = None
    has_profile_picture: bool | None = None
    bio_length: int | None = None
    display_name: str | None = None
    last_status_at: datetime | None = None
    profile_key: str | None = None

    def __post_init__(self):
        for name in ('followers_count', 'following_count', 'posts_count', 'bio_length'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{name} must be a nonnegative integer or None')
        for name in ('is_bot', 'has_profile_picture'):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f'{name} must be a boolean or None')
        for name in ('created_at', 'last_status_at'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise ValueError(f'{name} must be a timezone-aware datetime or None')
