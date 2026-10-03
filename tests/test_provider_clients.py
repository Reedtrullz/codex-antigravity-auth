"""Synthetic client-pool isolation and ownership; no providers or user state."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from codex_antigravity_auth import provider_clients as clients, server
from codex_antigravity_auth.request_budget import owned_context
from test_request_deadlines import NATIVE, Request, setup_route, success_response  # noqa: F401


@pytest.mark.parametrize('lane', sorted(clients.ProviderClientPool.LANES))
def test_concurrent_requests_do_not_share_auth_timeout_or_response_cookies(lane):
    async def scenario():
        observed = []
        made = []
        async def handler(request):
            await asyncio.sleep(0)
            observed.append(request)
            return httpx.Response(200, json={'fixture': True}, headers={'set-cookie': 'session=fixture; Path=/'})
        def factory(**kwargs):
            kwargs.setdefault('trust_env', False)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            made.append(client)
            return client
        pool = clients.ProviderClientPool(max_connections=32, client_factory=factory)
        async def one(index):
            async with owned_context(pool.borrow(lane, timeout=index + 1)) as lease:
                response = await lease.post('https://fixture.invalid/v1', headers={
                    'Authorization': f'Bearer fixture-{index}', 'X-Account': str(index)}, json={'i': index})
                assert response.status_code == 200
        await asyncio.gather(*(one(index) for index in range(30)))
        await one(30)  # Run after all Set-Cookie responses have been received.
        assert len(made) == 1 and not made[0].is_closed
        assert not made[0].cookies
        for request in observed:
            index = json.loads(request.content)['i']
            assert request.headers['authorization'] == f'Bearer fixture-{index}'
            assert request.headers['x-account'] == str(index)
            assert 'cookie' not in request.headers
            assert all(value == index + 1 for value in request.extensions['timeout'].values())
        assert 'authorization' not in made[0].headers
        await pool.aclose()
        assert made[0].is_closed
        with pytest.raises(RuntimeError, match='closed'):
            pool.borrow(lane, timeout=1)
    asyncio.run(scenario())


def test_pool_is_bounded_by_lane_and_closes_each_client_once():
    async def scenario():
        made = []
        def factory(**kwargs):
            client = AsyncMock()
            made.append((client, kwargs))
            return client
        pool = clients.ProviderClientPool(max_connections=4, client_factory=factory)
        leases = [pool.borrow(lane, timeout=index + 1, trust_env=trust_env)
                  for index in range(8) for lane in sorted(pool.LANES) for trust_env in (False, True)]
        assert len(made) == 6
        for _client, kwargs in made:
            assert kwargs['limits'].max_connections == 4
            assert kwargs['limits'].max_keepalive_connections == 4
            assert kwargs['follow_redirects'] is False
            assert type(kwargs['trust_env']) is bool
        with pytest.raises(ValueError, match='Unknown'):
            pool.borrow('user-controlled-lane', timeout=1)
        for lease in leases:
            await lease.aclose()
        assert all(not client.aclose.called for client, _ in made)
        await pool.aclose()
        await pool.aclose()
        for client, _ in made:
            client.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_request_cancellation_closes_only_its_response_and_other_requests_survive():
    async def scenario():
        entered = asyncio.Event()
        closed = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Future()
                yield b''
            async def aclose(self):
                closed.append(True)
        async def handler(request):
            if request.url.path == '/slow':
                return httpx.Response(200, stream=Body())
            return httpx.Response(200, json={'fixture': True})
        raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pool = clients.ProviderClientPool(max_connections=2, client_factory=lambda **kw: raw)
        async def slow():
            async with owned_context(pool.borrow('native', timeout=1)) as lease:
                async with owned_context(lease.stream('POST', 'https://fixture.invalid/slow')) as response:
                    async for _chunk in response.aiter_bytes():
                        pass
        pending = asyncio.create_task(slow())
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert closed == [True] and not raw.is_closed
        async with owned_context(pool.borrow('native', timeout=2)) as lease:
            assert (await lease.post('https://fixture.invalid/next')).json() == {'fixture': True}
        await pool.aclose()
        assert raw.is_closed
    asyncio.run(scenario())


def test_pool_rejects_a_different_event_loop():
    async def make():
        return clients.ProviderClientPool(max_connections=1)
    pool = asyncio.run(make())
    async def another():
        with pytest.raises(RuntimeError, match='another event loop'):
            pool.borrow('google', timeout=1)
        await pool.aclose()
    asyncio.run(another())


def test_lifespan_closes_pools_when_refresh_shutdown_raises(monkeypatch):
    original = httpx.AsyncClient
    made = []
    def factory(**kwargs):
        client = original(transport=httpx.MockTransport(lambda req: httpx.Response(200)), **kwargs)
        made.append(client)
        return client
    monkeypatch.setattr(server.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(server._RefreshAheadOwner, 'start', lambda self: None)
    monkeypatch.setattr(server._RefreshAheadOwner, 'close', AsyncMock(side_effect=RuntimeError('fixture close')))
    async def scenario():
        with pytest.raises(RuntimeError, match='fixture close'):
            async with server.gateway_lifespan(server.app):
                async with owned_context(server.provider_client('byok', timeout=1)) as lease:
                    await lease.post('https://fixture.invalid/v1')
                assert not made[0].is_closed
        assert made[0].is_closed
        assert server._provider_client_pool is None and server._refresh_ahead_owner is None
    asyncio.run(scenario())


@pytest.mark.parametrize('route', ['google', 'byok', 'openai', 'openai_oauth'])
@pytest.mark.parametrize('stream', [False, True])
def test_gateway_routes_reuse_clients_and_release_response_ownership(monkeypatch, setup_route, route, stream):
    state = setup_route(route, timeout=5)
    original = httpx.AsyncClient
    made, requests, closed = [], [], []
    async def handler(request):
        requests.append(request)
        if not stream:
            response = success_response(route)
            body = response.content
        elif route == 'google':
            body = b'data: {"candidates":[{"content":{"parts":[{"text":"fixture"}]},"finishReason":"STOP"}]}\n\ndata: [DONE]\n\n'
        elif route == 'byok':
            body = b'data: {"choices":[{"index":0,"delta":{"content":"fixture"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        else:
            body = ('data: ' + json.dumps({'type': 'response.completed', 'response': NATIVE}) + '\n\n').encode()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body
            async def aclose(self):
                closed.append(True)
        return httpx.Response(200, stream=Body())
    def factory(**kwargs):
        kwargs.setdefault('trust_env', False)
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        made.append(client)
        return client
    monkeypatch.setattr(server.httpx, 'AsyncClient', factory)
    async def scenario():
        async with server.gateway_lifespan(server.app):
            for _ in range(2):
                response = await server.create_response(Request(route, stream=stream))
                if stream:
                    wire = ''.join([chunk async for chunk in response.body_iterator])
                    assert 'response.completed' in wire and 'response.failed' not in wire
                else:
                    assert response['status'] == 'completed'
            assert len(made) == 1 and not made[0].is_closed
            assert len(requests) == 2 and len(closed) == 2
        assert made[0].is_closed and server._provider_client_pool is None
    asyncio.run(scenario())
    assert state.release.await_count == (2 if route == 'google' else 0)


@pytest.mark.parametrize('pooled', [False, True], ids=['operation-client', 'lifespan-pool'])
def test_byok_dispatch_preserves_endpoint_client_policy(monkeypatch, setup_route, pooled):
    state = setup_route('byok', timeout=5)
    original = httpx.AsyncClient
    monkeypatch.delenv('ANTIGRAVITY_LOCAL_ONLY', raising=False)
    endpoint = {'base_url': 'https://provider.fixture.invalid/v1'}
    created, requests, policies = [], [], []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            'choices': [{'index': 0, 'message': {'content': 'fixture'}, 'finish_reason': 'stop'}],
        })

    def configs():
        return {'fixture': {'id': 'fixture', 'kind': 'openai_chat', 'baseUrl': endpoint['base_url'],
                            'apiKey': 'synthetic-only', 'models': ['model']}}

    def factory(**kwargs):
        async def observed_handler(request):
            policies.append(kwargs['trust_env'])
            return await handler(request)
        client = original(transport=httpx.MockTransport(observed_handler), **kwargs)
        created.append((client, dict(kwargs)))
        return client

    monkeypatch.setattr(server, 'all_provider_configs', configs)
    monkeypatch.setattr(server.httpx, 'AsyncClient', factory)

    async def dispatch_twice():
        async def request_once():
            response = await server.create_response(Request('byok'))
            assert response['status'] == 'completed'

        if pooled:
            async with server.gateway_lifespan(server.app):
                await request_once()  # Ordinary remote HTTPS keeps HTTPX environment support.
                endpoint['base_url'] = 'http://127.0.0.1:51129/v1'
                await request_once()  # Plaintext loopback bypasses environment proxies.
            assert all(client.is_closed for client, _ in created)
        else:
            monkeypatch.setattr(server, '_provider_client_pool', None)
            await request_once()
            endpoint['base_url'] = 'http://127.0.0.1:51129/v1'
            await request_once()
            assert all(client.is_closed for client, _ in created)

    asyncio.run(dispatch_twice())
    assert len(requests) == len(created) == 2
    assert [request.url.scheme for request in requests] == ['https', 'http']
    assert policies == [True, False]
    assert all(options['follow_redirects'] is False for _, options in created)
    assert all(request.headers['authorization'] == 'Bearer synthetic-only' for request in requests)
    assert state.release.await_count == 0


@pytest.mark.parametrize('pooled', [False, True], ids=['standalone-client', 'local-lifespan-pool'])
def test_local_only_https_dispatch_uses_real_endpoint_policy(monkeypatch, setup_route, pooled):
    state = setup_route('byok', timeout=5)
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY', '1')
    monkeypatch.setattr(server.app.state, 'local_only_mode', None, raising=False)
    endpoint = {'base_url': 'https://127.0.0.1:51129/v1'}
    created, requests = [], []
    original = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            'choices': [{'index': 0, 'message': {'content': 'fixture'}, 'finish_reason': 'stop'}],
        })

    def factory(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        created.append((client, dict(kwargs)))
        return client

    monkeypatch.setattr(server, 'all_provider_configs', lambda: {
        'fixture': {'id': 'fixture', 'kind': 'openai_chat', 'baseUrl': endpoint['base_url'],
                    'apiKey': 'synthetic-only', 'models': ['model']},
    })
    monkeypatch.setattr(server.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(server, '_RefreshAheadOwner', lambda: pytest.fail('local mode must not create OAuth refresh owner'))

    async def dispatch():
        async def request_once():
            response = await server.create_response(Request('byok'))
            assert response['status'] == 'completed'

        if pooled:
            async with server.gateway_lifespan(server.app):
                assert server._refresh_ahead_owner is None
                assert server._provider_client_pool is not None
                assert server.ADMISSION.startup_ceiling == server.ResourceLimits.from_env().inflight
                await request_once()
                assert not created[0][0].is_closed
            assert created[0][0].is_closed
            assert server._provider_client_pool is None
        else:
            monkeypatch.setattr(server, '_provider_client_pool', None)
            await request_once()
            assert created[0][0].is_closed

    asyncio.run(dispatch())
    assert len(created) == len(requests) == 1
    assert requests[0].url.scheme == 'https'
    assert created[0][1]['trust_env'] is False
    assert created[0][1]['follow_redirects'] is False
    assert requests[0].headers['authorization'] == 'Bearer synthetic-only'
    assert state.release.await_count == 0
    if pooled:
        assert server.ADMISSION.startup_ceiling is None


@pytest.mark.parametrize('route', ['google', 'byok', 'openai', 'openai_oauth'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_gateway_stop_keeps_shared_client_usable_and_returns_admission(monkeypatch, setup_route, route, stream, stop):
    state = setup_route(route, timeout=0.08)
    from fastapi import HTTPException
    from codex_antigravity_auth.resource_limits import Admission
    admission = Admission()
    monkeypatch.setattr(server, 'ADMISSION', admission)
    original = httpx.AsyncClient
    made = []
    async def scenario():
        entered = asyncio.Event()
        closed = []
        class StalledBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Future()
                yield b''
            async def aclose(self):
                closed.append(True)
        async def handler(request):
            if request.url.path == '/next':
                return httpx.Response(200, json={'fixture': True})
            return httpx.Response(200, stream=StalledBody())
        def factory(**kwargs):
            kwargs.setdefault('trust_env', False)
            client = original(transport=httpx.MockTransport(handler), **kwargs)
            made.append(client)
            return client
        monkeypatch.setattr(server.httpx, 'AsyncClient', factory)
        async with server.gateway_lifespan(server.app):
            async def operation():
                response = await server.create_response(Request(route, stream=stream))
                if stream:
                    return ''.join([chunk async for chunk in response.body_iterator])
            pending = asyncio.create_task(operation())
            await asyncio.wait_for(entered.wait(), 1)
            if stop == 'cancel':
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif stream:
                wire = await asyncio.wait_for(pending, 1)
                assert 'response.failed' in wire and 'response.completed' not in wire
            else:
                with pytest.raises(HTTPException) as caught:
                    await asyncio.wait_for(pending, 1)
                assert caught.value.status_code == 504
            assert closed == [True]
            assert admission.total == 0 and not admission.routes
            assert len(made) == 1 and not made[0].is_closed
            lane = 'native' if route.startswith('openai') else route
            async with server.provider_client(lane, timeout=1) as lease:
                response = await lease.post('https://fixture.invalid/next')
                assert response.json() == {'fixture': True}
        assert made[0].is_closed
    asyncio.run(scenario())
    assert state.release.await_count == (1 if route == 'google' else 0)
