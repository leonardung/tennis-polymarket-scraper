"""Thin read-only client for the Gamma and CLOB APIs. No auth required."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

import httpx

from .config import BOOKS_CHUNK, CLOB, GAMMA

log = logging.getLogger(__name__)

# Gamma returns these as JSON-encoded strings rather than real arrays.
_ENCODED_FIELDS = ("outcomes", "outcomePrices", "clobTokenIds")


def _decode(obj: dict[str, Any]) -> dict[str, Any]:
    for field in _ENCODED_FIELDS:
        value = obj.get(field)
        if isinstance(value, str):
            try:
                obj[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    for market in obj.get("markets") or []:
        if isinstance(market, dict):
            _decode(market)
    return obj


class Polymarket:
    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "polymarket-tennis/0.1"},
            transport=httpx.HTTPTransport(retries=2),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Polymarket":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------- Gamma (catalog) ----------------

    def paginate(
        self, path: str, page_size: int = 100, max_pages: int = 60, **params: Any
    ) -> Iterator[dict[str, Any]]:
        """Page through a Gamma list endpoint via limit/offset."""
        for page in range(max_pages):
            query = {**params, "limit": page_size, "offset": page * page_size}
            resp = self._client.get(f"{GAMMA}{path}", params=query)
            resp.raise_for_status()
            batch = resp.json()
            if isinstance(batch, dict):  # some deployments wrap in {"data": [...]}
                batch = batch.get("data") or []
            if not batch:
                return
            for item in batch:
                yield _decode(item)
            if len(batch) < page_size:
                return
        log.warning("hit max_pages=%s paging %s; results may be truncated", max_pages, path)

    def events(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self.paginate("/events", **params)

    def markets(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self.paginate("/markets", **params)

    def tag_id(self, slug: str) -> str | None:
        """Resolve a Gamma tag slug (e.g. 'tennis') to its numeric id."""
        for path in (f"/tags/slug/{slug}", "/tags"):
            try:
                resp = self._client.get(f"{GAMMA}{path}", params={"limit": 500})
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for tag in candidates:
                if isinstance(tag, dict) and tag.get("slug") == slug:
                    return str(tag.get("id"))
        return None

    # ---------------- CLOB (books) ----------------

    def books(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-fetch order books. Returns {token_id: book}; missing ids are omitted."""
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(token_ids), BOOKS_CHUNK):
            chunk = token_ids[start : start + BOOKS_CHUNK]
            body = [{"token_id": tid} for tid in chunk]
            resp = self._client.post(f"{CLOB}/books", json=body)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict):
                payload = payload.get("data") or []
            for book in payload:
                asset = str(book.get("asset_id") or book.get("assetId") or "")
                if asset:
                    out[asset] = book
        return out

    def book(self, token_id: str) -> dict[str, Any]:
        resp = self._client.get(f"{CLOB}/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()
