# Подключение к ChatGPT без публичного хостинга (Secure MCP Tunnel)

ChatGPT не обращается к `localhost` и не запускает локальные процессы. Единственный
способ оставить сервер на своей машине — **Secure MCP Tunnel**: локальный
`tunnel-client` сам открывает исходящее соединение к OpenAI и проксирует запросы в
наш MCP-сервер. Входящие порты и публичный домен не нужны.

Источник: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
[Add custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server).

## Что понадобится

- аккаунт OpenAI Platform и ChatGPT, у которых есть доступ к tunnels и к custom MCP
  серверам (зависит от организации/workspace и ролей — проверить у себя);
- роль **Tunnels Read + Manage** для создания туннеля и **Read + Use** для запуска и
  выбора туннеля в ChatGPT;
- API-ключ Platform с доступом к туннелям (хранить только в переменной окружения);
- этот репозиторий с рабочим `.venv` (сервер уже проверен по stdio).

Серверу не нужны `MAGNIT_MCP_TOKEN` и `MAGNIT_ALLOWED_HOSTS`: он работает по stdio,
наружу ничего не слушает.

## Шаг 1. Создать туннель и скачать клиент

1. Открыть [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
2. Создать туннель, записать `tunnel_id` вида `tunnel_0123456789abcdef...`.
3. Убедиться, что туннель связан с нужным **ChatGPT workspace**, а не только с
   Platform-организацией: иначе он не появится в ChatGPT.
4. Скачать `tunnel-client` по ссылке на этой же странице или из
   [релизов openai/tunnel-client](https://github.com/openai/tunnel-client/releases/latest).

## Шаг 2. Настроить профиль

Команда сервера — абсолютный путь к исполняемому файлу в venv (проверено, что он
работает из любого каталога):

```bash
export CONTROL_PLANE_API_KEY="sk-..."     # не передавать в аргументах и в чат

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile magnit \
  --tunnel-id tunnel_ВАШ_ID \
  --mcp-command "/home/howleysens/www/Cart-Compaire/magnit-mcp/.venv/bin/magnit-mcp"
```

Проверка конфигурации:

```bash
tunnel-client doctor --profile magnit --explain
```

## Шаг 3. Запустить туннель

```bash
tunnel-client run --profile magnit
```

Держать запущенным всё время, пока пользуешься ChatGPT: без клиента вызовы падают.
Локальная панель — `http://127.0.0.1:<порт>/ui`, плюс `/healthz` и `/readyz`.

## Шаг 4. Добавить сервер в ChatGPT

1. `chatgpt.com/plugins` → **Add custom MCP server**.
2. **Connection** → **Tunnel** → выбрать туннель (или вставить `tunnel_id`).
3. **Authentication** → **No authentication** (доступ и так закрыт туннелем).
4. Подтвердить предупреждение о риске → **Create as a plugin**.
5. В карточке плагина проверить, что видны три инструмента:
   `find_delivery_store`, `search_products`, `check_basket`, все read-only.

## Шаг 5. Проверить в чате

Начать новое сообщение, упомянуть плагин через `@`:

```text
@magnit проверь корзину по адресу: Саратов, ул. Лисина 5А.
Молоко, яйца С1 20 штук, печенье Юбилейное с шоколадом 2 пачки, макароны спиральки.
```

Ожидаемо: модель вызывает `check_basket`, получает магазин, цены, наличие и итог.
Сумму считает сервер — если `summary.complete=false`, полного итога быть не должно.

## Ограничения туннеля

| Параметр | Значение по умолчанию |
|---|---|
| Частота | 600 запросов/мин на туннель |
| Дедлайн запроса | 120 секунд (включая ожидание в очереди) |
| Размер одного сообщения | 10 MiB |

Наши вызовы укладываются с запасом: проверка корзины — это несколько запросов к
Магниту по 1–3 секунды.

## Если не получается

| Симптом | Что проверить |
|---|---|
| «Tunnels access required» | нет роли **Read/Manage** в организации; роль появляется до 30 минут |
| Туннель не виден в ChatGPT | туннель привязан к Platform, но не к ChatGPT workspace |
| Вызовы падают после простоя | `tunnel-client run` не запущен; перезапустить и проверить `/readyz` |
| Туннели недоступны в аккаунте | вариант с публичным HTTPS-хостингом (Dockerfile + Caddy в README) или клиент с локальным stdio |

## Альтернатива без Platform

Если туннель недоступен, локальный запуск всё равно можно проверить:

- Claude Desktop, Codex, Cursor — подключают stdio напрямую, конфиг в README;
- MCP Inspector — визуальная проверка инструментов без всякого аккаунта.
