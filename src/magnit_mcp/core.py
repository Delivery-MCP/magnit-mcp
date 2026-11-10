#!/usr/bin/env python3
"""magnit_check.py — read-only проверка корзины в Магните по адресу.

Только чтение каталога: ничего не бронирует, не кладёт в корзину, не платит.
Зависимостей нет — стандартная библиотека Python 3.9+.

Подкоманды:
  stores  --lat --lon [--service express]   магазины доставки у точки
  search  --lat --lon --term "молоко"       сырая выдача поиска
  check   --lat --lon --items items.json    проверка корзины и расчёт

Проверка корзины:
  python3 magnit_check.py check --lat 51.6136 --lon 45.9833 \
      --items examples/items.json --human
"""

from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
from time import monotonic
from typing import ClassVar, Literal
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

BASE = "https://magnit.ru"
TIMEOUT = 25
STOP = {"с", "в", "и", "на", "для", "от", "по", "из", "шт", "гр"}


MAX_BASKET_ITEMS = 50
REQUEST_SECONDS = 30
MAX_SEARCH_PAGES = 5
_DEADLINE: ContextVar[float | None] = ContextVar("deadline", default=None)


def remaining_time() -> float:
    deadline = _DEADLINE.get()
    remaining = deadline - monotonic() if deadline is not None else REQUEST_SECONDS
    if remaining <= 0:
        raise TimeoutError("превышено общее время запроса")
    return remaining


