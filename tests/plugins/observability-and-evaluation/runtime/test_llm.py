"""The call surface: schema-constrained decoding and its downgrade path.

No network. `complete` is exercised against a stub client that records what it
was sent, because the things worth asserting here are all about the request.
"""
import asyncio

import httpx
import pytest
from openai import BadRequestError

from ojw_evaluation.core.llm import complete, json_schema_format
from ojw_evaluation.core.providers import PROVIDERS, request_extras
from ojw_evaluation.modules.failure_detector.schema import ZERO_SHOT_JSON_SCHEMA

FORMAT = json_schema_format("zero_shot_annotations", ZERO_SHOT_JSON_SCHEMA)


class _Message:
    def __init__(self, content):
        self.content = content


class _Response:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Message(content)})()]


def _bad_request(message):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    return BadRequestError(message, response=response, body=None)


class _StubClient:
    """Records every create() call; optionally fails the first one."""

    def __init__(self, fail_first: Exception | None = None):
        self.calls = []
        self._fail_first = fail_first
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_first is not None and len(self.calls) == 1:
            raise self._fail_first
        return _Response('{"annotations": []}')


def _run(client, **kwargs):
    """Drive one `complete` call to completion. Sync, so no pytest-asyncio."""
    return asyncio.run(complete(client, "m", "sys", "usr", **kwargs))


def test_response_format_is_sent_when_given():
    client = _StubClient()
    _run(client, response_format=FORMAT)
    assert client.calls[0]["response_format"] is FORMAT


def test_extra_body_is_sent_when_given():
    client = _StubClient()
    _run(client, extra_body={"provider": {"require_parameters": True}})
    assert client.calls[0]["extra_body"] == {"provider": {"require_parameters": True}}


def test_neither_is_sent_when_absent():
    """An unconstrained call must not carry empty keys some providers reject."""
    client = _StubClient()
    _run(client, temperature=0.2)
    assert "response_format" not in client.calls[0]
    assert "extra_body" not in client.calls[0]
    assert client.calls[0]["temperature"] == 0.2


def test_a_rejected_schema_downgrades_to_one_unconstrained_retry(caplog):
    """A degraded call that might parse beats a run that dies on a status code."""
    client = _StubClient(fail_first=_bad_request("response_format is not supported"))
    text = _run(client, response_format=FORMAT)

    assert text == '{"annotations": []}'
    assert len(client.calls) == 2
    assert "response_format" in client.calls[0]
    assert "response_format" not in client.calls[1]


def test_the_downgrade_is_logged_because_it_reopens_the_failure_mode(caplog):
    client = _StubClient(fail_first=_bad_request("nope"))
    with caplog.at_level("WARNING"):
        _run(client, response_format=FORMAT)
    assert "retrying unconstrained" in caplog.text


def test_an_unconstrained_call_does_not_retry():
    """Without response_format there is nothing to downgrade — the error is real."""
    client = _StubClient(fail_first=_bad_request("bad model"))
    with pytest.raises(BadRequestError):
        _run(client)
    assert len(client.calls) == 1


def test_openrouter_pins_routing_to_providers_that_honour_parameters():
    """Otherwise OpenRouter silently drops response_format and the schema is moot."""
    assert request_extras("openrouter") == {"provider": {"require_parameters": True}}


def test_providers_without_extras_get_an_empty_dict():
    assert request_extras("openai") == {}
    assert request_extras("unknown-provider") == {}


def test_request_extras_cannot_mutate_the_provider_table():
    extras = request_extras("openrouter")
    extras["provider"] = "clobbered"
    assert PROVIDERS["openrouter"]["extra_body"]["provider"] == {
        "require_parameters": True
    }
