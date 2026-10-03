"""The gateway's existing deadline/disconnect race, shared by every route."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from functools import partial
import time

import anyio
from starlette.requests import ClientDisconnect

CURRENT_BUDGET = ContextVar("antigravity_request_budget", default=None)
CLEANUP_SECONDS = 2.0
DRAIN_SECONDS = 0.2
DISCONNECT_POLL_SECONDS = 0.1


class RequestDeadlineExceeded(Exception):
    pass


def consume_task(task):
    if not task.cancelled():
        with suppress(Exception):
            task.result()


async def shielded_cleanup(*callbacks, timeout=None):
    """Attempt each owned close once without letting one stalled close block peers."""
    async def invoke(callback):
        with suppress(Exception):
            await callback()
    with anyio.CancelScope(shield=True):
        tasks = {asyncio.create_task(invoke(callback)) for callback in callbacks}
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=CLEANUP_SECONDS if timeout is None else timeout)
        finally:
            # A second/native asyncio cancellation must not orphan cleanup
            # tasks outside their own deadline.
            for task in tasks:
                if not task.done():
                    task.cancel()
                    task.add_done_callback(consume_task)
                else:
                    consume_task(task)


class ContextOwner:
    """Close a context only after entry finishes, including a late entry result."""
    def __init__(self, context):
        self.context = context
        self.entered = False
        self.requested = False
        self.closed = False

    async def close(self):
        self.requested = True
        if self.entered and not self.closed:
            self.closed = True
            await self.context.__aexit__(None, None, None)


@asynccontextmanager
async def owned_context(context):
    owner = ContextOwner(context)
    budget = CURRENT_BUDGET.get()
    if budget is not None:
        budget.register_close(owner.close)
    try:
        value = await context.__aenter__()
        owner.entered = True
        if owner.requested:
            raise RequestDeadlineExceeded()
        yield value
    finally:
        await shielded_cleanup(owner.close)


class RequestBudget:
    def __init__(self, request, *, timeout, release_account, started=None):
        self.request = request
        self.started = time.monotonic() if started is None else started
        self.deadline = self.started + timeout
        self.body_read = False
        self.release_account = release_account
        self.accounts = []
        self.closers = []
        self.finalizers = []
        self.closed = False
        self.abort = None
        self.abort_reported = False
        self.terminal_observed = False
        self.failure_code = None
        self.cancelled = False
        self.context = {}
        self.stream_idle = 60.0
        self.stream_total = 1800.0

    @contextmanager
    def active(self):
        token = CURRENT_BUDGET.set(self)
        try:
            yield
        finally:
            CURRENT_BUDGET.reset(token)

    def register_close(self, closer):
        if self.closed:
            task = asyncio.create_task(shielded_cleanup(closer))
            task.add_done_callback(consume_task)
        else:
            self.closers.append(closer)

    def register_finalizer(self, callback):
        if self.closed:
            callback()
        else:
            self.finalizers.append(callback)

    def run_finalizers(self):
        callbacks, self.finalizers = self.finalizers, []
        for callback in callbacks:
            callback()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        accounts, self.accounts = self.accounts, []
        callbacks = self.closers + [partial(self.release_account, email) for email in accounts]
        self.closers = []
        try:
            await shielded_cleanup(*callbacks)
        finally:
            self.run_finalizers()

    async def release(self, email):
        if email in self.accounts:
            self.accounts.remove(email)
            await shielded_cleanup(partial(self.release_account, email))

    async def acquire(self, factory):
        async def release_late(value):
            if isinstance(value, dict):
                await shielded_cleanup(partial(self.release_account, value.get("email")))
        value = await self.run(factory, late_result=release_late)
        if isinstance(value, dict):
            self.accounts.append(value.get("email"))
        return value

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            if not self.terminal_observed:
                self.failure_code = "request_deadline_exceeded"
            raise RequestDeadlineExceeded()

    def start_stream(self):
        # Preparation has its own cap and may already be the shorter total
        # stream cap. Never extend an expired preparation deadline at handoff.
        self.check_deadline()
        self.deadline = self.started + self.stream_total
        self.check_deadline()

    async def disconnected(self):
        if not self.body_read:
            return False
        return await self.request.is_disconnected()

    async def run(self, factory, *, late_result=None, deadline=None, watch_disconnect=True):
        deadline = self.deadline if deadline is None else deadline
        if time.monotonic() >= deadline:
            if not self.terminal_observed:
                self.failure_code = "request_deadline_exceeded"
            raise RequestDeadlineExceeded()
        if watch_disconnect and self.body_read:
            if await self.run(self.disconnected, deadline=deadline, watch_disconnect=False):
                self.cancelled = not self.terminal_observed
                raise ClientDisconnect()
        with self.active():
            operation = asyncio.create_task(factory())
        stop = asyncio.Event()
        async def watch():
            while not stop.is_set():
                if await self.disconnected():
                    return True
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=DISCONNECT_POLL_SECONDS)
            return False
        watcher = asyncio.create_task(watch()) if watch_disconnect and self.body_read else None
        timer = asyncio.create_task(asyncio.sleep(max(0, deadline - time.monotonic())))
        abandoned = False

        async def abandon():
            nonlocal abandoned
            if abandoned:
                return
            abandoned = True
            if late_result is not None:
                def later(task):
                    if not task.cancelled():
                        try:
                            value = task.result()
                        except Exception:
                            return
                        cleanup = asyncio.create_task(late_result(value))
                        cleanup.add_done_callback(consume_task)
                operation.add_done_callback(later)
                return
            operation.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.wait({operation}, timeout=DRAIN_SECONDS)
            operation.add_done_callback(consume_task)

        try:
            watched = {operation, timer} | ({watcher} if watcher else set())
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                # Propagate a failed disconnect observer instead of treating it
                # as successful completion of the provider operation.
                await watcher
                await abandon()
                self.cancelled = not self.terminal_observed
                raise ClientDisconnect()
            if timer in done or time.monotonic() >= deadline:
                await abandon()
                if not self.terminal_observed:
                    self.failure_code = "request_deadline_exceeded"
                raise RequestDeadlineExceeded()
            return await operation
        except BaseException:
            await abandon()
            raise
        finally:
            stop.set()
            for task in (timer, watcher):
                if task is not None:
                    task.cancel()
                    task.add_done_callback(consume_task)
            if not operation.done() and not abandoned:
                await abandon()

    async def sync(self, function, *args, **kwargs):
        return await self.run(lambda: anyio.to_thread.run_sync(partial(function, *args, **kwargs), abandon_on_cancel=True))


async def stream_with_budget(source, budget):
    """Bound event silence and elapsed stream time without replaying visible output."""
    import json
    import uuid
    terminal_seen = False
    done_seen = False
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    iterator = source.__aiter__()
    try:
        while True:
            idle_deadline = time.monotonic() + budget.stream_idle
            deadline = min(budget.deadline, idle_deadline)
            try:
                chunk = await budget.run(lambda: anext(iterator), deadline=deadline, watch_disconnect=False)
            except StopAsyncIteration:
                return
            except RequestDeadlineExceeded:
                if not terminal_seen:
                    code = "stream_idle_timeout" if idle_deadline < budget.deadline else "request_deadline_exceeded"
                    budget.failure_code = code
                    event = {"type": "response.failed", "response": {
                        "id": response_id, "object": "response", "status": "failed",
                        "model": budget.context.get("model", ""), "output": [],
                        "error": {"code": code, "message": "The gateway stream exceeded its time budget."}}}
                    budget.terminal_observed = True
                    yield "data: " + json.dumps(event) + "\n\n"
                if not done_seen:
                    yield "data: [DONE]\n\n"
                return
            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line == "data: [DONE]":
                    done_seen = True
                elif line.startswith("data: "):
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        if isinstance(event.get("response_id"), str):
                            response_id = event["response_id"]
                        payload = event.get("response")
                        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                            response_id = payload["id"]
                        if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                            terminal_seen = True
                            budget.terminal_observed = True
            yield chunk
    finally:
        await shielded_cleanup(iterator.aclose)


async def call_sync(function, *args, **kwargs):
    budget = CURRENT_BUDGET.get()
    if budget is None:
        return function(*args, **kwargs)
    return await budget.sync(function, *args, **kwargs)


class NativeStreamState:
    """Ownership bridge between early native HTTP open and response iteration."""
    def __init__(self, client, context):
        self.client, self.context = client, context
        self.response = None
        self.owner = ContextOwner(context)
        self.client_closed = False

    def __iter__(self):
        return iter((self.client, self.context, self.response))

    async def _close_client(self):
        if not self.client_closed:
            self.client_closed = True
            await self.client.aclose()

    async def close(self):
        await shielded_cleanup(self.owner.close, self._close_client)
