#!/usr/bin/env python3
"""Проверка MCP-сервера по HTTP без клиента.

Handshake → tools/list → реальный вызов check_basket.

  python3 scripts/smoke.py http://127.0.0.1:8000/mcp
  python3 scripts/smoke.py http://127.0.0.1:8000/mcp --lat 51.603893 --lon 46.014305

Зависимостей нет. Смысл: убедиться, что сервер отвечает по протоколу и что
внутри него работают живые запросы к Магниту.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HDRS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post(url: str, payload: dict, sid: str | None = None) -> tuple[str, str | None, int]:
    headers = dict(HDRS)
    if os.environ.get("MAGNIT_MCP_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["MAGNIT_MCP_TOKEN"]
    if sid:
        headers["mcp-session-id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode(), r.headers.get("mcp-session-id"), r.status
    except urllib.error.HTTPError as e:
        return e.read().decode(), e.headers.get("mcp-session-id"), e.code


def messages(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("url", help="например http://127.0.0.1:8000/mcp")
    p.add_argument("--lat", type=float, default=51.603893)
    p.add_argument("--lon", type=float, default=46.014305)
    a = p.parse_args()

    print(f"1. initialize → {a.url}")
    body, sid, code = post(
        a.url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        },
    )
    msgs = messages(body)
    if code != 200 or not msgs or "error" in msgs[0]:
        print(f"   ОШИБКА HTTP {code}: {body[:300]}")
        print("   421 → задайте MAGNIT_ALLOWED_HOSTS; 401 → проверьте MAGNIT_MCP_TOKEN")
        return 1
    info = msgs[0]["result"]["serverInfo"]
    print(f"   ok: {info['name']} {info['version']}, protocol={msgs[0]['result']['protocolVersion']}")
    if not sid:
        print("   предупреждение: сервер не выдал mcp-session-id (stateless?)")

    print("2. notifications/initialized")
    _, _, code = post(a.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    print(f"   HTTP {code}")

    print("3. tools/list")
    body, _, code = post(a.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    tools = []
    for m in messages(body):
        tools = m.get("result", {}).get("tools", tools)
    if not tools:
        print(f"   ОШИБКА HTTP {code}: {body[:300]}")
        return 1
    for t in tools:
        ann = t.get("annotations") or {}
        print(f"   {t['name']:22} readOnly={ann.get('readOnlyHint')}  {list(t['inputSchema'].get('properties', {}))}")

    print(f"4. tools/call check_basket ({a.lat}, {a.lon})")
    body, _, code = post(
        a.url,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "check_basket",
                "arguments": {
                    "lat": a.lat,
                    "lon": a.lon,
                    "pick": "cheapest",
                    "items": [
                        {"line_id": "eggs", "query": "яйца С1", "quantity": 20, "unit": "piece"},
                        {"line_id": "cookies", "query": "печенье юбилейное шоколад", "quantity": 2, "unit": "pack"},
                    ],
                },
            },
        },
        sid,
    )
    data = None
    for m in messages(body):
        res = m.get("result") or {}
        if m.get("error"):
            print(f"   ОШИБКА: {m['error']}")
            return 1
        for c in res.get("content", []):
            if c.get("type") == "text":
                try:
                    data = json.loads(c["text"])
                except json.JSONDecodeError:
                    pass
    if not data:
        print(f"   ОШИБКА HTTP {code}: {body[:300]}")
        return 1

    s = data.get("store", {})
    print(f"   магазин {s.get('storeCode')} · {s.get('address')} · {s.get('distance_m')} м")
    for ln in data.get("lines", []):
        sel = ln.get("selected") or {}
        total = ln.get("packages_required")
        print(f"   [{ln['status']:18}] {ln['requested'][:24]:26} {(sel.get('name') or '—')[:40]:42} x{total} {ln.get('line_total_rub', '—'):>8} ₽")
    summ = data.get("summary", {})
    print(f"   complete={summ.get('complete')}  Итого={summ.get('full_basket_total_rub')}  Доступно={summ.get('available_subtotal_rub')}")
    print("\nвсё работает: протокол, инструменты и живые запросы к Магниту")
    return 0


if __name__ == "__main__":
    sys.exit(main())
