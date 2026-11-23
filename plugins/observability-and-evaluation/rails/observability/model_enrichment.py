"""Enrich copies of rail spans; SDK spans remain unmodified raw evidence."""
from copy import deepcopy


def _key(span):
    context = span.get("context", {})
    return context.get("trace_id"), context.get("span_id")


def _first(attrs, *keys):
    return next((attrs[key] for key in keys if attrs.get(key) not in (None, "")), None)


def _usage(attrs):
    values = [_first(attrs, *keys) for keys in (
        ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.usage.prompt_tokens"),
        ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.usage.completion_tokens"),
        ("gen_ai.usage.total_tokens", "llm.usage.total_tokens"),
    )]
    if all(value is None for value in values):
        return None
    try:
        incoming, outgoing, total = (int(value or 0) for value in values)
    except (TypeError, ValueError):
        return None
    if min(incoming, outgoing, total) < 0:
        return None
    return incoming, outgoing, total or incoming + outgoing


def enrich_model_spans(local_spans, native_spans):
    """Return extraction spans, with exactly one model record per rail call.

    Only a unique outer SDK model descendant supplies metadata. Multiple sibling
    SDK calls are ambiguous; retain rail values rather than invent one response
    or aggregate possibly duplicated usage. HTTP retries do not cause ambiguity.
    Matching always includes trace ID and follows parent IDs, never timestamps.
    """
    local = deepcopy(local_spans)
    all_spans = local + native_spans
    index = {_key(span): span for span in all_spans}
    models = {_key(span): span for span in local if span.get("name") == "openai.chat"}
    sdk = {_key(span): span for span in native_spans if span.get("name") == "openai.chat"}

    def ancestors(span):
        trace_id, _ = _key(span)
        parent = span.get("parent_id")
        seen = set()
        while parent and (trace_id, parent) not in seen:
            key = (trace_id, parent)
            seen.add(key)
            ancestor = index.get(key)
            if ancestor is None:
                return
            yield key
            parent = ancestor.get("parent_id")

    children = {key: [] for key in models}
    for key, span in sdk.items():
        # Nested SDK instrumentation of the same call is not a second request.
        for ancestor in ancestors(span):
            if ancestor in sdk:
                break
            if ancestor in models:
                children[ancestor].append(span)
                break

    for key, model in models.items():
        candidates = children[key]
        if len(candidates) != 1:
            continue
        selected = candidates[0]
        attrs = model.setdefault("attributes", {})
        sdk_attrs = selected.get("attributes", {})
        for target, sources in (
            ("gen_ai.response.id", ("gen_ai.response.id",)),
            ("gen_ai.response.model", ("gen_ai.response.model",)),
            ("gen_ai.system", ("gen_ai.provider.name", "gen_ai.system", "llm.system")),
        ):
            value = _first(sdk_attrs, *sources)
            if value is not None:
                attrs[target] = value
        if not attrs.get("gen_ai.request.model"):
            value = _first(sdk_attrs, "gen_ai.request.model", "llm.request.model")
            if value is not None:
                attrs["gen_ai.request.model"] = value
        if not attrs.get("observability.usage_available", False):
            usage = _usage(sdk_attrs)
            if usage is not None:
                for field, value in zip(("input_tokens", "output_tokens", "total_tokens"), usage):
                    attrs[f"gen_ai.usage.{field}"] = value
                attrs["observability.usage_available"] = True

        attempts = [span for span in native_spans
                    if _first(span.get("attributes", {}), "url.full", "http.url")
                    and _key(selected) in ancestors(span)]
        successful = [span for span in attempts if str(_first(span.get("attributes", {}),
                      "http.response.status_code", "http.status_code") or "").startswith("2")]
        attempts = sorted(successful or attempts, key=lambda span: span.get("end_time", ""))
        url = (_first(attempts[-1].get("attributes", {}), "url.full", "http.url") if attempts else None)
        url = url or _first(sdk_attrs, "url.full", "http.url", "gen_ai.openai.api_base", "server.address")
        if url:
            # The legacy extractor prefers server.address over http.url.
            attrs["server.address"] = url
            attrs["http.url"] = url

    return local + [deepcopy(span) for span in native_spans if span.get("name") != "openai.chat"]
