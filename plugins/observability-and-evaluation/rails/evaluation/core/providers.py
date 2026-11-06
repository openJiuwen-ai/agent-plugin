"""Provider profiles: endpoint, credentials, and sampling-parameter policy.

The parameter policy exists because reasoning models reject the sampling
parameters the old runner passed unconditionally:

    400 Unsupported value: 'temperature' does not support 0.7 with this model.
    Only the default (1) value is supported.

That made the documented OpenAI command fail on its first API call, every time.
Legality is model-specific, so it belongs here rather than as an argparse default.
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI


# Models that accept only the default sampling parameters.
_FIXED_SAMPLING = re.compile(r"^(gpt-5|o[1-9])", re.IGNORECASE)

PROVIDERS = {
    "openai": {
        "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.5",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-5.5",
        # OpenRouter fans one model id out across several upstream providers,
        # and by default it silently DROPS parameters the chosen upstream does
        # not support. A dropped response_format looks exactly like a model that
        # ignored the schema. require_parameters restricts routing to upstreams
        # that honour it, turning a silent downgrade into a visible 404.
        "extra_body": {"provider": {"require_parameters": True}},
    },
    "mlops": {
        "env": "MLOPS_API_KEY",
        "base_url": "http://mlops.huawei.com/mlops-service/api/v2/agentService/v1/",
        "default_model": "DeepSeek-V4-Flash-W8A8",
    },
}


def supports_sampling_params(model: str) -> bool:
    """False for reasoning models that only accept temperature/top_p == 1."""
    return not _FIXED_SAMPLING.match((model or "").split("/")[-1])


def sampling_params(model: str, temperature=None, top_p=None) -> dict:
    """Filter sampling parameters down to what this model actually accepts."""
    if not supports_sampling_params(model):
        return {}
    params = {}
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    return params


def request_extras(client: str) -> dict:
    """Provider-specific `extra_body` sent with every chat completion."""
    return dict(PROVIDERS.get(client, {}).get("extra_body") or {})


def build_client(client: str) -> tuple[AsyncOpenAI, str]:
    """Return an (AsyncOpenAI, default_model) pair for the named provider."""
    from openai import AsyncOpenAI

    if client not in PROVIDERS:
        raise ValueError(
            f"Unknown client {client!r}. Choose from: {', '.join(PROVIDERS)}"
        )
    profile = PROVIDERS[client]
    api_key = os.environ.get(profile["env"])
    if not api_key:
        raise ValueError(
            f"Missing API key for client {client!r}. "
            f"Set the {profile['env']} environment variable."
        )
    return (
        AsyncOpenAI(api_key=api_key, base_url=profile["base_url"]),
        profile["default_model"],
    )
