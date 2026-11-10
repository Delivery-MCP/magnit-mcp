"""CLI поверх ядра: те же функции, что и у MCP-сервера."""

from __future__ import annotations

import argparse
import json
import sys

from . import core


def _human(report: dict) -> str:
    """Компактный ответ: магазин, таблица «позиция | варианты и цены», итог."""
    store = report["store"]
    out = [
        f"Магазин {store['storeCode']} · {store['address']} · {store['distance_m']} м",
        "",
        report.get("markdown") or core.render_table(report),
        "",
    ]
    summary = report["summary"]

    def rub(value: str) -> str:
        return value.replace(".", ",")

    if summary["complete"] and summary["full_basket_total_rub"]:
        out.append(f"Итого: {rub(summary['full_basket_total_rub'])} ₽")
    elif summary.get("priced_all"):
        out.append(f"Итого (оценка): {rub(summary['total_rub'])} ₽")
        if summary.get("shortage_lines"):
            out.append(f"Не хватает товара: {len(summary['shortage_lines'])} поз.")
    else:
        out.append(f"Итого по доступным позициям: {rub(summary['total_rub'])} ₽")
    if any(not ln.get("search_complete", True) for ln in report["lines"]):
        out.append("Поиск неполный: результат выбран среди просмотренных страниц; оптимум не гарантирован.")
    if summary.get("suggested_lines"):
        out.append(
            f"Жирным выделен вариант, учтённый в итоге "
            f"(позиций с выбором: {len(summary['suggested_lines'])})."
        )
    skipped = [ln for ln in report["lines"] if ln["status"] in core.HIDDEN_STATUSES]
    if skipped:
        names = ", ".join(ln.get("requested") or ln["line_id"] for ln in skipped)
        out.append(f"Не добавлено: {names}")
    return "\n".join(out)


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Проверка корзины в Магните (только чтение)")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("stores", "search", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--lat", type=float, help="широта")
        sp.add_argument("--lon", type=float, help="долгота")
        sp.add_argument("--address", help="адрес текстом (геокодируется через Photon/OSM)")
        sp.add_argument("--address-choice", type=int, default=None, help="номер варианта адреса из списка")
        sp.add_argument("--service", default="express")
        sp.add_argument("--human", action="store_true")
        if name == "search":
            sp.add_argument("--term", required=True)
        if name == "check":
            sp.add_argument("--items", help="JSON с позициями")
            sp.add_argument("--list", help="текстовый список: одна позиция в строке")
            sp.add_argument("--pick", choices=["ask", "first", "cheapest"], default="ask")

    a = p.parse_args(argv)

    # Координаты: напрямую или через адрес.
    if a.address:
        try:
            options = core.geocode(a.address)
        except RuntimeError as e:
            print(json.dumps({"error": "geocode_failed", "detail": str(e)}, ensure_ascii=False))
            return 1
        if not options:
            print(
                json.dumps(
                    {"error": "address_not_found", "detail": f"не найден адрес: {a.address}"},
                    ensure_ascii=False,
                )
            )
            return 2
        if len(options) > 1 and a.address_choice is None:
            print(
                json.dumps(
                    {
                        "error": "address_ambiguous",
                        "detail": "несколько вариантов — выберите и повторите с --address-choice N",
                        "candidates": [
                            {"index": i, **o} for i, o in enumerate(options)
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        idx = a.address_choice or 0
        if idx < 0 or idx >= len(options):
            print(json.dumps({"error": "bad_address_choice", "detail": f"нет варианта {idx}"}, ensure_ascii=False))
            return 3
        a.lat, a.lon = options[idx]["lat"], options[idx]["lon"]
    elif a.lat is None or a.lon is None:
        print(
            json.dumps(
                {"error": "no_location", "detail": "укажите --address или --lat и --lon"},
                ensure_ascii=False,
            )
        )
        return 2

    try:
        shops = core.get_shops(a.lat, a.lon)
    except RuntimeError as e:
        print(json.dumps({"error": "check_failed", "detail": str(e)}, ensure_ascii=False))
        return 1

    shop = core.pick_store(shops, a.lat, a.lon, a.service)
    if not shop:
        print(
            json.dumps(
                {"error": "no_delivery_zone", "detail": f"нет магазина {a.service} у точки"},
                ensure_ascii=False,
            )
        )
        return 2
    ctx = core.store_context(shop, a.lat, a.lon, a.service)

    if a.cmd == "stores":
        result = {"store": ctx, "shops": shops}
    elif a.cmd == "search":
        result = {"store": ctx, "page": core.search(ctx["storeCode"], a.term)}
    else:
        if bool(a.items) == bool(a.list):
            print(
                json.dumps(
                    {"error": "no_basket", "detail": "укажите ровно одно: --items JSON или --list текстом"},
                    ensure_ascii=False,
                )
            )
            return 2
        if a.list:
            try:
                with open(a.list, encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                print(
                    json.dumps(
                        {"error": "items_file_not_found", "detail": f"нет файла: {a.list}"},
                        ensure_ascii=False,
                    )
                )
                return 2
            items = core.parse_basket_text(text)
            if not items:
                print(json.dumps({"error": "empty_basket", "detail": "список пуст"}, ensure_ascii=False))
                return 2
            result = core.check(ctx, items, a.pick)
            if a.human:
                print(_human(result))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        try:
            with open(a.items, encoding="utf-8") as f:
                items = json.load(f)["items"]
        except FileNotFoundError:
            print(
                json.dumps(
                    {"error": "items_file_not_found", "detail": f"нет файла: {a.items}"},
                    ensure_ascii=False,
                )
            )
            return 2
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(
                json.dumps(
                    {
                        "error": "bad_items_json",
                        "detail": f'ожидается {{"items": [...]}}, получено: {e}',
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        if not isinstance(items, list) or not items:
            print(json.dumps({"error": "empty_basket", "detail": "список items пуст"}, ensure_ascii=False))
            return 2
        result = core.check(ctx, items, a.pick)

    if a.human and a.cmd == "check":
        print(_human(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"error": "check_failed", "detail": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