def request_budget(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _DEADLINE.set(_DEADLINE.get() or monotonic() + REQUEST_SECONDS)
        try:
            remaining_time()
            return fn(*args, **kwargs)
        finally:
            _DEADLINE.reset(token)
    return wrapped


@dataclass
class BasketItem:
    """Общая схема CLI, автономного скрипта и MCP; неизвестные поля запрещены."""
    __pydantic_config__: ClassVar[dict] = {"extra": "forbid"}
    query: str
    quantity: int = 1
    unit: Literal["pack", "piece"] = "pack"
    line_id: str | None = None
    label: str | None = None
    pick: Literal["ask", "first", "cheapest"] | None = None
    prefer: Literal["max_fat"] | None = None
    unverified: list[str] | None = None

    def __post_init__(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 300:
            raise ValueError("query: требуется непустая строка до 300 символов")
        if type(self.quantity) is not int or not 0 < self.quantity <= 10000:
            raise ValueError("quantity: требуется положительное целое число до 10000")
        if self.unit not in ("pack", "piece"):
            raise ValueError("unit: допустимы pack, piece")
        if self.pick not in (None, "ask", "first", "cheapest") or self.prefer not in (None, "max_fat"):
            raise ValueError("неизвестная стратегия подбора")
        for value in (self.line_id, self.label):
            if value is not None and (not isinstance(value, str) or len(value) > 300):
                raise ValueError("line_id/label: требуется строка до 300 символов")
        if self.unverified is not None and (not isinstance(self.unverified, list) or
                len(self.unverified) > 10 or any(not isinstance(x, str) or len(x) > 300 for x in self.unverified)):
            raise ValueError("unverified: требуется список строк")


def validate_items(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BASKET_ITEMS:
        raise ValueError(f"корзина должна содержать от 1 до {MAX_BASKET_ITEMS} строк")
    try:
        return [{k: v for k, v in asdict(BasketItem(**line)).items() if v is not None} for line in items]
    except TypeError as exc:
        raise ValueError(f"неверные поля позиции: {exc}") from exc


# ---------------------------------------------------------------- транспорт
@request_budget
def _request(url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    last: Exception | None = None
    for _ in range(2):  # один повтор только для сетевых сбоев
        try:
            with urllib.request.urlopen(req, timeout=min(TIMEOUT, remaining_time())) as r:
                chunks = []
                size = 0
                while True:
                    remaining_time()
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise RuntimeError("слишком большой ответ каталога")
                    chunks.append(chunk)
                remaining_time()
                return json.loads(b"".join(chunks).decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
    raise RuntimeError(f"запрос не удался: {last}")


def get_shops(lat: float, lon: float) -> list[dict]:
    # ВНИМАНИЕ: у endpoint'а x — широта, y — долгота (не наоборот).
    url = f"{BASE}/webgate/shops/v1/shop_by_point?x={lat}&y={lon}"
    return _request(url).get("shops", [])


GEOCODE_UA = "magnit-basket-check/0.1 (personal, non-commercial)"


def geocode(address: str, limit: int = 3) -> list[dict]:
    """Адрес → координаты. Публичный Photon (OpenStreetMap), без ключа.

    Это внешний сервис: адрес уходит на photon.komoot.io. Для личного
    использования; при больших объёмах нужен свой геокодер.
    """
    # Photon не принимает lang=ru: поддерживаются только de/en/fr/it, иначе HTTP 400.
    params = {"q": address, "limit": limit}
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_UA, "Accept": "application/json"})
    try:
        data = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    except Exception as e:  # noqa: BLE001 — сеть/формат, дальше решает вызывающий
        raise RuntimeError(f"geocode_failed: {e}") from e

    out = []
    for f in data.get("features", []):
        coords = f.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        p = f.get("properties", {})
        parts = [p.get("name"), p.get("street"), p.get("housenumber"), p.get("city"), p.get("state")]
        label = ", ".join(dict.fromkeys(x for x in parts if x))
        out.append({"lat": coords[1], "lon": coords[0], "label": label, "type": p.get("type")})
    return out


def search(store: str, term: str, offset: int = 0, limit: int = 36) -> dict:
    payload = {
        "includeAdultGoods": True,
        "pagination": {"offset": offset, "limit": limit},
        "sort": {"order": "desc", "type": "popularity"},
        "term": term,
        "storeCode": store,
        "storeType": "express",
        "catalogType": "2",
    }
    return _request(f"{BASE}/webgate/v2/goods/search", payload)


# ------------------------------------------------------------------ магазин
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = r2 - r1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def pick_store(shops: list[dict], lat: float, lon: float, service: str) -> dict | None:
    usable = [
        s
        for s in shops
        if s.get("active") and s.get("delivery") and s.get("delivery_type") == service
    ]
    if not usable:
        return None
    return min(
        usable,
        key=lambda s: haversine(
            lat, lon, s["coordinates"]["x"], s["coordinates"]["y"]
        ),
    )


def store_context(shop: dict, lat: float, lon: float, service: str) -> dict:
    return {
        "storeCode": shop["xml_id"],
        "storeType": service,
        "catalogType": "2",
        "address": shop.get("formatted_address", ""),
        "name": shop.get("name", ""),
        "distance_m": round(
            haversine(lat, lon, shop["coordinates"]["x"], shop["coordinates"]["y"])
        ),
        "source_point": {"lat": lat, "lon": lon},
    }


# ------------------------------------------------------------- сопоставление
_CONFUSABLE = str.maketrans({"c": "с", "a": "а", "b": "в", "e": "е", "o": "о", "p": "р", "x": "х"})


def norm(text: str) -> str:
    s = text.lower().replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    return " ".join(s.split())


def _token(t: str) -> str:
    # Латинские "C1"/"CO" в выдаче встречаются наравне с кириллическими.
    return t.translate(_CONFUSABLE) if len(t) <= 3 else t


def tokens(text: str) -> list[str]:
    return [_token(t) for t in norm(text).split() if t not in STOP]


def _prefix(tok: str) -> str:
    """Грубая основа для русских словоформ: яйца→яйц, молоко→молок, печенье→печен."""
    n = len(tok)
    return tok[: 3 if n <= 4 else (n - 1 if n == 5 else 5)]


def _token_variants(tok: str) -> tuple[str, ...]:
    return (tok,) + SYNONYM_STEMS.get(_prefix(tok), ())


def match(query_tokens: list[str], name: str) -> tuple[float, bool]:
    """(доля совпавших токенов, совпадает ли категория в начале названия).

    Второй флаг лечит классический промах: «молоко» не должно матчить
    «Трубочки Три кота для молока», где слово есть, но товар другой.
    """
    words = [_token(w) for w in norm(name).split()]
    if not query_tokens or not words:
        return 0.0, False

    def hit(tok: str) -> bool:
        return any(
            w.startswith(_prefix(v)) for v in _token_variants(tok) for w in words
        )

    hits = sum(1 for t in query_tokens if hit(t))

    # Категория должна стоять в самом начале названия. Второе слово допускаем только
    # для прямого совпадения («Куриное яйцо»), но не для синонима: иначе
    # «помидоры» превращаются в «Соус томатный».
    head = any(
        words[i].startswith(_prefix(query_tokens[0])) for i in range(min(2, len(words)))
    )
    if not head:
        head = any(
            words[0].startswith(_prefix(v))
            for v in _token_variants(query_tokens[0])[1:]
        )
    return hits / len(query_tokens), head


# Синонимы, из-за которых поиск находит товар, а сопоставление по словам — нет.
# Намеренно короткий список: только то, что реально встретилось в каталоге.
SYNONYM_STEMS: dict[str, tuple[str, ...]] = {
    "помид": ("томат",),
    "томат": ("помид",),
}


def is_perfect(query_tokens: list[str], name: str) -> bool:
    s, head = match(query_tokens, name)
    return s == 1.0 and head


PACK_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(шт|г|кг|мл|л)\b")


def pack_size(name: str) -> tuple[Decimal, str] | None:
    m = PACK_RE.search(norm(name))
    if not m:
        return None
    return Decimal(m.group(1).replace(",", ".")), m.group(2)


FAT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def fat_percent(name: str) -> Decimal | None:
    """Жирность из названия: «Сливки 33%» → 33. Нет данных — None, а не ноль."""
    # norm() вырезает «%», поэтому ищем по исходной строке.
    m = FAT_RE.search(name.lower().replace("ё", "е"))
    return Decimal(m.group(1).replace(",", ".")) if m else None


# ------------------------------------------------- разбор списка строками
CHEAP_RE = re.compile(r"(самы[йеха]\w*\s+дешев\w*|подешевле|дешевле|самое дешевое)", re.IGNORECASE)
FATTY_RE = re.compile(r"(самы[йеха]\w*\s+жирн\w*|пожирнее|жирнее|наибольш\w*\s+жирност\w*)", re.IGNORECASE)

# Признаки, которые нельзя проверить по каталогу: цвет банки или этикетки, тип упаковки.
UNVERIFIABLE_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"в\s+красн\w*\s+банк\w*", re.IGNORECASE), "цвет банки"),
    (re.compile(r"в\s+син\w*\s+банк\w*", re.IGNORECASE), "цвет банки"),
    (re.compile(r"с\s+зелен\w*\s+этикетк\w*|с\s+зелён\w*\s+этикетк\w*", re.IGNORECASE), "цвет этикетки"),
    (re.compile(r"с\s+красн\w*\s+этикетк\w*", re.IGNORECASE), "цвет этикетки"),
    (re.compile(r"в\s+коробк\w*", re.IGNORECASE), "тип упаковки (коробка)"),
    (re.compile(r"в\s+стекл\w*", re.IGNORECASE), "материал упаковки (стекло)"),
]


