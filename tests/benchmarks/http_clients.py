"""Opt-in synthetic loopback TLS benchmark: pytest tests/benchmarks/http_clients.py -s.

No real credentials, providers, or config reads. Certificate/key are generated in
pytest's temporary directory. Timing/RSS are not CI pass/fail criteria.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import gc
import ipaddress
import json
import platform
import ssl
import statistics
import time
import tracemalloc

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx

from codex_antigravity_auth.provider_clients import ProviderClientPool
from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter
from codex_antigravity_auth.server import _collect_openai_sse_terminal


@asynccontextmanager
async def tls_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-loopback')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'fixture.crt', tmp_path / 'fixture.key'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    listener_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    listener_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    state = {'connections': 0, 'requests': 0}
    writers, tasks = set(), set()

    async def serve(reader, writer):
        state['connections'] += 1
        writers.add(writer)
        task = asyncio.current_task()
        tasks.add(task)
        try:
            while True:
                header = await reader.readuntil(b'\r\n\r\n')
                fields = dict(line.split(b': ', 1) for line in header.split(b'\r\n')[1:] if b': ' in line)
                await reader.readexactly(int(fields.get(b'Content-Length', b'0')))
                state['requests'] += 1
                body = b'{"fixture":true}'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                             + b'\r\nContent-Type: application/json\r\n\r\n' + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    listener = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=listener_context)
    try:
        yield f'https://127.0.0.1:{listener.sockets[0].getsockname()[1]}/fixture', client_context, state
    finally:
        listener.close()
        await listener.wait_closed()
        for writer in list(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)


async def measure(url, context, state, shared, count=30):
    durations = []
    before = state['connections']
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    pool = ProviderClientPool(max_connections=32, client_factory=lambda **kw: httpx.AsyncClient(
        verify=context, **kw)) if shared else None
    try:
        for _ in range(count):
            begin = time.perf_counter()
            if shared:
                async with pool.borrow('native', timeout=5, trust_env=False) as lease:
                    response = await lease.post(url, json={'fixture': True}, headers={'Authorization': 'Bearer fixture-only'})
            else:
                async with httpx.AsyncClient(verify=context, trust_env=False) as operation:
                    response = await operation.post(url, json={'fixture': True}, headers={'Authorization': 'Bearer fixture-only'})
            assert response.json() == {'fixture': True}
            durations.append((time.perf_counter() - begin) * 1000)
    finally:
        if pool:
            await pool.aclose()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'mode': 'lifespan_pool' if shared else 'per_operation', 'requests': count,
            'connections': state['connections'] - before, 'elapsed_ms': round(elapsed * 1000, 2),
            'median_ms': round(statistics.median(durations), 3),
            'p95_ms': round(sorted(durations)[int(len(durations) * .95)], 3),
            'python_peak_bytes': peak}


def test_connection_reuse_experiment(tmp_path):
    async def run():
        async with tls_server(tmp_path) as (url, context, state):
            # Warm imports/async backend without counting it as a measured run.
            await measure(url, context, state, False, 1)
            rows = []
            for repeat in range(3):
                for shared in ([False, True] if repeat % 2 == 0 else [True, False]):
                    rows.append(await measure(url, context, state, shared))
            print(json.dumps({'python': platform.python_version(), 'httpx': httpx.__version__,
                              'platform': platform.system(), 'tls': ssl.OPENSSL_VERSION, 'results': rows}, indent=2))
            assert all(row['connections'] == (1 if row['mode'] == 'lifespan_pool' else row['requests']) for row in rows)
    asyncio.run(run())


def test_incremental_oauth_collection_memory():
    def frame(event):
        return ('data: ' + json.dumps(event) + '\n\n').encode()
    delta = frame({'type': 'response.output_text.delta', 'item_id': 'msg_fixture',
                   'output_index': 0, 'content_index': 0, 'delta': 'x' * 1024})
    text = 'x' * (1024 * 1024)
    terminal = frame({'type': 'response.completed', 'response': {
        'id': 'resp_fixture', 'status': 'completed', 'output': [{
            'type': 'message', 'id': 'msg_fixture', 'status': 'completed', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}]}})
    chunks = [delta] * 1024 + [terminal[i:i + 16384] for i in range(0, len(terminal), 16384)]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk
    async def one(incremental):
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=Body()))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            if incremental:
                adapter = NativeResponsesStreamAdapter(display_model='fixture')
                async with client.stream('POST', 'https://fixture.invalid/responses') as response:
                    async for chunk in response.aiter_bytes():
                        adapter.consume_bytes(chunk)
                result = adapter.finish()[-1]['response']
            else:
                response = await client.post('https://fixture.invalid/responses')
                result = _collect_openai_sse_terminal(response.content, 'fixture')
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result['status'] == 'completed'
        assert result['output'][0]['content'][0]['text'] == text
        return {'mode': 'incremental' if incremental else 'buffered', 'wire_bytes': sum(map(len, chunks)),
                'terminal_text_bytes': len(text), 'python_peak_bytes': peak, 'elapsed_ms': round(elapsed * 1000, 2)}
    async def run():
        # Warm identical paths before measuring. Source chunks are allocated
        # before tracing; both consumers receive the same bytes and terminal.
        await one(True)
        rows = []
        for repeat in range(3):
            for incremental in ([False, True] if repeat % 2 == 0 else [True, False]):
                rows.append(await one(incremental))
        print(json.dumps({'oauth_collection': rows}, indent=2))
    asyncio.run(run())
