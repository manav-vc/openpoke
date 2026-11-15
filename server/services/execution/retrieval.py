"""Bounded hybrid discovery over durable agent records.

Identity evidence and ranking are separate: cosine similarity is never a
probability of correct reuse. The interaction layer still inspects current
state before dispatching a selected candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
import numpy as np

from ...config import get_settings
from .budgets import estimate_tokens, truncate_text

logger = logging.getLogger(__name__)
_IDENTITY_KINDS = {"agent_id", "task_id", "invoice_id", "case_id"}
_EMAIL_KINDS = {"email", "contact", "sender", "recipient"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reference_value(kind: str, value: Any) -> str:
    value = str(value or "").strip()
    if kind in _EMAIL_KINDS:
        match = re.search(r"[\w.+-]+@[\w.-]+", value)
        return (match.group(0) if match else value).casefold()
    if kind in {"invoice_id", "case_id"}:
        return value.casefold()
    return value


def reference_evidence(record: dict, references: Iterable[dict] | None) -> tuple[list[dict], list[dict]]:
    """Compare confirmed source-backed fields, keeping missing fields unknown."""
    matches, conflicts = [], []
    known = [ref for ref in record.get("references", []) if ref.get("confirmed")]
    groups: dict[tuple[str, str], list[dict]] = {}
    for requested in references or []:
        kind = requested.get("kind", "")
        value = _reference_value(kind, requested.get("value"))
        if not value:
            continue
        groups.setdefault((kind, requested.get("scope") or ""), []).append(requested)
    for (kind, scope), requested_group in groups.items():
        if kind == "agent_id":
            options = [{"kind": kind, "value": record["id"], "scope": ""}]
        else:
            options = [ref for ref in known if ref.get("kind") == kind]
        scoped = [ref for ref in options if not scope or not ref.get("scope") or ref.get("scope") == scope]
        known_values = {_reference_value(kind, ref.get("value")) for ref in scoped}
        for requested in requested_group:
            if _reference_value(kind, requested.get("value")) in known_values:
                matches.append({"kind": kind, "value": requested["value"], "scope": scope})
        # Multiple explicit IDs may describe a comparison across distinct tasks.
        # Model-added weak alternatives cannot relax trusted user constraints.
        trusted = [ref for ref in requested_group if ref.get("trusted", True)]
        allowed = {_reference_value(kind, ref.get("value")) for ref in trusted}
        if scoped and kind in _IDENTITY_KINDS and allowed and not known_values.intersection(allowed):
            conflicts.append({"kind": kind, "requested_values": [ref["value"] for ref in trusted], "known": [ref.get("value") for ref in scoped]})
    return matches, conflicts


def _identity_resolution(evidence: dict[str, tuple[list[dict], list[dict]]], eligible_ids: set[str], references: list[dict]):
    by_reference: dict[tuple[str, str, str], set[str]] = {}
    original = {}
    for reference in references:
        key = (reference.get("kind", ""), reference.get("scope") or "", _reference_value(reference.get("kind", ""), reference.get("value")))
        original[key] = reference
    for agent_id in eligible_ids:
        for reference in evidence[agent_id][0]:
            kind = reference["kind"]
            key = (kind, reference.get("scope") or "", _reference_value(kind, reference.get("value")))
            by_reference.setdefault(key, set()).add(agent_id)
    strong_resolved = {key for key in by_reference if key[0] in _IDENTITY_KINDS}
    relevant = strong_resolved or {key for key in by_reference if key[0] in {"thread_id", "message_id", "draft_id"}}
    ambiguous = [
        {"kind": key[0], "value": original.get(key, {}).get("value", key[2]), "scope": key[1], "agent_ids": sorted(by_reference[key])}
        for key in sorted(relevant) if len(by_reference[key]) > 1
    ]
    wanted_strong = {key for key in original if key[0] in _IDENTITY_KINDS}
    unresolved = [original[key] for key in sorted(wanted_strong - strong_resolved)]
    return ambiguous, unresolved


class OpenRouterEmbedder:
    """Small embedding adapter sharing the application's OpenRouter account."""

    def __init__(self, settings=None, *, transport=None):
        self.settings = settings or get_settings()
        self.model = self.settings.agent_embedding_model
        self.dimensions = self.settings.agent_embedding_dimensions
        self.transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.settings.openrouter_api_key:
            raise RuntimeError("OpenRouter embedding API key is not configured")
        if not texts:
            return []
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {self.settings.openrouter_api_key}"},
                json={"model": self.model, "input": texts, "dimensions": self.dimensions, "encoding_format": "float"},
                timeout=self.settings.agent_embedding_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Embedding response does not match input count")
        vectors = [None] * len(texts)
        for item in data:
            index = item.get("index")
            if type(index) is not int or index < 0 or index >= len(texts) or vectors[index] is not None:
                raise ValueError("Embedding response has invalid or duplicate indexes")
            vectors[index] = _normalized(item.get("embedding"), self.dimensions).tolist()
        logger.info("agent embedding usage", extra={"model": self.model, "usage": payload.get("usage", {}), "input_count": len(texts)})
        return vectors


