"""Gateway binding of the policy bundled with the standalone Anti client."""
from .skills.anti.scripts.anti_lib import endpoint_policy as shared
from .skills.anti.scripts.anti_lib.local_policy import environment_enabled

NoRedirectHandler = shared.NoRedirectHandler
is_loopback_endpoint = shared.is_loopback_endpoint
validate_endpoint_url = shared.validate_endpoint_url


def _local_only():
    from .request_budget import CURRENT_BUDGET
    return environment_enabled() or bool(getattr(CURRENT_BUDGET.get(), 'local_only', False))


def httpx_client_options(url, *, timeout, loopback_only=False):
    return shared.httpx_client_options(url, timeout=timeout, loopback_only=loopback_only or _local_only())


def open_http_request(request, *, timeout=10.0, before_open=None, loopback_only=False):
    return shared.open_http_request(request, timeout=timeout, before_open=before_open,
                                    loopback_only=loopback_only or _local_only())


__all__ = ['NoRedirectHandler','httpx_client_options','is_loopback_endpoint','open_http_request','validate_endpoint_url']
