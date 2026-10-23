from aiogram.filters import Filter
from aiogram.types import TelegramObject

from app.db.models import AdminRole


class IsAdmin(Filter):
    """Пропускает администраторов; с owner_only=True — только владельцев."""

    def __init__(self, owner_only: bool = False):
        self.owner_only = owner_only

    async def __call__(self, event: TelegramObject, role: AdminRole | None = None) -> bool:
        if role is None:
            return False
        return role == AdminRole.OWNER if self.owner_only else True
