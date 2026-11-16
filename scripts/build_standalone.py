#!/usr/bin/env python3
"""Собирает автономный скрипт скилла из src/magnit_mcp/{core,cli}.py.

Единственный источник истины — пакет в src/. Этот файл склеивает два модуля в
один standalone-скрипт без зависимостей, который кладётся в скилл:

  python3 scripts/build_standalone.py          # перезаписать
  python3 scripts/build_standalone.py --check  # проверить, что файл не разошёлся
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "scripts" / "magnit_basket.py"

HEADER = '''#!/usr/bin/env python3
"""Проверка продуктовой корзины в Магните по адресу — автономный скрипт скилла.

СГЕНЕРИРОВАН из src/magnit_mcp/core.py + src/magnit_mcp/cli.py.
Не редактировать вручную: правьте пакет и запускайте scripts/build_standalone.py.

Только чтение каталога: ничего не бронируется, не кладётся в корзину и не оплачивается.
Зависимостей нет — Python 3.9+ и стандартная библиотека.

  magnit_basket.py check --address "Саратов, улица Лисина 5А" --items items.json --human
"""
'''


def strip_module_docstring(text: str) -> str:
    """Убирает shebang и первый строковый литерал модуля."""
    if text.startswith("#!"):
        text = text.split("\n", 1)[1]
    text = text.lstrip("\n")
    if text.startswith('"""'):
        end = text.find('"""', 3)
        if end != -1:
            text = text[end + 3 :].lstrip("\n")
    return text


def build() -> str:
    core = strip_module_docstring((ROOT / "src/magnit_mcp/core.py").read_text(encoding="utf-8"))
    cli = strip_module_docstring((ROOT / "src/magnit_mcp/cli.py").read_text(encoding="utf-8"))

    # В склеенном файле future-import допустим только один и только в начале.
    cli = re.sub(r"^from __future__ import annotations\n", "", cli, flags=re.MULTILINE)
    cli = cli.replace("from . import core\n", "")
    cli = re.sub(r"\bcore\.", "", cli)
    cli = cli.strip("\n")

    return f"{HEADER}\n\n{core.rstrip()}\n\n\n{cli}\n"


def main(argv: list[str]) -> int:
    content = build()
    if "--check" in argv:
        existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if existing != content:
            print(f"РАСХОЖДЕНИЕ: {TARGET} не совпадает с пакетом. Запустите build_standalone.py")
            return 1
        print("standalone-скрипт совпадает с пакетом")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content, encoding="utf-8")
    TARGET.chmod(0o755)
    print(f"собрано: {TARGET.relative_to(ROOT)} ({len(content.splitlines())} строк)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
