# Offline test contract

Run `python scripts/run_tests.py -q` in the development environment. It isolates
HOME, platform state directories, credential variables, keyring and subprocesses
**before pytest plugins or application imports**. Ordinary `python -m pytest`
also installs the guard at root conftest collection time. Both test trees,
including bundled Anti, use private per-test state. Do not run the packaged
unittest file directly on a contributor's workstation.

Only TCP destinations registered by an owned loopback fixture are reachable;
the developer's normal gateway port is not an exception. DNS, unregistered
listeners, arbitrary executables and access to the original user credential
namespace fail. Python children inherit startup isolation even with an empty
explicit environment. Local Git fixture commands use no user/global config or
hooks. Assertions caught by application error handling still fail test teardown
(or child process exit). Negative guard tests use `expected_denial` explicitly.
This guards against accidental host dependencies, not malicious code deliberately
escaping Python's runtime.

`fake_upstream.upstream` scripts real HTTP responses, captures synthetic request
payloads and asserts complete script consumption. `split_bytes` uses a fixed seed;
parser tests also exercise every single byte split, including UTF-8 code points.
The replay suite covers terminal outcomes, tools, malformed/partial streams,
refresh through encrypted temporary storage, Retry-After rotation with a fixed
account clock, disconnect cleanup and telemetry. Unit tests remain useful for
focused boundaries; real HTTP replays must not replace the HTTP client with mocks.

There is no automatic live-test profile. A live run requires separate explicit
user authorization and a separate process/environment; never disable this guard
to make ordinary tests pass. CI and installed-artifact tests remain offline.