def parse_basket_text(text: str) -> list[dict]:
    """Список покупок строками → позиции корзины.

    Правила по умолчанию (согласованы с пользователем):
    - количество не указано → 1 упаковка;
    - бренд не указан → подходит любой, берём самый популярный вариант;
    - «самый дешёвый» → внутри строки выбирается минимальная цена;
    - «самые жирные» → максимальная жирность из найденных;
    - цвет банки/этикетки и тип упаковки по каталогу не проверяются: строка
      возвращается как ambiguous с кандидатами, а не с выдуманным ответом.
    """
    items: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•* \t")
        if not line:
            continue
        query = line
        pick = "first"
        prefer = None
        unverified: list[str] = []

        if CHEAP_RE.search(query):
            pick = "cheapest"
            query = CHEAP_RE.sub(" ", query)
        if FATTY_RE.search(query):
            prefer = "max_fat"
            query = FATTY_RE.sub(" ", query)
        for pattern, human in UNVERIFIABLE_RE:
            if pattern.search(query):
                if human not in unverified:
                    unverified.append(human)
                query = pattern.sub(" ", query)

        quantity, unit = 1, "pack"
        amount = re.search(r"(?<![\w.,-])([+-]?\d+(?:[.,]\d+)?)\s*(штук|штуки|штука|шт\.?|упаковок|упаковки|упаковка|упак\.?|пачки|пачек|пачка)(?!\w)", query, re.I)
        if amount:
            value = Decimal(amount.group(1).replace(",", "."))
            if value <= 0 or value != value.to_integral_value():
                raise ValueError("количество должно быть положительным целым")
            quantity = int(value)
            unit = "piece" if amount.group(2).lower().startswith("ш") else "pack"
            query = query[:amount.start()] + " " + query[amount.end():]
        query = " ".join(query.split()).strip(" ,;")
        if not query:
            query = line

        item = {
            "line_id": f"line{len(items) + 1}",
            "label": line,
            "query": query,
            "quantity": quantity,
            "unit": unit,
            "pick": pick,
        }
        if prefer:
            item["prefer"] = prefer
        if unverified:
            item["unverified"] = unverified
        items.append(item)
    return items


