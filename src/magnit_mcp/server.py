"""MCP-сервер Магнита: только чтение каталога.

Запуск:
  python -m magnit_mcp.server                        # stdio (локальные клиенты, tunnel-client)
  python -m magnit_mcp.server --transport http       # streamable HTTP на /mcp

Переменные окружения:
  MAGNIT_MCP_TOKEN       Bearer-токен в заголовке Authorization (рекомендуется при публичном хосте)
  MAGNIT_MCP_HOST        адрес прослушивания (по умолчанию 127.0.0.1)
  MAGNIT_MCP_PORT        порт (по умолчанию 8000)
  MAGNIT_ALLOWED_HOSTS   через запятую, например: mcp.example.com
  MAGNIT_MCP_STATELESS   1 — без состояния (если клиент не держит сессию)
"""

from __future__ import annotations

import os
import asyncio
import hmac
from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, fields
from typing import get_type_hints
from threading import BoundedSemaphore
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, StrictInt, create_model

from . import core

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

INSTRUCTIONS = """Проверка продуктовой корзины в Магните по адресу. Только чтение: ничего не
бронируется, не кладётся в корзину и не оплачивается.

Обязательная последовательность: сначала find_delivery_store с координатами адреса
(широта/долгота), затем search_products или check_basket с теми же координатами.
Не подставляйте магазин по памяти и не выбирайте товар за пользователя: если
check_basket вернул status=ambiguous или needs_clarification, задайте вопрос и
повторите вызов с уточнённым query.

Деньги и количества считает сервер. Не пересчитывайте итог самостоятельно и не
называйте сумму полной, если summary.complete=false. Суммы не включают доставку и
условные акции; товары не зарезервированы.
"""


# MCP-схема строится из общей модели; StrictInt запрещает bool и дробные числа.
_item_types = get_type_hints(core.BasketItem)
BasketItem = create_model(
    "BasketItem",
    __config__=ConfigDict(extra="forbid"),
    **{f.name: (StrictInt if f.name == "quantity" else _item_types[f.name],
                ... if f.default is MISSING else f.default) for f in fields(core.BasketItem)},
)
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="catalog")
_SLOTS = BoundedSemaphore(4)


async def _run_blocking(fn, *args):
    # Слот остаётся занят до завершения потока даже при отмене/таймауте клиента.
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("server_busy: уже выполняются 4 запроса")

    def work():
        try:
            return core.request_budget(fn)(*args)
        finally:
            _SLOTS.release()

    try:
        future = _EXECUTOR.submit(work)
    except BaseException:
        _SLOTS.release()
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), core.REQUEST_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ValueError("request_timeout: превышено общее время запроса") from exc


class BearerAuth:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = dict(scope.get("headers", []))
            expected = ("Bearer " + self.token).encode()
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                from starlette.responses import Response
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app():
    return BearerAuth(mcp.streamable_http_app(), os.environ.get("MAGNIT_MCP_TOKEN", "").strip())


def _security() -> TransportSecuritySettings:
    raw = os.environ.get("MAGNIT_ALLOWED_HOSTS", "").strip()
    if raw:
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        if any("*" in h or "/" in h for h in hosts):
            raise ValueError("MAGNIT_ALLOWED_HOSTS: укажите точные хосты, без wildcard и схемы URL")
        origins = [f"https://{h}" for h in hosts]
        return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)
    # Значения по умолчанию у самого класса пустые, поэтому localhost задаём явно,
    # иначе локальная проверка падает с «Invalid Host header».
    return TransportSecuritySettings(
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1", "http://localhost", "http://127.0.0.1:*", "http://localhost:*"],
    )


mcp = FastMCP(
    "magnit-catalog",
    instructions=INSTRUCTIONS,
    host=os.environ.get("MAGNIT_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MAGNIT_MCP_PORT", "8000")),
    streamable_http_path="/mcp",
    stateless_http=os.environ.get("MAGNIT_MCP_STATELESS", "") == "1",
    transport_security=_security(),
)


