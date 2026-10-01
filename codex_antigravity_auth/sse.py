"""Bounded byte decoding and event framing, independent of provider schemas."""

from __future__ import annotations

import codecs
import json
import re
from typing import AsyncIterator, Iterator


MAX_SSE_BUFFER_CHARS = 8 * 1024 * 1024
MAX_SSE_DATA_LINES = 10_000
_LINE_END = re.compile(r"\r\n|\r|\n")


class SSELineError(RuntimeError):
    """Malformed, incomplete, or oversized SSE framing."""


def _complete_legacy_payload(value: str) -> bool:
    """Compatibility interpretation; standard SSE never parses JSON to frame."""
    if value == "[DONE]":
        return True
    try:
        json.loads(value)
    except (ValueError, RecursionError):
        return False
    return True


class SSEDecoder:
    def __init__(self, *, legacy_json_lines: bool = False,
                 max_buffer_chars: int = MAX_SSE_BUFFER_CHARS) -> None:
        if max_buffer_chars <= 0:
            raise ValueError("SSE buffer limit must be positive")
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self._legacy = legacy_json_lines
        self._limit = max_buffer_chars
        self._line: list[str] = []
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
            raise SSELineError("The provider stream exceeded the SSE buffer limit.")

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
        if self._legacy and value == "[DONE]" and self._data:
            yield from self._flush()
        self._check_limit(len(value) + 1)
        if len(self._data) >= MAX_SSE_DATA_LINES:
            raise SSELineError("The provider stream exceeded the SSE data-line limit.")
        self._data.append(value)
        self._data_chars += len(value) + 1
        if self._legacy and _complete_legacy_payload("\n".join(self._data)):
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
            self._line.append(fragment)
            line = "".join(self._line)
            self._line = []
            self._line_chars = 0
            self._skip_lf = match.group() == "\r" and match.end() == len(text)
            yield from self._consume_line(line)
            offset = match.end()
        fragment = text[offset:]
        self._check_limit(len(fragment))
        if fragment:
            self._line.append(fragment)
            self._line_chars += len(fragment)

    def feed(self, chunk: bytes) -> Iterator[str]:
        if self._closed:
            raise SSELineError("The provider stream decoder is already closed.")
        if not isinstance(chunk, bytes):
            raise SSELineError("The provider returned a non-byte stream chunk.")
        try:
            # Bound transient decoded allocations even if a transport gives us
            # a single very large chunk containing many small events.
            for offset in range(0, len(chunk), 65536):
                yield from self._feed_text(self._decoder.decode(chunk[offset:offset + 65536]))
        except SSELineError:
            self._closed = True
            self._line, self._data = [], []
            self._line_chars = self._data_chars = 0
            raise

    def finish(self) -> Iterator[str]:
        if self._closed:
            return
        self._closed = True
        try:
            yield from self._feed_text(self._decoder.decode(b"", final=True))
            trailing = "".join(self._line)
            if self._data or trailing.partition(":")[0] == "data":
                raise SSELineError("The provider stream ended with an incomplete SSE frame.")
        finally:
            self._line, self._data = [], []
            self._line_chars = self._data_chars = 0


async def iter_sse_data(response, *, label: str = "provider", legacy_json_lines: bool = False) -> AsyncIterator[str]:
    decoder = SSEDecoder(legacy_json_lines=legacy_json_lines)
    try:
        async for chunk in response.aiter_bytes():
            for data in decoder.feed(chunk):
                yield data
        for data in decoder.finish():
            yield data
    except SSELineError as exc:
        raise SSELineError(f"The {label} stream has invalid SSE framing: {exc}") from exc