# ------------------------------------------------------------------ расчёт
def _money(minor: int) -> Decimal:
    return (Decimal(minor) / Decimal(100)).quantize(Decimal("0.01"))


def product_url(product_id: str) -> str:
    """Страница товара: /product/<id> отдаёт 200, несуществующий id — 404."""
    return f"{BASE}/product/{product_id}"


def _candidate(item: dict) -> dict:
    return {
        "productId": item["productId"],
        "url": product_url(item["productId"]),
        "name": item["name"],
        "price_minor": item["price"],
        "price_rub": str(_money(item["price"])),
        "reported_quantity": item["quantity"],
        "pickupOnly": item.get("pickupOnly", False),
    }


def _packs_for(item: dict, qty: float, unit: str) -> int | None:
    if unit != "piece":
        return int(qty)
    ps = pack_size(item["name"])
    if not ps or ps[1] != "шт":
        return None
    per = int(ps[0])
    return math.ceil(qty / per) if per > 0 else None


def delivery_available(item: dict) -> bool:
    return int(item.get("quantity") or 0) > 0 and not item.get("pickupOnly", False)


def pick_relevant(items: list[dict], qty: float, unit: str) -> dict | None:
    """Самый релевантный вариант — для строки, где нужно показать несколько.

    Порядок items — это порядок выдачи поиска, то есть популярность. Правило:
    сначала доступные (quantity > 0), затем выше по популярности; вариант, у
    которого нельзя посчитать упаковки, пропускается.
    """
    if not items:
        return None
    available = [i for i in items if delivery_available(i)]
    for item in available:
        if _packs_for(item, qty, unit):
            return item
    return None


def _ambiguous_payload(line: dict, base: dict, pool: list[dict], qty: float, unit: str, reason: str) -> dict:
    """Общая сборка ответа для неоднозначной строки, включая вариант для итога."""
    payload = {
        **base,
        "status": "ambiguous",
        "reason": reason,
        "candidates": [_candidate(i) for i in pool[:5]],
    }
    suggestion = pick_relevant(pool, qty, unit)
    if suggestion:
        packs = _packs_for(suggestion, qty, unit) or int(qty)
        payload["suggested"] = _candidate(suggestion)
        payload["suggested_packages"] = packs
        payload["suggested_total_rub"] = str(_money(suggestion["price"] * packs))
    return payload


