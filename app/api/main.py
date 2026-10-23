"""HTTP-сервер: приём уведомлений ЮKassa и проверка работоспособности."""

import hmac
import logging
from contextlib import asynccontextmanager

from aiogram import Bot
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.bot.main import create_bot
from app.bot.sender import notify_payment_succeeded, notify_refund_succeeded
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.logging_setup import setup_logging
from app.services.payments import PaymentResult, RefundResult, handle_notification
from app.services.yookassa import YooKassaError, is_yookassa_ip

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("api")
    app.state.bot = create_bot() if get_settings().bot_token else None
    yield
    if app.state.bot is not None:
        await app.state.bot.session.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Фонд студсовета", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/yookassa/webhook/{secret}")
    async def yookassa_webhook(secret: str, request: Request) -> JSONResponse:
        settings = get_settings()
        if not settings.yookassa_webhook_secret or not hmac.compare_digest(
            secret, settings.yookassa_webhook_secret
        ):
            raise HTTPException(status_code=404)
        client_ip = request.headers.get(settings.real_ip_header) or (
            request.client.host if request.client else None
        )
        if settings.yookassa_check_ip and not is_yookassa_ip(client_ip):
            log.warning("Уведомление с недоверенного адреса %s отклонено", client_ip)
            raise HTTPException(status_code=403)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400) from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400)

        async with get_sessionmaker()() as session:
            try:
                result = await handle_notification(session, body)
            except YooKassaError as exc:
                # Ответ не 200 — ЮKassa повторит уведомление позже.
                log.error("Не удалось проверить уведомление %s: %s", body.get("event"), exc)
                raise HTTPException(status_code=503) from exc

            bot: Bot | None = request.app.state.bot
            if bot is not None:
                if isinstance(result, PaymentResult) and result.newly_succeeded:
                    await notify_payment_succeeded(bot, session, result.payment)
                elif isinstance(result, RefundResult) and result.newly_succeeded:
                    await notify_refund_succeeded(bot, session, result.refund)
        return JSONResponse({"ok": True})

    return app


app = create_app()
