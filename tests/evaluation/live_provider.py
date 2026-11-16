"""Bounded OpenRouter transport used only by the workflow evaluator.

Reservations are estimates, not a guarantee about the provider's final bill.
Unknown usage and failed attempts retain their reservation. No automatic retries.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import httpx

from server.services.execution.budgets import estimate_tokens

BASE_URL = "https://openrouter.ai/api/v1"


class BudgetExceeded(RuntimeError):
    pass


class CatalogUnavailable(RuntimeError):
    """Model discovery failed before any billable requests were submitted."""


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Expected a finite, nonnegative price or usage value")
    return number


def text_request_pricing(price, model):
    """Reserve for this harness's text/function calls, without server-side tools.

    Cache writes can cost more than ordinary input, so reserve the largest input
    rate. Optional image/audio/video/search tariffs do not apply to this payload.
    Unknown nonzero fees still stop the run and name the unsupported fields.
    """
    cache_fields = {"input_cache_read", "input_cache_write", "input_cache_write_1h"}
    optional_fields = {"image", "image_output", "audio", "audio_output", "video", "web_search"}
    known = {"prompt", "completion", "request", "internal_reasoning", "discount"} | cache_fields | optional_fields
    unknown = [key for key, value in price.items()
               if key not in known and value is not None and nonnegative(value)]
    if unknown:
        raise ValueError(f"Unsupported additional pricing for {model}: {', '.join(sorted(unknown))}")
    prompt = nonnegative(price["prompt"])
    prompt = max([prompt] + [nonnegative(price[key]) for key in cache_fields if price.get(key) is not None])
    # Treat separately listed reasoning charges as additive for a conservative reservation.
    completion = nonnegative(price.get("completion", 0)) + nonnegative(price.get("internal_reasoning") or 0)
    return dict(prompt=prompt, completion=completion, request=nonnegative(price.get("request") or 0),
                catalog_pricing=dict(price),
                unused_feature_prices={key: value for key, value in price.items() if key in optional_fields},
                reservation_policy="Largest input/cache rate; completion plus separately listed reasoning rate; no discount assumed.")


def build_chat_payload(*, model, messages, system, tools, max_output_tokens, supported_parameters):
    payload = dict(model=model, messages=([{"role": "system", "content": system}] if system else []) + messages,
                   max_tokens=max_output_tokens, stream=False)
    if "temperature" in supported_parameters:
        payload["temperature"] = 0
    if tools:
        payload["tools"] = tools
    return payload


class UsageLedger:
    def __init__(self, *, max_calls=100, max_tokens=250_000, max_cost_usd=2.0):
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.max_cost_usd = max_cost_usd
        self.calls = []
        self.stop_reason = None

    def reserve(self, *, kind, model, input_tokens, output_tokens, pricing, case_id):
        tokens = input_tokens + output_tokens
        cost = input_tokens * pricing["prompt"] + output_tokens * pricing["completion"] + pricing["request"]
        reason = None
        if len(self.calls) >= self.max_calls:
            reason = "Model-call limit reached"
        elif sum(c["accounted_tokens"] for c in self.calls) + tokens > self.max_tokens:
            reason = "Token reservation limit reached"
        elif sum(c["accounted_cost_usd"] for c in self.calls) + cost > self.max_cost_usd:
            reason = "Cost reservation limit reached"
        if self.stop_reason or reason:
            self.stop_reason = self.stop_reason or reason
            raise BudgetExceeded(self.stop_reason)
        row = dict(index=len(self.calls), kind=kind, model=model, case_id=case_id,
                   status="pending", reserved_tokens=tokens, reserved_cost_usd=cost,
                   accounted_tokens=tokens, accounted_cost_usd=cost, usage=None)
        self.calls.append(row)
        return row

    def finish(self, row, usage, elapsed_ms, *, error=None, response_id=None):
        row.update(status="failed" if error else "completed", elapsed_ms=round(elapsed_ms, 2),
                   error=error, response_id=response_id, usage=usage or None)
        if usage:
            if usage.get("total_tokens") is not None:
                row["accounted_tokens"] = nonnegative(usage["total_tokens"])
            if usage.get("cost") is not None:
                row["accounted_cost_usd"] = nonnegative(usage["cost"])
        if (sum(c["accounted_tokens"] for c in self.calls) > self.max_tokens
                or sum(c["accounted_cost_usd"] for c in self.calls) > self.max_cost_usd):
            self.stop_reason = "Reported usage exceeded estimated reservations; further calls stopped"

    def summary(self):
        known_costs = [c["usage"]["cost"] for c in self.calls
                       if c.get("usage") and c["usage"].get("cost") is not None]
        return dict(
            calls=len(self.calls), failed_calls=sum(c["status"] != "completed" for c in self.calls),
            prompt_tokens=sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in self.calls),
            completion_tokens=sum((c.get("usage") or {}).get("completion_tokens", 0) for c in self.calls),
            calls_with_reported_tokens=sum(bool(c.get("usage") and c["usage"].get("total_tokens") is not None)
                                           for c in self.calls),
            provider_reported_cost_usd=sum(map(float, known_costs)) if known_costs else None,
            calls_with_reported_cost=len(known_costs),
            accounted_cost_usd=sum(c["accounted_cost_usd"] for c in self.calls),
            accounted_tokens=sum(c["accounted_tokens"] for c in self.calls),
            limits=dict(calls=self.max_calls, tokens=self.max_tokens, estimated_cost_usd=self.max_cost_usd),
            stop_reason=self.stop_reason,
            note="Unknown usage retains its reservation. Reservations are estimates, not provider charges.",
        )


class LiveProvider:
    def __init__(self, api_key, ledger, trace_path: Path, *, max_output_tokens=1200,
                 catalog_timeout_seconds=30):
        self.api_key, self.ledger, self.trace_path = api_key, ledger, trace_path
        self.max_output_tokens = max_output_tokens
        self.catalog_timeout_seconds = catalog_timeout_seconds
        self.prices = {}
        self.catalog_pricing = {}
        self.supported_parameters = {}
        self.case_id = "preindex"

    def _trace(self, value):
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    def preflight(self, chat_model, embedding_model):
        """Synchronous and time-bounded, before creating an asyncio event loop."""
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not configured")
        headers = {"Authorization": "Bearer " + self.api_key}
        timeout = httpx.Timeout(self.catalog_timeout_seconds, connect=min(10, self.catalog_timeout_seconds))
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for endpoint, wanted in (("/models", chat_model), ("/embeddings/models", embedding_model)):
                for attempt in range(2):
                    try:
                        response = client.get(BASE_URL + endpoint, headers=headers)
                        break
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        if attempt == 1:
                            stage = "embedding" if endpoint == "/embeddings/models" else "chat"
                            raise CatalogUnavailable(
                                f"Could not load OpenRouter's {stage} model catalog ({endpoint}): "
                                f"{type(exc).__name__} after 2 attempts with a "
                                f"{self.catalog_timeout_seconds:g}-second timeout. "
                                "No model calls were submitted. Check the connection to openrouter.ai "
                                "and retry; use --catalog-timeout 60 if the connection is slow."
                            ) from exc
                response.raise_for_status()
                records = response.json().get("data", [])
                record = next((r for r in records if r["id"] == wanted), None)
                if not record:
                    raise ValueError(f"Configured model is absent from OpenRouter's catalog: {wanted}")
                price = record.get("pricing", {})
                self.catalog_pricing[wanted] = dict(price)
                self.supported_parameters[wanted] = set(record.get("supported_parameters", []))
                self.prices[wanted] = text_request_pricing(price, wanted)
                if endpoint == "/models" and "tools" not in record.get("supported_parameters", []):
                    raise ValueError(f"Selected chat model does not advertise tool support: {wanted}")
        return self.prices

    async def _request(self, kind, endpoint, payload):
        output = payload.get("max_tokens", 0)
        # Includes schemas, message envelopes and a safety allowance.
        inputs = estimate_tokens(json.dumps(payload, ensure_ascii=False)) + 128
        row = self.ledger.reserve(kind=kind, model=payload["model"], input_tokens=inputs,
                                  output_tokens=output, pricing=self.prices[payload["model"]],
                                  case_id=self.case_id)
        # Persist before the network call, including attempts killed by the outer deadline.
        self._trace(dict(event="reserved", call=row, request=payload if kind == "chat" else
                         {k: v for k, v in payload.items() if k != "input"}))
        started = time.perf_counter()
        result = None
        error = None
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                response = await client.post(
                    BASE_URL + endpoint, headers={"Authorization": "Bearer " + self.api_key},
                    json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("error"):
                raise RuntimeError("Provider returned an error payload")
            return result
        except BaseException as exc:
            # No response bodies or credentials in errors.
            error = type(exc).__name__
            raise
        finally:
            self.ledger.finish(row, result.get("usage") if result else None,
                               (time.perf_counter() - started) * 1000, error=error,
                               response_id=result.get("id") if result else None)
            row["response_model"] = (result or {}).get("model")
            # Only synthetic inputs, tool calls, assistant content and usage; no headers/reasoning.
            choices = [{"message": {key: value for key, value in c.get("message", {}).items()
                                    if key in {"role", "content", "tool_calls"}},
                        "finish_reason": c.get("finish_reason")} for c in (result or {}).get("choices", [])]
            self._trace(dict(event="finished", call=row, choices=choices))

    async def chat(self, *, model, messages, system=None, tools=None, **ignored):
        payload = build_chat_payload(model=model, messages=messages, system=system, tools=tools,
                                     max_output_tokens=self.max_output_tokens,
                                     supported_parameters=self.supported_parameters.get(model, set()))
        result = await self._request("chat", "/chat/completions", payload)
        if not result.get("choices") or not isinstance(result["choices"][0].get("message"), dict):
            raise RuntimeError("Provider returned no assistant message")
        if result["choices"][0].get("finish_reason") == "length":
            raise RuntimeError("Model output was truncated; raise --max-output-tokens for a new run")
        return result


class LiveEmbedder:
    def __init__(self, provider, settings):
        self.provider, self.settings = provider, settings

    async def embed(self, texts):
        result = await self.provider._request("embedding", "/embeddings", dict(
            model=self.settings.agent_embedding_model, input=texts,
            dimensions=self.settings.agent_embedding_dimensions, encoding_format="float"))
        data = result.get("data", [])
        if (len(data) != len(texts) or {item.get("index") for item in data} != set(range(len(texts)))):
            raise ValueError("Embedding response has missing or duplicate indexes")
        return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