def _store(lat: float, lon: float, service: str) -> dict:
    try:
        shops = core.get_shops(lat, lon)
    except RuntimeError as e:
        raise ValueError(f"catalog_unavailable: {e}") from e
    shop = core.pick_store(shops, lat, lon, service)
    if not shop:
        raise ValueError(
            f"no_delivery_zone: нет магазина {service} у точки {lat},{lon}"
        )
    return core.store_context(shop, lat, lon, service)


@mcp.tool(name="find_delivery_store", annotations=READ_ONLY)
async def find_delivery_store(lat: float, lon: float, service: str = "express") -> dict:
    """Магазин, который обслуживает точку. Вызывать первым.

    lat — широта, lon — долгота. service: express (продуктовая доставка) по умолчанию.
    Возвращает storeCode, адрес и расстояние; storeCode нужен остальным инструментам
    неявно — они принимают те же координаты.
    """
    ctx = await _run_blocking(_store, lat, lon, service)
    return {"store": ctx}


def _search_products(
    lat: float,
    lon: float,
    term: str,
    limit: int = 10,
    service: str = "express",
) -> dict:
    """Поиск товаров в магазине у точки. Возвращает компактный список.

    Используйте для уточнения формулировок и чтобы показать пользователю варианты.
    price_rub — цена за упаковку; quantity — доступное количество по данным сайта.
    """
    ctx = _store(lat, lon, service)
    page = core.search(ctx["storeCode"], term, 0, max(1, min(limit, 36)))
    items = [
        {
            "productId": i["productId"],
            "name": i["name"],
            "price_rub": str(core._money(i["price"])),
            "quantity": i["quantity"],
            "pack_size": (lambda p: f"{p[0]} {p[1]}" if p else None)(core.pack_size(i["name"])),
        }
        for i in page.get("items", []) if core.delivery_available(i)
    ]
    return {
        "store": ctx,
        "term": term,
        "total_count": page.get("pagination", {}).get("totalCount"),
        "has_more": page.get("pagination", {}).get("hasMore"),
        "items": items,
    }


@mcp.tool(name="search_products", annotations=READ_ONLY)
async def search_products(lat: float, lon: float, term: str, limit: int = 10, service: str = "express") -> dict:
    """Поиск доступных товаров для доставки в магазине у точки."""
    return await _run_blocking(_search_products, lat, lon, term, limit, service)


@mcp.tool(name="check_basket", annotations=READ_ONLY)
async def check_basket(
    lat: float,
    lon: float,
    items: list[BasketItem],
    pick: str = "ask",
    service: str = "express",
) -> dict:
    """Проверить корзину: подбор товаров, наличие, цены и предварительный итог.

    pick=ask (по умолчанию) не выбирает товар за пользователя и возвращает
    candidates при неоднозначности; pick=first/cheapest — явный выбор стратегии.
    summary.complete=true только если все строки доступны и однозначны;
    full_basket_total_rub заполняется только тогда, иначе смотрите
    available_subtotal_rub. Ничего не резервируется.
    """
    payload = core.validate_items([{k: v for k, v in i.model_dump(exclude_none=True).items() if v is not None} for i in items])
    def work():
        ctx = _store(lat, lon, service)
        return core.check(ctx, payload, pick)
    return await _run_blocking(work)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--transport" in argv:
        mode = argv[argv.index("--transport") + 1]
        if mode not in ("http", "streamable-http"):
            print("поддерживается --transport http", file=sys.stderr)
            return 2
        if not os.environ.get("MAGNIT_MCP_TOKEN", "").strip() and os.environ.get(
            "MAGNIT_MCP_HOST", "127.0.0.1"
        ) not in ("127.0.0.1", "localhost"):
            print(
                "Публичный HTTP требует MAGNIT_MCP_TOKEN (Authorization: Bearer)",
                file=sys.stderr,
            )
            return 2
        import uvicorn
        uvicorn.run(http_app(), host=mcp.settings.host, port=mcp.settings.port, access_log=False)
        return 0
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
