"""Хранилище FSM aiogram в PostgreSQL (таблица fsm_states)."""

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, KeyBuilder, StateType, StorageKey
from sqlalchemy.exc import IntegrityError

from app.db.models import FsmState
from app.db.session import get_sessionmaker


class SqlStorage(BaseStorage):
    def __init__(self, key_builder: KeyBuilder | None = None) -> None:
        self.key_builder = key_builder or DefaultKeyBuilder()

    async def _save(self, key: StorageKey, **values: Any) -> None:
        db_key = self.key_builder.build(key)
        async with get_sessionmaker()() as session:
            row = await session.get(FsmState, db_key)
            if row is None:
                row = FsmState(key=db_key, state=None, data="{}")
                session.add(row)
            for name, value in values.items():
                setattr(row, name, value)
            try:
                await session.commit()
            except IntegrityError:
                # Параллельный запрос успел создать строку — повторяем как обновление.
                await session.rollback()
                row = await session.get(FsmState, db_key)
                for name, value in values.items():
                    setattr(row, name, value)
                await session.commit()

    async def _load(self, key: StorageKey) -> FsmState | None:
        async with get_sessionmaker()() as session:
            return await session.get(FsmState, self.key_builder.build(key))

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._save(key, state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._load(key)
        return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self._save(key, data=json.dumps(dict(data), ensure_ascii=False))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._load(key)
        return json.loads(row.data) if row and row.data else {}

    async def close(self) -> None:
        pass
