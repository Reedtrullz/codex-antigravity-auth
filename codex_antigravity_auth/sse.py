"""Bounded byte decoding and event framing, independent of provider schemas."""

from __future__ import annotations

import codecs
import io
import re
from typing import AsyncIterator, Iterator


from .resource_limits import ResourceLimitError, current_limits, json_loads_limited


MAX_SSE_BUFFER_CHARS = 8 * 1024 * 1024
MAX_SSE_DATA_LINES = 10_000
_LINE_END = re.compile(r"\r\n|\r|\n")


class SSELineError(RuntimeError):
    """Malformed, incomplete, or oversized SSE framing."""


class SSELimitError(SSELineError):
    """The provider exceeded a parser or cumulative stream budget."""


def _complete_legacy_payload(value: str, limits=None) -> bool:
    """Compatibility interpretation; standard SSE never parses JSON to frame."""
    if value == "[DONE]":
        return True
    try:
        json_loads_limited(value, limits=limits)
    except ResourceLimitError as exc:
        raise (SSELimitError if exc.status == 413 else SSELineError)(str(exc)) from exc
    except (ValueError, RecursionError):
        return False
    return True


class SSEDecoder:
    def __init__(self, *, legacy_json_lines: bool = False,
                 max_buffer_chars: int | None = None, max_total_bytes: int | None = None) -> None:
        limits = current_limits()
        self._resource_limits = limits
        max_buffer_chars = limits.sse_frame_chars if max_buffer_chars is None else max_buffer_chars
        self._total_limit = limits.sse_total_bytes if max_total_bytes is None else max_total_bytes
        self._total_bytes = 0
        if max_buffer_chars <= 0 or self._total_limit <= 0:
            raise ValueError("SSE buffer limit must be positive")
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self._legacy = legacy_json_lines
        self._limit = max_buffer_chars
        self._line = io.StringIO()
        self._line_chars = 0
        self._data: list[str] = []
        self._data_chars = 0
        self._skip_lf = False
        self._closed = False

    @property
    def buffered_chars(self) -> int:
        return self._line_chars + self._data_chars

    def _check_limit(self, added: int = 0) -> None:
        if self.buffered_chars + added > self._limit:
            raise SSELimitError("The provider stream exceeded the SSE buffer limit.")

    def _flush(self) -> Iterator[str]:
        if self._data:
            payload = "\n".join(self._data)
            self._data = []
            self._data_chars = 0
            yield payload

    def _consume_line(self, line: str) -> Iterator[str]:
        if not line:
            yield from self._flush()
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if field != "data":
            return
        if separator and value.startswith(" "):
            value = value[1:]
        if self._legacy and value.strip() == "[DONE]":
            # Historical JSON-line callers accepted padded sentinels. Do not
            # trim ordinary data or standard SSE field values.
            value = "[DONE]"
            if self._data:
                yield from self._flush()
        self._check_limit(len(value) + 1)
        if len(self._data) >= MAX_SSE_DATA_LINES:
            raise SSELimitError("The provider stream exceeded the SSE data-line limit.")
        self._data.append(value)
        self._data_chars += len(value) + 1
        if self._legacy and _complete_legacy_payload("\n".join(self._data), self._resource_limits):
            yield from self._flush()

    def _feed_text(self, text: str) -> Iterator[str]:
        if not text:
            return
        if self._skip_lf:
            self._skip_lf = False
            if text.startswith("\n"):
                text = text[1:]
        offset = 0
        for match in _LINE_END.finditer(text):
            fragment = text[offset:match.start()]
            self._check_limit(len(fragment))
            self._line.write(fragment)
            line = self._line.getvalue()
            self._line.close()
            self._line = io.StringIO()
            self._line_chars = 0
            self._skip_lf = match.group() == "\r" and match.end() == len(text)
            yield from self._consume_line(line)
            offset = match.end()
        fragment = text[offset:]
        self._check_limit(len(fragment))
        if fragment:
            self._line.write(fragment)
            self._line_chars += len(fragment)

    def _feed_bounded(self, text: str) -> Iterator[str]:
        encoded = text.encode("utf-8")
        remaining = self._total_limit - self._total_bytes
        if len(encoded) > remaining:
            prefix = encoded[:remaining].decode("utf-8", errors="ignore")
            self._total_bytes += len(prefix.encode("utf-8"))
            yield from self._feed_text(prefix)
            raise SSELimitError("The provider stream exceeded the cumulative SSE limit.")
        self._total_bytes += len(encoded)
        yield from self._feed_text(text)

    def feed(self, chunk: bytes) -> Iterator[str]:
        if self._closed:
            raise SSELineError("The provider stream decoder is already closed.")
        if not isinstance(chunk, bytes):
            raise SSELineError("The provider returned a non-byte stream chunk.")
        try:
            # Bound transient decoded allocations even if a transport gives us
            # a single very large chunk containing many small events.
            for offset in range(0, len(chunk), 65536):
                yield from self._feed_bounded(self._decoder.decode(chunk[offset:offset + 65536]))
        except SSELineError:
            self._closed = True
            self._line.close()
            self._line, self._data = io.StringIO(), []
            self._line_chars = self._data_chars = 0
            raise

    def finish(self) -> Iterator[str]:
        if self._closed:
            return
        self._closed = True
        try:
            yield from self._feed_bounded(self._decoder.decode(b"", final=True))
            trailing = self._line.getvalue()
            if self._data or trailing.partition(":")[0] == "data":
                raise SSELineError("The provider stream ended with an incomplete SSE frame.")
        finally:
            self._line.close()
            self._line, self._data = io.StringIO(), []
            self._line_chars = self._data_chars = 0


async def iter_sse_data(response, *, label: str = "provider", legacy_json_lines: bool = False) -> AsyncIterator[str]:
    decoder = SSEDecoder(legacy_json_lines=legacy_json_lines)
    try:
        async for chunk in response.aiter_bytes():
            for data in decoder.feed(chunk):
                yield data
        for data in decoder.finish():
            yield data
    except SSELimitError:
        raise
    except SSELineError as exc:
        raise SSELineError(f"The {label} stream has invalid SSE framing: {exc}") from exc
