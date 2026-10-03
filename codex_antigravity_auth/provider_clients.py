"""Lifespan-owned generation clients; credentials and timeouts stay on requests."""
from __future__ import annotations

import asyncio
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from .request_budget import shielded_cleanup


class _RejectCookies(DefaultCookiePolicy):
    # A provider response must not create implicit credentials for another
    # account on the same origin (or grow a shared jar indefinitely).
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class ProviderClientLease:
    """Request-local settings over a borrowed client, never owning its pool."""
    def __init__(self, owner, client, timeout):
        self._owner = owner
        self._client = client
        self._timeout = timeout
        self._closed = False

    def _check(self):
        self._owner.check_open()
        if self._closed:
            raise RuntimeError('Provider client lease is closed')

    async def __aenter__(self):
        self._check()
        return self

    async def __aexit__(self, *_args):
        await self.aclose()

    async def aclose(self):
        # Per-request response contexts retain their existing ownership and
        # close the response. Only lifespan may close the shared client.
        self._closed = True

    def stream(self, method, url, **kwargs):
        self._check()
        kwargs.setdefault('timeout', self._timeout)
        return self._client.stream(method, url, **kwargs)

    async def post(self, url, **kwargs):
        self._check()
        kwargs.setdefault('timeout', self._timeout)
        return await self._client.post(url, **kwargs)


class ProviderClientPool:
    """Six bounded lane/policy clients, created lazily on one event loop."""
    LANES = frozenset({'google', 'native', 'byok'})

    def __init__(self, *, max_connections, client_factory=None):
        self._loop = asyncio.get_running_loop()
        self._factory = httpx.AsyncClient if client_factory is None else client_factory
        self._limits = httpx.Limits(max_connections=max_connections,
                                    max_keepalive_connections=min(8, max_connections),
                                    keepalive_expiry=30.0)
        self._clients = {}
        self._closed = False

    def check_open(self):
        if self._closed:
            raise RuntimeError('Provider client pool is closed')
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError('Provider client pool belongs to another event loop')

    def borrow(self, lane, *, timeout, trust_env=True):
        self.check_open()
        if lane not in self.LANES:
            raise ValueError('Unknown provider client lane')
        if type(trust_env) is not bool:
            raise ValueError('Provider client trust_env policy must be boolean')
        # No await between lookup and construction: concurrent coroutines on
        # the owned loop cannot construct duplicate clients.
        key = (lane, trust_env)
        if key not in self._clients:
            self._clients[key] = self._factory(
                limits=self._limits, cookies=CookieJar(policy=_RejectCookies()),
                follow_redirects=False, trust_env=trust_env,
            )
        return ProviderClientLease(self, self._clients[key], timeout)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        clients, self._clients = list(self._clients.values()), {}
        await shielded_cleanup(*(client.aclose for client in clients))
