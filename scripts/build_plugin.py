#!/usr/bin/env python3
"""Собирает zip-пакет плагина и проверяет структуру.

  python3 scripts/build_plugin.py

Результат: dist/magnit-basket-plugin-<version>.zip
"""

from __future__ import annotations

import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED = [
    ".codex-plugin/plugin.json",
    "skills/magnit-basket/SKILL.md",
    "scripts/magnit_basket.py",
    "README.md",
]


def fail(msg: str) -> int:
    print(f"ОШИБКА: {msg}")
    return 1


def main() -> int:
    missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
    if missing:
        return fail(f"нет обязательных файлов: {missing}")

    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    for key in ("name", "version", "description", "skills"):
        if not manifest.get(key):
            return fail(f"в plugin.json нет поля {key}")

    skill_md = (ROOT / "skills/magnit-basket/SKILL.md").read_text(encoding="utf-8")
    if not skill_md.startswith("---\n"):
        return fail("SKILL.md должен начинаться с YAML frontmatter")
    if "name: magnit-basket" not in skill_md.split("---")[1]:
        return fail("в frontmatter SKILL.md нет name: magnit-basket")

    version = manifest["version"]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"magnit-basket-plugin-{version}.zip"

    include = [
        ".codex-plugin/plugin.json",
        "agents/openai.yaml",
        "skills/magnit-basket/SKILL.md",
        "scripts/magnit_basket.py",
        "eval.md",
        "README.md",
        "LICENSE",
        "PRIVACY.md",
        "TERMS.md",
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in include:
            path = ROOT / rel
            if path.exists():
                z.write(path, rel)
            else:
                print(f"предупреждение: пропущен {rel}")

    print(f"собрано: {out.relative_to(ROOT)} ({out.stat().st_size // 1024} КиБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
