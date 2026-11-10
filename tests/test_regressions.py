"""Регрессии ревью: корзина, схема, авторизация, конкуренция. Без сети."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from magnit_mcp import core, cli

CTX = {'storeCode': '1', 'address': 'test', 'distance_m': 0}


def product(pid='1', name='Молоко 1л', price=10000, qty=5, pickup=False):
    return dict(productId=pid, name=name, price=price, quantity=qty, pickupOnly=pickup)


def page(items, more=False, offset=36):
    return {'items': items, 'pagination': {'hasMore': more, 'nextOffset': offset}}


class BasketTests(unittest.TestCase):
    def run_basket(self, products, items=None, pick='ask'):
        with patch.object(core, 'search', return_value=page(products)):
            return core.check(CTX, items or [{'query': 'молоко'}], pick)

    def test_zero_stock_and_pickup_hidden(self):
        for products in ([product(qty=0), product('2', qty=0)], [product(pickup=True)]):
            r = self.run_basket(products)
            self.assertFalse(r['summary']['complete'])
            self.assertEqual(r['summary']['total_rub'], '0')
            self.assertNotIn('/product/', r['markdown'])

    def test_first_skips_unavailable(self):
        r = self.run_basket([product(qty=0), product('2')], pick='first')
        self.assertEqual(r['lines'][0]['selected']['productId'], '2')

    def test_shared_suggested_and_mixed_stock(self):
        for items in ([{'query': 'молоко'}, {'query': 'молоко'}],
                      [{'query': 'молоко', 'pick': 'first'}, {'query': 'молоко'}]):
            r = self.run_basket([product(qty=1), product('2')], items)
            self.assertFalse(r['summary']['complete'])
            self.assertEqual([x['status'] for x in r['lines']], ['insufficient_stock'] * 2)
            self.assertEqual(r['summary']['total_rub'], '200.00')

    def test_invalid_inputs_before_network(self):
        invalid = [dict(quantity=x) for x in (-2, 0, 1.5, float('nan'), True)]
        invalid += [dict(unit='kg'), dict(prefer='other'), dict(typo=1), dict(pick='random')]
        with patch.object(core, 'search') as search:
            for fields in invalid:
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    core.check(CTX, [{'query': 'молоко', **fields}], 'ask')
            for items in ([], [{'query': 'молоко'}] * 51):
                with self.assertRaises(ValueError):
                    core.check(CTX, items, 'ask')
            search.assert_not_called()

    def test_text_quantity(self):
        items = core.parse_basket_text('Яйца С1 20 штук\nПеченье 2 пачки')
        self.assertEqual((items[0]['query'], items[0]['quantity'], items[0]['unit']), ('Яйца С1', 20, 'piece'))
        self.assertEqual(items[1]['quantity'], 2)
        with patch.object(core, 'search', return_value=page([product(name='Яйцо С1 10шт')])):
            self.assertEqual(core.check(CTX, items[:1], 'first')['lines'][0]['packages_required'], 2)
        for text in ('Яйца -2 штук', 'Яйца 1,5 штук'):
            with self.assertRaises(ValueError):
                core.parse_basket_text(text)

    def test_optimization_pagination(self):
        for field, first, second in (({'pick': 'cheapest'}, product(name='Бекон', price=20000), product('2', name='Бекон', price=5000)),
                                     ({'prefer': 'max_fat'}, product(name='Сливки 10%'), product('2', name='Сливки 33%'))):
            with patch.object(core, 'search', side_effect=[page([first], True), page([second])]) as search:
                r = core.check(CTX, [{'query': 'Бекон' if 'pick' in field else 'Сливки', **field}], 'ask')
                self.assertEqual(r['lines'][0]['selected']['productId'], '2')
                self.assertEqual(search.call_count, 2)
                self.assertTrue(r['lines'][0]['search_complete'])

    def test_search_budget_is_explicit(self):
        with patch.object(core, 'search', return_value=page([product()], True)) as search:
            r = core.check(CTX, [{'query': 'молоко', 'pick': 'cheapest'}], 'ask')
            self.assertEqual(search.call_count, core.MAX_SEARCH_PAGES)
            self.assertFalse(r['lines'][0]['search_complete'])
            self.assertIn('Поиск неполный', cli._human(r))

    def test_partial_match_gets_suggestion_and_human_total(self):
        r = self.run_basket([product(), product('2')], [{'query': 'молоко'}, {'query': 'абракадабра'}])
        self.assertFalse(r['summary']['complete'])
        self.assertIn('100,00 ₽', cli._human(r).split('Итого по доступным позициям: ')[1])
        partial = self.run_basket([product(name='Молоко 1л')], [{'query': 'молоко шоколад'}])
        self.assertEqual(partial['lines'][0]['status'], 'ambiguous')
        self.assertIn('suggested', partial['lines'][0])

    def test_deadline_before_search(self):
        token = core._DEADLINE.set(time.monotonic() - 1)
        try:
            with patch.object(core, 'search') as search, self.assertRaises(TimeoutError):
                core.check(CTX, [{'query': 'молоко'}], 'ask')
            search.assert_not_called()
        finally:
            core._DEADLINE.reset(token)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from magnit_mcp import server
        self.server = server
        # Параллельный таймер моделирует активность других клиентов во время I/O.
        async def ticker():
            while True:
                await asyncio.sleep(.005)
        self.ticker = asyncio.create_task(ticker())

    async def asyncTearDown(self):
        self.ticker.cancel()
        await asyncio.gather(self.ticker, return_exceptions=True)

    async def test_auth_headers_and_fixed_path(self):
        import httpx
        with patch.dict(os.environ, {'MAGNIT_MCP_TOKEN': 'test-secret'}):
            app = self.server.http_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                response = await client.get('/mcp', headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['www-authenticate'], 'Bearer')
            response = await client.get('/test-secret/mcp', headers={'Authorization': 'Bearer test-secret'})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.server.mcp.settings.streamable_http_path, '/mcp')

    async def test_authenticated_mcp_initialize(self):
        import httpx
        with patch.dict(os.environ, {'MAGNIT_MCP_TOKEN': 'test-secret'}):
            app = self.server.http_app()
        async with self.server.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                response = await client.post('/mcp', headers={
                    'Authorization': 'Bearer test-secret',
                    'Accept': 'application/json, text/event-stream',
                }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'regression', 'version': '1'},
                }})
                self.assertEqual(response.status_code, 200)
                self.assertIn('magnit-catalog', response.text)

    async def test_public_requires_token_and_access_log_disabled(self):
        with patch.dict(os.environ, {'MAGNIT_MCP_HOST': '0.0.0.0', 'MAGNIT_MCP_TOKEN': ''}):
            with patch('uvicorn.run') as run:
                self.assertEqual(self.server.main(['--transport', 'http']), 2)
                run.assert_not_called()
        with patch.dict(os.environ, {'MAGNIT_MCP_TOKEN': 'test-secret'}):
            with patch('uvicorn.run') as run:
                self.assertEqual(self.server.main(['--transport', 'http']), 0)
                self.assertFalse(run.call_args.kwargs['access_log'])

    async def test_mcp_schema_and_validation(self):
        tools = await self.server.mcp.list_tools()
        schema = next(t.inputSchema for t in tools if t.name == 'check_basket')
        item = schema['$defs']['BasketItem']
        self.assertFalse(item['additionalProperties'])
        self.assertIn('prefer', item['properties'])
        self.assertIn('pick', item['properties'])
        for extra in ({'typo': 1}, {'quantity': -2}, {'quantity': 1.5}, {'quantity': True}, {'unit': 'kg'}):
            with patch.object(core, 'get_shops') as network:
                # FastMCP exposes argument validation through its registered Tool.
                tool = self.server.mcp._tool_manager.get_tool('check_basket')
                with self.assertRaises(Exception):
                    await tool.run({'lat': 0, 'lon': 0, 'items': [{'query': 'молоко', **extra}]})
                network.assert_not_called()

    async def test_slow_search_does_not_block_loop(self):
        def slow(*args):
            time.sleep(.2)
            return page([product()])
        with patch.object(self.server, '_store', return_value=CTX), patch.object(core, 'search', side_effect=slow):
            request = asyncio.create_task(self.server.check_basket(0, 0, [self.server.BasketItem(query='молоко')]))
            started = time.monotonic()
            await asyncio.sleep(.01)
            self.assertLess(time.monotonic() - started, .1)
            self.assertTrue((await request)['summary']['complete'])

    async def test_timeout_preserves_slot_and_concurrency_cap(self):
        with patch.object(core, 'REQUEST_SECONDS', .02):
            with self.assertRaisesRegex(ValueError, 'request_timeout'):
                await self.server._run_blocking(time.sleep, .15)
            self.assertEqual(self.server._SLOTS._value, 3)
        await asyncio.sleep(.2)
        self.assertEqual(self.server._SLOTS._value, 4)
        jobs = [asyncio.create_task(self.server._run_blocking(time.sleep, .1)) for _ in range(4)]
        await asyncio.sleep(.01)
        with self.assertRaisesRegex(ValueError, 'server_busy'):
            await self.server._run_blocking(lambda: None)
        await asyncio.gather(*jobs)

    async def test_exact_hosts_required(self):
        for hosts in ('*', '*.example.com', 'mcp.example.com,*'):
            with patch.dict(os.environ, {'MAGNIT_ALLOWED_HOSTS': hosts}), self.assertRaises(ValueError):
                self.server._security()
        with patch.dict(os.environ, {'MAGNIT_ALLOWED_HOSTS': 'mcp.example.com'}):
            self.assertEqual(self.server._security().allowed_hosts, ['mcp.example.com'])