def resolve_line(ctx: dict, line: dict, pick: str) -> dict:
    lid = line.get("line_id") or line["query"]
    # Количество по умолчанию — 1 упаковка: так задано для формата списка строками.
    qty = line.get("quantity")
    if qty is None:
        qty = 1
    unit = line.get("unit", "pack")
    qt = tokens(line["query"])
    pick = line.get("pick") or pick
    prefer = line.get("prefer")
    base = {
        "line_id": lid,
        "requested": line.get("label") or line["query"],
        "query": line["query"],
        "quantity": qty,
        "unit": unit,
    }

    items: list[dict] = []
    offset, limit = 0, 36
    has_more = False
    for _ in range(MAX_SEARCH_PAGES):
        remaining_time()
        page = search(ctx["storeCode"], line["query"], offset, limit)
        items += page.get("items", [])
        pag = page.get("pagination", {})
        has_more = bool(pag.get("hasMore"))
        if pick != "cheapest" and prefer != "max_fat" and any(
                delivery_available(i) and is_perfect(qt, i["name"]) for i in items):
            break
        if not has_more:
            break
        offset = pag.get("nextOffset", offset + limit)

    base["search_complete"] = not has_more
    # Не показываем недоступные товары и самовывоз в сценарии доставки.
    matched_unavailable = any(is_perfect(qt, i["name"]) for i in items)
    items = list({str(i["productId"]): i for i in items if delivery_available(i)}.values())
    perfect = [i for i in items if is_perfect(qt, i["name"])]
    near = sorted(items, key=lambda i: match(qt, i["name"])[0], reverse=True)[:5]

    if not perfect:
        if near and match(qt, near[0]["name"])[0] >= 0.5:
            return _ambiguous_payload(line, base, near, qty, unit,
                                      "точных совпадений нет, есть частичные")
        if matched_unavailable:
            return {**base, "status": "out_of_stock", "reason": "нет доступных товаров для доставки"}
        return {**base, "status": "not_found", "reason": f"по запросу «{line['query']}» ничего не найдено"}

    # Признак, который нельзя проверить по каталогу (цвет банки/этикетки, коробка),
    # не даёт права выбирать товар за пользователя.
    if line.get("unverified"):
        return _ambiguous_payload(
            line,
            base,
            perfect or near,
            qty,
            unit,
            "по каталогу нельзя проверить: " + ", ".join(line["unverified"]),
        )

    chosen: dict | None = None
    if len(perfect) == 1:
        chosen = perfect[0]
    elif pick in ("first", "cheapest") or prefer == "max_fat":
        pass  # решается ниже, когда известны фасовки
    else:
        return _ambiguous_payload(
            line,
            base,
            perfect,
            qty,
            unit,
            f"подходящих вариантов: {len(perfect)} — нужен выбор",
        )

    def plan(item: dict) -> tuple[int, int, str] | None:
        """(упаковок, излишек в единицах, пояснение) или None если единицы не сходятся."""
        ps = pack_size(item["name"])
        if unit == "piece":
            if not ps or ps[1] != "шт":
                return None
            per = int(ps[0])
            if per <= 0:
                return None
            packs = math.ceil(qty / per)
            return packs, packs * per - qty, f"{per} шт/упак"
        return qty, 0, "упаковок"

    if chosen is None:
        scored = [(plan(i), i) for i in perfect]
        scored = [(p, i) for p, i in scored if p]
        if not scored:
            return {
                **base,
                "status": "unsupported_quantity_unit",
                "reason": "не удалось определить фасовку в штуках для выбранных товаров",
                "candidates": [_candidate(i) for i in perfect[:5]],
            }
        if prefer == "max_fat":
            # «Самые жирные»: максимум жирности; товары без жирности в названии — в конец.
            chosen = max(
                scored,
                key=lambda pi: (fat_percent(pi[1]["name"]) is not None, fat_percent(pi[1]["name"]) or 0),
            )[1]
        elif pick == "first":
            chosen = scored[0][1]
        else:  # cheapest — по стоимости нужного количества
            chosen = min(scored, key=lambda pi: pi[0][0] * pi[1]["price"])[1]

    p = plan(chosen)
    if not p:
        return {
            **base,
            "status": "unsupported_quantity_unit",
            "reason": "не удалось определить фасовку",
            "selected": _candidate(chosen),
        }
    packs, surplus, pack_note = p
    avail = int(chosen["quantity"] or 0)
    out = {
        **base,
        "status": "available",
        "selected": _candidate(chosen),
        "pack_size": pack_note,
        "packages_required": packs,
        "surplus_units": surplus,
        "line_total_rub": str(_money(chosen["price"] * packs)),
        "assumptions": ["цена взята из каталога, единицы — рубли (цена сайта / 100)"],
    }
    if line.get("label") and line.get("label") != line["query"]:
        out["assumptions"].append(f'из строки «{line["label"]}» поиск: «{line["query"]}»')
    if prefer == "max_fat":
        fat = fat_percent(chosen["name"])
        out["assumptions"].append(
            f"жирность: выбрана максимальная из найденных — {fat}%" if fat is not None
            else "жирность в названиях не указана: выбран первый из подходящих"
        )
    if pick == "first" and len(perfect) > 1:
        out["assumptions"].append(
            f"бренд не указан — взят самый популярный из {len(perfect)} подходящих"
        )
    if surplus:
        out["assumptions"].append(
            f"точное количество не набирается: {packs}×{pack_note} = излишек {surplus}"
        )
    if chosen.get("pickupOnly"):
        out["assumptions"].append("товар только для самовывоза")
    if avail == 0:
        out["status"] = "out_of_stock"
    elif packs > avail:
        out["status"] = "insufficient_stock"
        out["shortage_packages"] = packs - avail
        out["fulfillable_packages"] = avail
    return out


