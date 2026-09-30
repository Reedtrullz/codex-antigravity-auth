"""Owned, short-lived HTTP listener with an ordered deterministic script."""
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import random
import threading

from _test_isolation import allow_listener, remove_listener


def frames(*events):
    return b"".join(b"data: " + (event.encode() if isinstance(event, str) else json.dumps(event).encode()) + b"\n\n" for event in events)


def split_bytes(data, seed=0):
    rng = random.Random(seed)
    while data:
        size = rng.randint(1, 13)
        yield data[:size]
        data = data[size:]


@contextmanager
def upstream(*responses):
    """Each response is (status, headers, bytes-or-chunks). No external sockets."""
    script = deque(responses)
    requests = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body) if body.startswith(b"{") else body.decode()})
            if not script:
                errors.append("unexpected extra upstream request")
                self.send_error(500)
                return
            status, headers, content = script.popleft()
            chunks = [content] if isinstance(content, bytes) else list(content)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(sum(map(len, chunks))))
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # A cancellation may close the consumer.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
    endpoint = allow_listener(server.socket)
    server.server_address = endpoint
    server.server_activate()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://{endpoint[0]}:{endpoint[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        remove_listener(endpoint)
    assert not errors, errors
    assert not script, "scripted upstream responses were not consumed"
