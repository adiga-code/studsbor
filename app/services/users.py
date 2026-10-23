from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, AdminRole, User


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    owner_ids: list[int] | None = None,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    changed = False
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            display_name=(first_name or "")[:64] or None,
            is_anonymous=False,
            reminders_enabled=True,
            reminder_period_months=1,
            is_blocked=False,
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError:
            # Параллельное обновление от того же пользователя успело его создать.
            await session.rollback()
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        changed = True
    else:
        if user.username != username or user.first_name != first_name:
            user.username = username
            user.first_name = first_name
            changed = True
        if user.is_blocked:
            # Пользователь снова пишет боту — значит, разблокировал его.
            user.is_blocked = False
            changed = True

    if owner_ids and telegram_id in owner_ids:
        admin = await session.get(Admin, user.id)
        if admin is None:
            session.add(Admin(user_id=user.id, role=AdminRole.OWNER))
            changed = True
        elif admin.role != AdminRole.OWNER:
            admin.role = AdminRole.OWNER
            changed = True

    if changed:
        await session.commit()
        await session.refresh(user, ["admin"])
    return user


async def get_role(session: AsyncSession, user: User) -> AdminRole | None:
    admin = await session.get(Admin, user.id)
    return AdminRole(admin.role) if admin else None


async def find_users(session: AsyncSession, query: str, limit: int = 10) -> list[User]:
    query = query.strip().lstrip("@")
    conditions = [User.username.ilike(f"%{query}%"), User.display_name.ilike(f"%{query}%")]
    if query.isdigit():
        conditions += [User.telegram_id == int(query), User.id == int(query)]
    rows = await session.scalars(select(User).where(or_(*conditions)).order_by(User.id).limit(limit))
    return list(rows)


async def set_role(
    session: AsyncSession, user: User, role: AdminRole | None, granted_by: User
) -> None:
    admin = await session.get(Admin, user.id)
    if role is None:
        if admin is not None:
            await session.delete(admin)
    elif admin is None:
        session.add(Admin(user_id=user.id, role=role, granted_by=granted_by.id))
    else:
        admin.role = role
        admin.granted_by = granted_by.id
    await session.commit()


async def list_admins(session: AsyncSession) -> list[tuple[User, str]]:
    rows = await session.execute(
        select(User, Admin.role).join(Admin, Admin.user_id == User.id).order_by(Admin.created_at)
    )
    return [(user, role) for user, role in rows.all()]
