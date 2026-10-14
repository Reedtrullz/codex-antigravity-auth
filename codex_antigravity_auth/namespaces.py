"""Shared namespace contract; the implementation also ships in standalone Anti."""
from .skills.anti.scripts.anti_lib.namespaces import (
    CLIENT_HOME_ENV,
    STATE_HOME_ENV,
    client_config_path,
    client_home,
    client_skills_path,
    gateway_file,
    gateway_home,
    namespace_diagnostics,
    root_path,
)

__all__ = [
    "CLIENT_HOME_ENV", "STATE_HOME_ENV", "client_config_path", "client_home",
    "client_skills_path", "gateway_file", "gateway_home", "namespace_diagnostics", "root_path",
]
