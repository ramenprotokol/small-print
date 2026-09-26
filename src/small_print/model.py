"""The model call, through the official Anthropic Python SDK.

The SDK runs unmodified inside Python Workers: Cloudflare's Pyodide build
ships ``httpx2_jsfetch``, which routes the SDK's HTTP client through the
Workers ``fetch``. (The SDK warns that connection-pool options are ignored
there; the Worker filters that one warning.)

Errors are sorted by whether the call was billed, because that decides
whether the daily budget unit is given back:

* ``ModelUnavailable``: the request failed before producing a message
  (network, rate limit, auth, bad request, server error). Not billed; refund.
* ``ModelRefused``: a safety classifier declined (``stop_reason: "refusal"``).
* ``ModelOutputError``: a message came back but is not usable JSON (for
  example it hit ``max_tokens``). Billed; no refund.
"""

from __future__ import annotations

import json

import anthropic

from .verify import ModelOutputError


class ModelUnavailable(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "busy" | "network" | "config" | "upstream"


class ModelRefused(Exception):
    pass


class AnthropicReader:
    """Sends one annotation request and returns the parsed clause JSON."""

    def __init__(self, api_key: str, *, base_url: str | None = None, client=None, timeout: float = 180.0):
        # The key goes straight into the SDK client and nowhere else: never
        # logged, never echoed in a response.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or None,
            max_retries=1,
            timeout=timeout,
        )

    async def read(self, params: dict) -> dict:
        params = dict(params)
        try:
            if "betas" in params:
                message = await self._client.beta.messages.create(**params)
            else:
                message = await self._client.messages.create(**params)
        except anthropic.RateLimitError:
            raise ModelUnavailable("busy") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise ModelUnavailable("config") from None
        except anthropic.BadRequestError:
            raise ModelUnavailable("config") from None
        except anthropic.APIStatusError:
            raise ModelUnavailable("upstream") from None
        except anthropic.APIConnectionError:  # includes APITimeoutError
            raise ModelUnavailable("network") from None

        if message.stop_reason == "refusal":
            raise ModelRefused()
        if message.stop_reason == "max_tokens":
            raise ModelOutputError("the reply was cut off at max_tokens")
        texts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        if not texts:
            raise ModelOutputError("no text block in the reply")
        try:
            data = json.loads(texts[-1])
        except ValueError:
            raise ModelOutputError("the reply was not valid JSON") from None
        return {"data": data, "model": message.model}
