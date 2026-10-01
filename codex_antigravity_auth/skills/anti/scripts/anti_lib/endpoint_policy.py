"""Dependency-free endpoint policy shared by packaged and standalone clients."""

from __future__ import annotations

import io
import ipaddress
import urllib.error
import urllib.parse
import urllib.request


def is_loopback_endpoint(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint_url(value: object, *, label: str = "endpoint URL", allow_query: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty absolute http(s) URL")
    value = value.strip()
    if any(ch.isspace() or ord(ch) < 0x20 or 0x7f <= ord(ch) < 0xa0 for ch in value) or "\\" in value:
        raise ValueError(f"{label} must not contain whitespace or control characters or backslashes")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{label} must be an absolute http(s) URL") from exc
    try:
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must include a valid port if a port is specified") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{label} must be an absolute http(s) URL; scheme must be http or https")
    if not parsed.netloc or not hostname:
        raise ValueError(f"{label} must be an absolute http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain username or password")
    if ":" in hostname:
        expected = f"[{hostname}]" + (f":{port}" if port is not None else "")
        if parsed.netloc.lower() != expected.lower():
            raise ValueError(f"{label} must be an absolute http(s) URL")
    if parsed.fragment or (parsed.query and not allow_query):
        raise ValueError(f"{label} must not include query strings or fragments")
    if parsed.scheme == "http" and not is_loopback_endpoint(hostname):
        raise ValueError(f"{label} must use https unless it points at a loopback/local host")
    return value


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay a request/body, even for a same-origin redirect."""

    def http_error_302(self, request, response, code, message, headers):
        response.close()
        # The remote Location/body can contain secrets. Return a fixed error
        # through the callers' existing HTTP-error paths instead of echoing it.
        body = b'{"error":{"code":"redirect_not_allowed","message":"HTTP redirects are disabled; use a non-redirecting endpoint."}}'
        raise urllib.error.HTTPError(
            request.full_url, code, "HTTP redirects are disabled", {}, io.BytesIO(body),
        )

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def open_http_request(request: urllib.request.Request | str, *, timeout: float = 10.0):
    if isinstance(request, str):
        request = urllib.request.Request(validate_endpoint_url(request, allow_query=True))
    url = validate_endpoint_url(request.full_url, allow_query=True)
    for key, value in list(request.headers.items()):
        if key.lower() in {"authorization", "cookie"}:
            request.remove_header(key)
            request.add_unredirected_header(key, value)
    handlers = [NoRedirectHandler()]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http" and is_loopback_endpoint(parsed.hostname):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def httpx_client_options(url: str, *, timeout: float) -> dict:
    """Apply the same redirect and loopback-proxy policy to HTTPX clients."""
    value = validate_endpoint_url(url, allow_query=True)
    parsed = urllib.parse.urlsplit(value)
    return {
        "timeout": timeout,
        "follow_redirects": False,
        # Bypass proxies for plaintext loopback. HTTPS keeps its configured CA
        # environment, including local TLS endpoints with a private CA.
        "trust_env": not (parsed.scheme == "http" and is_loopback_endpoint(parsed.hostname)),
    }
