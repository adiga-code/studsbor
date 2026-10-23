"""Минимальный клиент API ЮKassa v3 (https://yookassa.ru/developers/api)."""

import ipaddress
import logging
import uuid
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.money import to_api_value

log = logging.getLogger(__name__)

# Адреса, с которых ЮKassa отправляет HTTP-уведомления.
YOOKASSA_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.156.11/32",
        "77.75.156.35/32",
        "77.75.154.128/25",
        "2a02:5180::/32",
    )
]


def is_yookassa_ip(address: str | None) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip in net for net in YOOKASSA_NETWORKS)


class YooKassaError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        # Сетевые сбои и ответы 5xx можно повторить с тем же ключом идемпотентности.
        self.retryable = retryable


class PaymentGateway(Protocol):
    async def create_payment(
        self,
        *,
        amount: int,
        description: str,
        idempotence_key: str,
        return_url: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]: ...

    async def get_payment(self, payment_id: str) -> dict[str, Any]: ...

    async def create_refund(
        self, *, payment_id: str, amount: int, idempotence_key: str, description: str
    ) -> dict[str, Any]: ...

    async def get_refund(self, refund_id: str) -> dict[str, Any]: ...


class YooKassaClient:
    def __init__(self, shop_id: str, secret_key: str, base_url: str, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            base_url=base_url, auth=(shop_id, secret_key), timeout=timeout
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Ошибка соединения с ЮKassa: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise YooKassaError(
                f"ЮKassa вернула {response.status_code}: {response.text[:500]}",
                retryable=response.status_code >= 500,
            )
        return response.json()

    async def create_payment(
        self,
        *,
        amount: int,
        description: str,
        idempotence_key: str,
        return_url: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        body = {
            "amount": {"value": to_api_value(amount), "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata,
        }
        return await self._request(
            "POST", "/payments", json=body, headers={"Idempotence-Key": idempotence_key}
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payments/{payment_id}")

    async def create_refund(
        self, *, payment_id: str, amount: int, idempotence_key: str, description: str
    ) -> dict[str, Any]:
        body = {
            "payment_id": payment_id,
            "amount": {"value": to_api_value(amount), "currency": "RUB"},
            "description": description[:250],
        }
        return await self._request(
            "POST", "/refunds", json=body, headers={"Idempotence-Key": idempotence_key}
        )

    async def get_refund(self, refund_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/refunds/{refund_id}")


_gateway: PaymentGateway | None = None


def get_gateway() -> PaymentGateway:
    global _gateway
    if _gateway is None:
        settings = get_settings()
        _gateway = YooKassaClient(
            settings.yookassa_shop_id, settings.yookassa_secret_key, settings.yookassa_api_url
        )
    return _gateway


def set_gateway(gateway: PaymentGateway | None) -> None:
    """Подмена платёжного шлюза (используется в тестах)."""
    global _gateway
    _gateway = gateway


def new_idempotence_key() -> str:
    return str(uuid.uuid4())