def _normalized(vector: Any, dimensions: int) -> np.ndarray:
    result = np.asarray(vector, dtype=np.float32)
    if result.shape != (dimensions,) or not np.all(np.isfinite(result)):
        raise ValueError("Invalid embedding dimensions or nonfinite values")
    norm = float(np.linalg.norm(result))
    if norm <= 0:
        raise ValueError("A zero embedding cannot be normalized")
    return result / norm


class SearchService:
    def __init__(self, registry=None, embedder=None, settings=None):
        if registry is None:
            from .registry import get_registry

            registry = get_registry()
        self.registry = registry
        self.settings = settings or get_settings()
        self.embedder = embedder or OpenRouterEmbedder(self.settings)
        self._snapshot_key = None
        self._snapshot_lock = threading.RLock()
        self._snapshot = ([], np.empty((0, self.settings.agent_embedding_dimensions), dtype=np.float32))
        self._query_cache: OrderedDict[str, tuple[float, np.ndarray]] = OrderedDict()
        self._index_lock = asyncio.Lock()

    def _records(self, scope: str | None = None) -> list[dict]:
        # Two bulk reads avoid a reference lookup for each of 10,000 records.
        with self.registry.connect() as conn:
            where, parameters = "", ()
            if scope:
                where = " WHERE NOT EXISTS(SELECT 1 FROM agent_references r WHERE r.agent_id=a.id AND r.scope!='') OR EXISTS(SELECT 1 FROM agent_references r WHERE r.agent_id=a.id AND r.scope=?)"
                parameters = (scope,)
            records = {row["id"]: {**dict(row), "references": []} for row in conn.execute("SELECT a.* FROM agents a" + where, parameters)}
            for row in conn.execute("SELECT * FROM agent_references"):
                if row["agent_id"] in records:
                    records[row["agent_id"]]["references"].append(dict(row))
        return list(records.values())

    def _keyword(self, query: str, eligible_ids: set[str]) -> list[str]:
        terms = list(dict.fromkeys(re.findall(r"[\w]+(?:[-@.][\w]+)*", query, re.UNICODE)))[:32]
        if not terms or not eligible_ids:
            return []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        placeholders = ",".join("?" for _ in eligible_ids)
        with self.registry.connect() as conn:
            rows = conn.execute(
                f"SELECT agent_id FROM agent_fts WHERE agent_fts MATCH ? AND agent_id IN ({placeholders}) ORDER BY bm25(agent_fts),agent_id LIMIT ?",
                (expression, *sorted(eligible_ids), self.settings.agent_candidate_pool),
            ).fetchall()
        return [row["agent_id"] for row in rows]

    def _matrix_snapshot(self):
        with self._snapshot_lock:
            return self._read_matrix_snapshot()

    def _read_matrix_snapshot(self):
        with self.registry.connect() as conn:
            metadata = conn.execute(
                "SELECT e.agent_id,e.search_version,e.updated_at FROM embeddings e JOIN agents a ON a.id=e.agent_id "
                "WHERE e.search_version=a.search_version AND e.model=? AND e.dimensions=? ORDER BY e.agent_id",
                (self.settings.agent_embedding_model, self.settings.agent_embedding_dimensions),
            ).fetchall()
            signature = (self.registry.generation, tuple(tuple(row) for row in metadata))
            if signature == self._snapshot_key:
                return self._snapshot
            rows = conn.execute(
                "SELECT e.agent_id,e.vector FROM embeddings e JOIN agents a ON a.id=e.agent_id "
                "WHERE e.search_version=a.search_version AND e.model=? AND e.dimensions=? ORDER BY e.agent_id",
                (self.settings.agent_embedding_model, self.settings.agent_embedding_dimensions),
            ).fetchall()
        ids, vectors = [], []
        for row in rows:
            try:
                vector = _normalized(np.frombuffer(row["vector"], dtype=np.float32), self.settings.agent_embedding_dimensions)
            except (ValueError, TypeError):
                logger.warning("Invalid persisted agent vector", extra={"agent_id": row["agent_id"]})
                continue
            ids.append(row["agent_id"])
            vectors.append(vector)
        matrix = np.stack(vectors) if vectors else np.empty((0, self.settings.agent_embedding_dimensions), dtype=np.float32)
        matrix.flags.writeable = False
        self._snapshot = (ids, matrix)
        self._snapshot_key = signature
        return self._snapshot

    async def _query_vector(self, query: str) -> np.ndarray:
        text = truncate_text(query, 2000)
        key = hashlib.sha256((self.settings.agent_embedding_model + "\0" + str(self.settings.agent_embedding_dimensions) + "\0" + text).encode()).hexdigest()
        cached = self._query_cache.get(key)
        if cached and time.monotonic() - cached[0] < 3600:
            self._query_cache.move_to_end(key)
            return cached[1]
        values = await self.embedder.embed([text])
        if len(values) != 1:
            raise ValueError("Query embedding response count mismatch")
        vector = _normalized(values[0], self.settings.agent_embedding_dimensions)
        self._query_cache[key] = (time.monotonic(), vector)
        self._query_cache.move_to_end(key)
        while len(self._query_cache) > 256:
            self._query_cache.popitem(last=False)
        return vector

    async def _semantic(self, query: str, eligible_ids: set[str]) -> tuple[list[str], list[str]]:
        if not self.settings.agent_hybrid_search_enabled or not query.strip() or not eligible_ids:
            return [], []
        ids, matrix = await asyncio.to_thread(self._matrix_snapshot)
        positions = [index for index, agent_id in enumerate(ids) if agent_id in eligible_ids]
        missing = len(eligible_ids - set(ids))
        reasons = ["Some eligible agents have missing or stale embeddings"] if missing else []
        if not positions:
            return [], reasons or ["No current embeddings are available"]
        vector = await self._query_vector(query)

        def rank():
            scores = matrix[positions] @ vector
            ordered = sorted(range(len(positions)), key=lambda index: (-float(scores[index]), ids[positions[index]]))
            return [ids[positions[index]] for index in ordered[:self.settings.agent_candidate_pool]]

        return await asyncio.to_thread(rank), reasons

    def _compact(self, record: dict, matches: list[dict], score: float) -> dict:
        # Leave space for immutable IDs and identity evidence in each record.
        result = {
            "id": record["id"], "name": truncate_text(record.get("name", ""), 40),
            "purpose": truncate_text(record.get("purpose", ""), 40),
            "summary": truncate_text(record.get("summary", ""), 80),
            "state": record.get("state", "open"), "archived": bool(record.get("archived")),
            "version": record["version"], "identifier_matches": matches,
            "ranking_score": score,
        }
        # Preserve identifiers as structured fields even if prose needs removal.
        for field in ("summary", "purpose", "name"):
            current = estimate_tokens(json.dumps(result, default=str))
            if current <= self.settings.agent_candidate_tokens:
                break
            allowance = max(0, estimate_tokens(result[field]) - (current - self.settings.agent_candidate_tokens) - 2)
            result[field] = truncate_text(result[field], allowance)
        if estimate_tokens(json.dumps(result, default=str)) > self.settings.agent_candidate_tokens:
            result["identifier_matches"] = [{"kind": ref["kind"], "value": truncate_text(ref.get("value", ""), 20)} for ref in matches[:2]]
        return result

    async def search(self, query: str, references: list[dict] | None = None, include_archived: bool = False, scope: str | None = None) -> dict:
        started = time.perf_counter()
        generation = self.registry.generation
        references = references or []
        scoped = {ref.get("scope") for ref in references if ref.get("scope")}
        if scope is None and len(scoped) == 1:
            scope = next(iter(scoped))
        records = await asyncio.to_thread(self._records, scope)
        evidence = {record["id"]: reference_evidence(record, references) for record in records}
        eligible = {record["id"]: record for record in records if not evidence[record["id"]][1]}
        explicit_ids = {str(ref.get("value")) for ref in references if ref.get("kind") == "agent_id"}
        if explicit_ids:
            eligible = {key: record for key, record in eligible.items() if key in explicit_ids}
        exact = sorted(key for key in eligible if any(ref["kind"] in _IDENTITY_KINDS | {"thread_id", "message_id", "draft_id"} for ref in evidence[key][0]))
        ambiguous, unresolved = _identity_resolution(evidence, set(eligible), references)
        if generation != self.registry.generation:
            raise RuntimeError("Agent registry was reset during discovery; repeat the search")
        if len(exact) > self.settings.agent_candidate_pool:
            return {"candidates": [], "exact_match_count": len(exact), "requires_clarification": True, "ambiguous_identifiers": ambiguous, "unresolved_identifiers": unresolved, "degraded": False, "degradation_reasons": [], "total_candidates": len(exact)}
        active = {key for key, record in eligible.items() if include_archived or not record.get("archived")}
        active.update(exact)
        # A known unique identity resolves without a provider round-trip.
        if len(exact) == 1 and not unresolved and (explicit_ids or any(ref["kind"] in _IDENTITY_KINDS for ref in evidence[exact[0]][0])):
            return {"candidates": [self._compact(eligible[exact[0]], evidence[exact[0]][0], 1.0)], "exact_match_count": 1, "requires_clarification": False, "ambiguous_identifiers": [], "unresolved_identifiers": [], "degraded": False, "degradation_reasons": [], "total_candidates": 1}
        outcomes = await asyncio.gather(
            asyncio.to_thread(self._keyword, query, active),
            self._semantic(query, active),
            return_exceptions=True,
        )
        reasons, keyword, semantic = [], [], []
        if isinstance(outcomes[0], BaseException):
            logger.warning("Agent keyword retrieval failed", extra={"error": str(outcomes[0])})
            reasons.append("Keyword discovery is unavailable")
        else:
            keyword = outcomes[0]
        if isinstance(outcomes[1], BaseException):
            logger.warning("Agent semantic retrieval failed", extra={"error": str(outcomes[1])})
            reasons.append("Semantic discovery is unavailable")
        else:
            semantic, embedding_reasons = outcomes[1]
            reasons.extend(embedding_reasons)
        scores: dict[str, float] = {}
        for ranking in (keyword, semantic):
            for rank, key in enumerate(ranking, 1):
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
        ordered = exact + sorted((key for key in scores if key not in exact), key=lambda key: (-scores[key], key))
        if not query.strip() and not references:
            ordered = sorted(active, key=lambda key: (str(eligible[key].get("last_activity", "")), key), reverse=True)
        ordered = ordered[:self.settings.agent_candidate_pool]
        if generation != self.registry.generation:
            raise RuntimeError("Agent registry was reset during discovery; repeat the search")
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("agent discovery", extra={"eligible_count": len(active), "candidate_count": len(ordered), "elapsed_ms": round(elapsed, 2), "degraded": bool(reasons)})
        return {"candidates": [self._compact(eligible[key], evidence[key][0], scores.get(key, 1.0 if key in exact else 0.0)) for key in ordered], "exact_match_count": len(exact), "requires_clarification": bool(ambiguous), "ambiguous_identifiers": ambiguous, "unresolved_identifiers": unresolved, "degraded": bool(reasons), "degradation_reasons": reasons, "total_candidates": len(ordered), "elapsed_ms": round(elapsed, 2)}

    def _pending(self, limit: int) -> list[dict]:
        with self.registry.connect() as conn:
            rows = conn.execute(
                "SELECT agent_id,search_version FROM index_outbox WHERE available_at IS NULL OR available_at='' OR available_at<=? ORDER BY agent_id LIMIT ?",
                (_utc_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def index_pending(self, limit: int = 64, generation: int | None = None) -> int:
        """Process durable index work, checking both generation and content version."""
        if not self.settings.agent_hybrid_search_enabled:
            return 0
        async with self._index_lock:
            generation = self.registry.generation if generation is None else generation
            pending = await asyncio.to_thread(self._pending, max(1, min(limit, 64)))
            records = []
            for row in pending:
                try:
                    record = self.registry.get(row["agent_id"])
                except KeyError:
                    continue
                if record and record["search_version"] == row["search_version"]:
                    records.append(record)
            if not records or generation != self.registry.generation:
                return 0
            texts = [truncate_text("\n".join((record.get("name", ""), record.get("purpose", ""), record.get("summary", ""), " ".join(str(ref.get("value", "")) for ref in record.get("references", []) if ref.get("confirmed")))), 2000) for record in records]
            try:
                embedded = await self.embedder.embed(texts)
                if len(embedded) != len(records):
                    raise ValueError("Index embedding response count mismatch")
                vectors = [_normalized(value, self.settings.agent_embedding_dimensions) for value in embedded]
            except Exception as exc:
                with self.registry.connect(write=True) as conn:
                    if generation != self.registry.generation:
                        return 0
                    for record in records:
                        conn.execute(
                            "UPDATE index_outbox SET attempts=attempts+1,last_error=?,available_at=? WHERE agent_id=? AND search_version=?",
                            (str(exc)[:300], (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), record["id"], record["search_version"]),
                        )
                logger.warning("Agent embedding index delayed", extra={"error": str(exc), "records": len(records)})
                return 0
            saved = 0
            with self.registry.connect(write=True) as conn:
                if generation != self.registry.generation:
                    return 0
                for record, vector in zip(records, vectors):
                    row = conn.execute("SELECT search_version FROM agents WHERE id=?", (record["id"],)).fetchone()
                    if not row or row["search_version"] != record["search_version"]:
                        continue
                    conn.execute(
                        "INSERT INTO embeddings(agent_id,model,dimensions,search_version,vector,updated_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(agent_id) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,search_version=excluded.search_version,vector=excluded.vector,updated_at=excluded.updated_at",
                        (record["id"], self.settings.agent_embedding_model, self.settings.agent_embedding_dimensions, record["search_version"], vector.tobytes(), _utc_now()),
                    )
                    conn.execute("DELETE FROM index_outbox WHERE agent_id=? AND search_version=?", (record["id"], record["search_version"]))
                    saved += 1
            self._snapshot_key = None
            return saved


_search_service: SearchService | None = None


def get_search_service() -> SearchService:
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service
