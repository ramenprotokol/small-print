"""The model call, through the official Anthropic Python SDK, streamed.

The SDK runs unmodified inside Python Workers: Cloudflare's Pyodide build
ships ``httpx2_jsfetch``, which routes the SDK's HTTP client through the
Workers ``fetch`` (streamed response bodies included). The SDK warns that
connection-pool options are ignored there; the Worker filters that warning.

Why streamed: a non-streamed call sends nothing back until the whole reply is
written, so a slow reply and a dead connection look the same, and a client
timeout can fire while the API is still generating (and billing) the reply.
A stream's response headers arrive as soon as the API accepts the request,
so every failure can be sorted by *when* it happened. That decides whether
the daily budget unit is given back:

* ``ModelUnavailable``: the request failed before generation could start.
  The connection could not be made, or the API answered with an HTTP error
  (4xx or 5xx) instead of opening the stream. Not billed; refunded.
* ``ModelIncomplete``: the stream opened (or we gave up waiting for it) and
  then failed: a timeout, a dropped connection, an error event mid-stream.
  The API may already have generated, and billed, part of a reply, so the
  unit stays spent and the page says so.
* ``ModelRefused``: a safety classifier declined (``stop_reason: "refusal"``)
  and no fallback model answered.
* ``ModelOutputError``: a message came back but is not usable JSON (for
  example it hit ``max_tokens``). Billed; no refund.

The SDK's automatic retries are off (``max_retries=0``). The SDK retries
timeouts and dropped connections, and a retried request can be billed a
second time on the same budget unit. So one budget unit is exactly one HTTP
request, and a failed part is retried only when the reader asks for it.
"""

from __future__ import annotations

import json
import time
from typing import Callable

import anthropic
import httpx2

from .verify import ModelOutputError

# Timing. The stream's headers must arrive within CONNECT_TIMEOUT_S (under
# the Workers fetch this covers the wait for the response headers), and the
# stream may never go silent for longer than IDLE_TIMEOUT_S (the API sends
# pings while the model thinks). The whole call is allowed deadline_s(),
# sized so a reply of max_tokens can finish at MIN_TOKENS_PER_S, an assumed
# floor for the output rate (not a measured one).
CONNECT_TIMEOUT_S = 60.0
IDLE_TIMEOUT_S = 60.0
MIN_TOKENS_PER_S = 50


def deadline_s(max_tokens: int) -> float:
    """Longest a streamed call may run, sized for ``max_tokens``."""
    return CONNECT_TIMEOUT_S + max_tokens / MIN_TOKENS_PER_S


def worst_case_s(max_tokens: int) -> float:
    """Longest the Worker can wait on one call. The deadline is checked as
    stream events arrive; if they stop, the idle timeout ends the wait."""
    return deadline_s(max_tokens) + IDLE_TIMEOUT_S


class ModelUnavailable(Exception):
    """Failed before generation started: nothing billed, refund the unit."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "busy" | "network" | "config" | "upstream"


class ModelIncomplete(Exception):
    """Failed after the request may have started generating: possibly
    billed, so the unit stays spent."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "timeout" | "interrupted"


class ModelRefused(Exception):
    pass


class _PastDeadline(Exception):
    pass


class _BytesChunks(httpx2.AsyncByteStream):
    def __init__(self, inner):
        self._inner = inner

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk if isinstance(chunk, bytes) else bytes(chunk)

    async def aclose(self):
        await self._inner.aclose()


class BytesTransport(httpx2.AsyncBaseTransport):
    """Hands the SDK response bodies as ``bytes``. The Workers fetch bridge
    (``httpx2_jsfetch``) yields ``memoryview`` chunks, which a whole-body read
    joins without complaint but the SDK's server-sent-event decoder cannot
    split into lines."""

    def __init__(self, inner: httpx2.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request):
        response = await self._inner.handle_async_request(request)
        response.stream = _BytesChunks(response.stream)
        return response

    async def aclose(self):
        await self._inner.aclose()


def _status_kind(exc: anthropic.APIStatusError) -> str:
    if isinstance(exc, anthropic.RateLimitError) or exc.status_code == 529:
        return "busy"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                        anthropic.NotFoundError, anthropic.BadRequestError)):
        return "config"
    return "upstream"


def _reply_json(message) -> object:
    """The JSON in the reply's text. After a mid-stream refusal fallback the
    text is split around a ``fallback`` block: the declined partial, then the
    fallback model's text. Try the joined text first (the fallback continues
    the partial), then the last block alone (it starts afresh)."""
    texts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    if not texts:
        raise ModelOutputError("no text block in the reply")
    for candidate in dict.fromkeys(("".join(texts), texts[-1])):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ModelOutputError("the reply was not valid JSON")


class AnthropicReader:
    """Streams one annotation request and returns the parsed clause JSON."""

    def __init__(self, api_key: str, *, base_url: str | None = None, transport=None,
                 clock: Callable[[], float] = time.monotonic):
        # ``transport`` is the HTTP layer under the SDK: httpx2's default
        # (the Workers fetch bridge under Pyodide), or a mock in tests.
        http_client = anthropic.DefaultAsyncHttpxClient(
            transport=BytesTransport(transport or httpx2.AsyncHTTPTransport()),
        )
        # The key goes straight into the SDK client and nowhere else: never
        # logged, never echoed in a response.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or None,
            max_retries=0,
            timeout=anthropic.Timeout(IDLE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            http_client=http_client,
        )
        self._clock = clock

    async def read(self, params: dict) -> dict:
        params = dict(params)
        messages = self._client.beta.messages if "betas" in params else self._client.messages
        deadline = self._clock() + deadline_s(params["max_tokens"])
        opened = False
        try:
            async with messages.stream(**params) as stream:
                opened = True  # headers arrived: generation may be under way
                async for _event in stream:
                    if self._clock() > deadline:
                        raise _PastDeadline()
                message = await stream.get_final_message()
        except _PastDeadline:
            raise ModelIncomplete("timeout") from None
        except (anthropic.APITimeoutError, httpx2.TimeoutException):
            raise ModelIncomplete("timeout") from None
        except anthropic.APIStatusError as exc:
            if opened:  # an error event inside an open stream
                raise ModelIncomplete("interrupted") from None
            raise ModelUnavailable(_status_kind(exc)) from None
        except anthropic.APIConnectionError as exc:
            if not opened and isinstance(exc.__cause__, httpx2.ConnectError):
                raise ModelUnavailable("network") from None
            raise ModelIncomplete("interrupted") from None
        except httpx2.TransportError:  # the stream body broke off mid-read
            raise ModelIncomplete("interrupted") from None

        if message.stop_reason == "refusal":
            raise ModelRefused()
        if message.stop_reason == "max_tokens":
            raise ModelOutputError("the reply was cut off at max_tokens")
        return {"data": _reply_json(message), "model": message.model}