@request_budget
def check(ctx: dict, items: list[dict], pick: str) -> dict:
    if pick not in ("ask", "first", "cheapest"):
        raise ValueError("pick: допустимы ask, first, cheapest")
    items = validate_items(items)
    lines = []
    for line in items:
        remaining_time()
        lines.append(resolve_line(ctx, line, pick))

    # И выбранные, и предложенные варианты расходуют один общий остаток SKU.
    demand: dict[str, int] = {}
    stock: dict[str, int] = {}
    for ln in lines:
        sel = ln.get("selected") or ln.get("suggested")
        packs = ln.get("packages_required") or ln.get("suggested_packages")
        if sel and packs:
            pid = str(sel["productId"])
            demand[pid] = demand.get(pid, 0) + packs
            stock[pid] = min(stock.get(pid, int(sel["reported_quantity"])), int(sel["reported_quantity"]))
    for ln in lines:
        sel = ln.get("selected") or ln.get("suggested")
        if not sel or ln["status"] not in ("available", "ambiguous", "insufficient_stock"):
            continue
        pid = str(sel["productId"])
        if demand.get(pid, 0) > stock.get(pid, 0):
            ln["status"] = "insufficient_stock"
            ln["shortage_packages"] = demand[pid] - stock[pid]

    ok = [ln for ln in lines if ln["status"] == "available"]
    subtotal = sum(Decimal(ln["line_total_rub"]) for ln in ok)

    # Итог учитывает самый релевантный вариант там, где их несколько.
    priced: list[Decimal] = []
    counted_all = True
    for ln in lines:
        if ln.get("line_total_rub") and ln["status"] in ("available", "insufficient_stock"):
            priced.append(Decimal(ln["line_total_rub"]))
        elif ln["status"] in ("ambiguous", "insufficient_stock") and ln.get("suggested_total_rub"):
            priced.append(Decimal(ln["suggested_total_rub"]))
        else:
            counted_all = False
    total = sum(priced, Decimal("0"))
    suggested_lines = [ln["line_id"] for ln in lines if ln.get("suggested_total_rub")]
    shortage_lines = [ln["line_id"] for ln in lines if ln["status"] == "insufficient_stock"]
    # complete — корзина собирается целиком: всё оценено и ничего не в дефиците.
    # Нехватка остатка делает корзину неполной, хотя цена такой строки известна.
    complete = counted_all and bool(lines) and not shortage_lines
    full = total if complete else None

    report = {
        "store": ctx,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "RUB",
        "lines": lines,
        "summary": {
            "complete": complete,
            "total_rub": str(total),
            "priced_all": counted_all,
            "full_basket_total_rub": str(full) if full is not None else None,
            "suggested_lines": suggested_lines,
            "shortage_lines": shortage_lines,
            "exact": complete and not suggested_lines,
            "available_subtotal_rub": str(subtotal),
            "available_lines": len(ok),
            "total_lines": len(lines),
            "problem_lines": [
                {"line_id": ln["line_id"], "status": ln["status"]} for ln in lines if ln["status"] != "available"
            ],
        },
        "notes": [
            "только чтение каталога: товары не зарезервированы",
            "quantity — доступное количество по данным сайта, не гарантия остатка на полке",
            "доставка и условные акции («от 2 шт», Премиум, Магнит Пэй) не учтены",
            "итоговая сумма должна быть сверена с корзиной на сайте",
        ],
    }
    report["markdown"] = render_table(report)
    return report


# Позиции, которые не попадают в таблицу: товара нет или ответ не получен.
HIDDEN_STATUSES = {
    "out_of_stock",
    "not_found",
    "check_failed",
    "search_incomplete",
    "needs_clarification",
    "unsupported_quantity_unit",
}


def _rub(value: str | Decimal) -> str:
    """66.99 → «66,99»: в русской таблице десятичная запятая."""
    return str(value).replace(".", ",")


def _cell(text: str) -> str:
    return text.replace("|", "/").strip()


def render_table(report: dict) -> str:
    """Таблица «Позиция | Варианты и цены».

    Правила: каждый товар — ссылка на страницу в Магните; отсутствующие позиции в
    таблицу не попадают; наличие не выводится; неоднозначные строки показывают варианты.
    """
    rows: list[str] = []
    for ln in report.get("lines", []):
        if ln.get("status") in HIDDEN_STATUSES:
            continue
        position = ln.get("requested") or ln.get("query") or ln.get("line_id", "")
        if ln.get("candidates"):
            chosen_id = (ln.get("suggested") or {}).get("productId")
            parts = []
            for c in ln.get("candidates", [])[:5]:
                text = f"[{c['name']}]({c['url']}) — {_rub(c['price_rub'])} ₽"
                if chosen_id and c.get("productId") == chosen_id:
                    text = f"**{text}**"
                parts.append(text)
            cell = "; ".join(parts) if parts else "—"
        else:
            sel = ln.get("selected") or {}
            cell = f"[{sel.get('name', '')}]({sel.get('url', '')}) — {_rub(ln.get('line_total_rub', '0'))} ₽"
            packs = ln.get("packages_required") or 1
            if packs > 1:
                cell += f" ({packs} уп.)"
        rows.append(f"| {_cell(position)} | {_cell(cell)} |")

    table = "| Позиция | Варианты и цены |\n|---|---|"
    if rows:
        table += "\n" + "\n".join(rows)
    return table


