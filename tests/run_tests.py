#!/usr/bin/env python3
"""Offline-тесты ядра. Без сети: HTTP-поиск подменяется фикстурой.

  python3 tests/run_tests.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from magnit_mcp import core  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def item(name: str, price: int, qty: int, pid: str = "1") -> dict:
    return {
        "productId": pid,
        "name": name,
        "price": price,
        "quantity": qty,
        "pickupOnly": False,
        "productId": pid,
    }



def _human_text(report: dict) -> str:
    """Текстовая форма отчёта: та же логика, что у CLI --human."""
    skipped = [ln for ln in report["lines"] if ln["status"] in core.HIDDEN_STATUSES]
    return ", ".join(ln.get("requested") or ln["line_id"] for ln in skipped)


# ---------------------------------------------------------------- подбор слов
print("подбор слов")
check("«молоко» матчит «Молоко 3,2% 1л»", core.is_perfect(core.tokens("молоко"), "Молоко 3,2% 1л"))
check(
    "«молоко» НЕ матчит «Трубочки для молока»",
    not core.is_perfect(core.tokens("молоко"), "Трубочки Три кота для молока 24г"),
)
check(
    "«яйца С1» матчит «Яйцо куриное C1 10шт» (латинская C)",
    core.is_perfect(core.tokens("яйца С1"), "Куриное яйцо C1 10шт в ассортименте"),
)
check(
    "«яйца С1» НЕ матчит С0",
    not core.is_perfect(core.tokens("яйца С1"), "Яйцо куриное С0 10шт"),
)
check(
    "«печенье юбилейное шоколад» матчит шоколадный вариант",
    core.is_perfect(core.tokens("печенье юбилейное шоколад"), "Печенье с тёмным шоколадом Юбилейное 128г"),
)
check(
    "«макароны спиральки» матчит «Спиралька»",
    core.is_perfect(core.tokens("макароны спиральки"), "Макароны спиралька из твердой пшеницы Царь 450г"),
)

# -------------------------------------------------------------------- фасовка
print("фасовка и деньги")
check("10шт парсится", core.pack_size("Яйцо куриное С1 10шт") == (Decimal(10), "шт"))
check("15шт парсится", core.pack_size("Яйцо Атемарская ферма С1 15шт") == (Decimal(15), "шт"))
check("9900 копеек = 99.00 ₽", core._money(9900) == Decimal("99.00"))
check("без фасовки — None", core.pack_size("Молоко 3,2%") is None)

# ------------------------------------------------------------------ корзина
print("корзина")
CTX = {"storeCode": "640243", "storeType": "express", "catalogType": "2"}


def patch(pages: list[list[dict]]):
    calls = {"n": 0}

    def fake_search(store, term, offset=0, limit=36):
        idx = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        return {
            "items": pages[idx],
            "pagination": {"hasMore": False, "nextOffset": offset + limit},
        }

    core.search = fake_search  # type: ignore[assignment]


# 20 яиц из упаковок по 10, остатка хватает
patch([[item("Яйцо куриное С1 10шт", 9999, 9)]])
r = core.check(CTX, [{"line_id": "e", "query": "яйца С1", "quantity": 20, "unit": "piece"}], "ask")
line = r["lines"][0]
check("20 яиц → 2 упаковки", line.get("packages_required") == 2, str(line.get("packages_required")))
check("позиция available", line["status"] == "available", line["status"])
check("итог 199.98", r["summary"]["full_basket_total_rub"] == "199.98", str(r["summary"]))
check("корзина полная", r["summary"]["complete"] is True)

# остатка не хватает: нужно 10 упаковок, есть 4
patch([[item("Яйцо куриное С1 10шт", 9999, 4)]])
r = core.check(CTX, [{"line_id": "e", "query": "яйца С1", "quantity": 100, "unit": "piece"}], "first")
line = r["lines"][0]
check("нехватка ловится", line["status"] == "insufficient_stock", line["status"])
check("не хватает 6 упаковок", line.get("shortage_packages") == 6, str(line.get("shortage_packages")))
check("итог не выдаётся за полный", r["summary"]["full_basket_total_rub"] is None)
check("неполная корзина помечена", r["summary"]["complete"] is False)

# один SKU в двух строках не должен дважды тратить остаток
patch([[item("Печенье Юбилейное 112г", 4100, 3, pid="777")]])
r = core.check(
    CTX,
    [
        {"line_id": "a", "query": "печенье Юбилейное", "quantity": 2, "unit": "pack"},
        {"line_id": "b", "query": "печенье Юбилейное", "quantity": 2, "unit": "pack"},
    ],
    "first",
)
check("двойной расход остатка ловится", all(ln["status"] == "insufficient_stock" for ln in r["lines"]),
      [ln["status"] for ln in r["lines"]])

# неоднозначность не разыгрывается в режиме ask
patch([[
    item("Молоко 3,2% 1л", 9900, 5, pid="1"),
    item("Молоко 2,5% 1л", 8900, 5, pid="2"),
]])
r = core.check(CTX, [{"line_id": "m", "query": "молоко", "quantity": 1, "unit": "pack"}], "ask")
check("ask возвращает ambiguous", r["lines"][0]["status"] == "ambiguous", r["lines"][0]["status"])
check("есть кандидаты", len(r["lines"][0]["candidates"]) == 2)
check("сумма не выдумана", r["summary"]["available_subtotal_rub"] == "0")

# ничего не найдено
patch([[]])
r = core.check(CTX, [{"line_id": "x", "query": "абракадабра", "quantity": 1, "unit": "pack"}], "ask")
check("not_found при пустой выдаче", r["lines"][0]["status"] == "not_found", r["lines"][0]["status"])

# нулевой остаток
patch([[item("Молоко 3,2% 1л", 9900, 0)]])
r = core.check(CTX, [{"line_id": "m", "query": "молоко", "quantity": 1, "unit": "pack"}], "first")
check("нулевой остаток → out_of_stock", r["lines"][0]["status"] == "out_of_stock", r["lines"][0]["status"])

# без количества → по умолчанию 1 упаковка
patch([[item("Молоко 3,2% 1л", 9900, 5)]])
r = core.check(CTX, [{"line_id": "m", "query": "молоко"}], "ask")
check("без quantity → 1 упаковка по умолчанию", r["lines"][0]["quantity"] == 1)
check("без quantity → позиция считается", r["lines"][0]["status"] == "available", r["lines"][0]["status"])

# --------------------------------------------------------------- список строками
print("список строками")
LIST = """Макароны спиральки
Сливки самые жирные
Бекон самый дешевый
Килька в томате в красной банке
Молоко с зеленой этикеткой
Печенье в коробке
"""
parsed = core.parse_basket_text(LIST)
by_label = {p["label"]: p for p in parsed}
check("6 строк → 6 позиций", len(parsed) == 6, str(len(parsed)))
check("количество по умолчанию 1", all(p["quantity"] == 1 for p in parsed))
check("«самые жирные» → max_fat", by_label["Сливки самые жирные"].get("prefer") == "max_fat")
check("«самый дешевый» → cheapest", by_label["Бекон самый дешевый"]["pick"] == "cheapest")
check(
    "«в красной банке» помечено как непроверяемое",
    by_label["Килька в томате в красной банке"].get("unverified") == ["цвет банки"],
    str(by_label["Килька в томате в красной банке"].get("unverified")),
)
check(
    "модификаторы вырезаны из запроса",
    by_label["Бекон самый дешевый"]["query"] == "Бекон",
    by_label["Бекон самый дешевый"]["query"],
)
check("цвет этикетки распознан", by_label["Молоко с зеленой этикеткой"].get("unverified") == ["цвет этикетки"])
check("коробка распознана", by_label["Печенье в коробке"].get("unverified") == ["тип упаковки (коробка)"])

print("жирность и синонимы")
check("33% из «Сливки 33%»", core.fat_percent("Сливки Село Зеленое 33%") == Decimal(33))
check("без процентов → None, не 0", core.fat_percent("Молоко пастеризованное") is None)
check(
    "«помидоры» матчит «томаты»",
    core.is_perfect(core.tokens("помидоры черри"), "Томаты черри Green Ribbon 680г"),
)
check(
    "синоним не ломает отсев",
    not core.is_perfect(core.tokens("помидоры"), "Соус томатный Heinz 200г"),
)

# «самые жирные» выбирает максимум жирности
patch([[
    item("Сливки 15% 200г", 9000, 5, pid="1"),
    item("Сливки 33% 200г", 20000, 5, pid="2"),
    item("Сливки без жирности", 10000, 5, pid="3"),
]])
r = core.check(CTX, core.parse_basket_text("Сливки самые жирные"), "ask")
ln = r["lines"][0]
check("выбраны сливки 33%", ln["selected"]["name"] == "Сливки 33% 200г", ln["selected"]["name"])
check("позиция available", ln["status"] == "available", ln["status"])

# непроверяемый признак не даёт выбрать товар молча
patch([[item("Килька в томатном соусе 240г", 6199, 9, pid="1")]])
r = core.check(CTX, core.parse_basket_text("Килька в томате в красной банке"), "first")
ln = r["lines"][0]
check("цвет банки → ambiguous даже при одном кандидате", ln["status"] == "ambiguous", ln["status"])
check("причина названа", "цвет банки" in ln.get("reason", ""), ln.get("reason", ""))
check("кандидат показан", len(ln.get("candidates", [])) == 1)

# бренд не указан → берётся популярный, это отражено в допущениях
patch([[
    item("Макароны Шебекинские Спирали 450г", 6699, 5, pid="1"),
    item("Макароны Makfa Спирали 450г", 7500, 5, pid="2"),
]])
r = core.check(CTX, core.parse_basket_text("Макароны спиральки"), "ask")
ln = r["lines"][0]
check("без модификатора берётся первый из подходящих", ln["status"] == "available", ln["status"])
check(
    "допущение про бренд названо",
    any("бренд не указан" in a for a in ln.get("assumptions", [])),
    str(ln.get("assumptions")),
)

# --------------------------------------------------------------- таблица
print("таблица и ссылки")
check(
    "ссылка на товар",
    core.product_url("1000255108") == "https://magnit.ru/product/1000255108",
)
patch([[item("Молоко 3,2% 1л", 9900, 5, pid="777")]])
mixed = core.check(
    CTX,
    [
        {"line_id": "m", "query": "молоко", "quantity": 1},
        {"line_id": "z", "query": "абракадабра", "quantity": 1},
    ],
    "first",
)
md = mixed["markdown"]
check("заголовок таблицы", md.startswith("| Позиция | Варианты и цены |"), md[:40])
check("в таблице есть ссылка", "https://magnit.ru/product/777" in md)
check("цена с запятой", "99,00 ₽" in md)
check("наличие не выводится", "available" not in md and "qty" not in md)
check("отсутствующий товар не попал в таблицу", "абракадабра" not in md, md)
check("«Не добавлено» перечисляет пропуск", "абракадабра" in _human_text(mixed))

patch([[
    item("Килька Вкусные консервы 240г", 11599, 9, pid="11"),
    item("Килька Знак качества 240г", 8400, 9, pid="12"),
]])
amb = core.check(CTX, core.parse_basket_text("Килька в томате в красной банке"), "first")
row = [ln for ln in amb["lines"]][0]
check("неоднозначная строка остаётся в таблице", "Килька в томате" in amb["markdown"])
check("варианты через точку с запятой", "; " in amb["markdown"])
check("у вариантов есть цены", "115,99 ₽" in amb["markdown"] and "84,00 ₽" in amb["markdown"])
check("статус по-прежнему ambiguous в JSON", row["status"] == "ambiguous", row["status"])

# ------------------------------------------------- учёт релевантного варианта
print("итог по релевантному варианту")
patch([[
    item("Молоко 3,2% 1л", 9900, 5, pid="1"),
    item("Молоко 2,5% 1л", 8900, 5, pid="2"),
]])
rel = core.check(CTX, [{"line_id": "m", "query": "молоко", "quantity": 1}], "ask")
check("итог включает релевантный вариант", rel["summary"]["total_rub"] == "99.00", rel["summary"]["total_rub"])
check("exact=False при выборе", rel["summary"]["exact"] is False)
check("строка попала в suggested_lines", rel["summary"]["suggested_lines"] == ["m"], str(rel["summary"]["suggested_lines"]))
check("корзина считается оценённой", rel["summary"]["complete"] is True)
check("в таблице вариант выделен жирным", "**[" in rel["markdown"])
check("available_subtotal не включает выбор", rel["summary"]["available_subtotal_rub"] == "0")

# недоступный первый вариант не должен попадать в итог
patch([[
    item("Молоко 3,2% 1л", 9900, 0, pid="1"),
    item("Молоко 2,5% 1л", 8900, 5, pid="2"),
]])
rel2 = core.check(CTX, [{"line_id": "m", "query": "молоко", "quantity": 1}], "ask")
check(
    "для итога берётся доступный вариант",
    rel2["summary"]["total_rub"] == "89.00",
    rel2["summary"]["total_rub"],
)

# нет товара → полного итога быть не может
patch([[]])
none = core.check(CTX, [{"line_id": "x", "query": "абракадабра", "quantity": 1}], "ask")
check("при отсутствии товара итога нет", none["summary"]["complete"] is False)
check("total_rub при этом нулевой", none["summary"]["total_rub"] == "0", none["summary"]["total_rub"])

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
    sys.exit(1)
print("все тесты прошли")
