"""Independent raw evidence collection for exploratory economics research."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import CLOB, GAMMA
from .evidence_chain import CTF, MAX_RPC_REQUESTS, RPC_URL, capture_payout_slots

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300.0
DEFAULT_TIMEOUT = 8.0
DEFAULT_REQUEST_PAUSE = 0.10
DEFAULT_MAX_REQUESTS = 120
PROTOCOL_VERSION = 1
DOC_INTERVAL = 86_400.0
OFFICIAL_DOCUMENTS = (
    "https://docs.polymarket.com/trading/fees.md",
    "https://docs.polymarket.com/resources/contracts.md",
    "https://docs.polymarket.com/market-data/market-details.md",
)


def protocol_settings(*, interval: float, timeout: float, request_pause: float,
                      max_requests: int) -> dict[str, Any]:
    """Freeze operational settings and source identities at protocol creation."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("evidence interval must be finite and positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("evidence timeout must be finite and positive")
    if not math.isfinite(request_pause) or request_pause < 0:
        raise ValueError("evidence request pause must be finite and nonnegative")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 9:
        raise ValueError("evidence max requests must be an integer of at least 9")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "interval": interval,
        "timeout": timeout,
        "request_pause": request_pause,
        "max_requests": max_requests,
        "sources": {
            "clob": CLOB,
            "gamma": GAMMA,
            "official_documents": list(OFFICIAL_DOCUMENTS),
            "polygon_rpc": RPC_URL,
            "conditional_tokens_contract": CTF,
        },
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS protocol (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    started_ts REAL NOT NULL, started_utc_date TEXT NOT NULL,
    source_db TEXT NOT NULL, settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    condition_id TEXT NOT NULL, token_id TEXT NOT NULL,
    token_index INTEGER NOT NULL, match_date TEXT, tour TEXT, market_type TEXT,
    first_seen_ts REAL NOT NULL, fresh_cohort INTEGER NOT NULL,
    PRIMARY KEY (condition_id, token_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL,
    request_ts REAL NOT NULL, received_ts REAL,
    duration_seconds REAL NOT NULL, method TEXT NOT NULL, endpoint TEXT NOT NULL,
    parameters_json TEXT, identity_json TEXT NOT NULL, status_code INTEGER, response_headers_json TEXT,
    response_body BLOB, response_sha256 TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS observations_by_identity
ON observations(identity_json, received_ts);
CREATE TABLE IF NOT EXISTS observation_errors (
    error_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
    recorded_ts REAL NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY, started_ts REAL NOT NULL, finished_ts REAL,
    duration_seconds REAL, identity_count INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS cycle_errors (
    error_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL,
    condition_id TEXT, recorded_ts REAL NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Identity:
    condition_id: str
    token_id: str
    token_index: int
    match_date: str | None
    tour: str | None
    market_type: str | None
    fresh_cohort: bool


class EvidenceStore:
    def __init__(self, path: str | Path, source_db: str | Path, settings: dict[str, Any]) -> None:
        self.path = Path(path)
        source_path = Path(source_db)
        if self.path.resolve() == source_path.resolve() or (
            self.path.exists() and source_path.exists() and os.path.samefile(self.path, source_path)
        ):
            raise ValueError("evidence output must differ from the read-only source database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        now = time.time()
        start_date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO protocol VALUES (1, ?, ?, ?, ?)",
            (now, start_date, str(Path(source_db).resolve()), json.dumps(settings, sort_keys=True)),
        )
        saved_source, saved_settings = self.conn.execute(
            "SELECT source_db, settings_json FROM protocol WHERE singleton = 1"
        ).fetchone()
        expected_source = str(Path(source_db).resolve())
        expected_settings = json.dumps(settings, sort_keys=True)
        if saved_source != expected_source or saved_settings != expected_settings:
            raise ValueError("evidence protocol source or settings differ from the persisted protocol")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def started_utc_date(self) -> str:
        return str(self.conn.execute("SELECT started_utc_date FROM protocol").fetchone()[0])

    def refresh_identities(self, source_db: str | Path) -> list[Identity]:
        """Copy identities from source through a read-only SQLite connection."""
        uri = f"file:{Path(source_db).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as source:
            rows = source.execute(
                "SELECT condition_id, token_0, token_1, match_date, tour, market_type FROM markets "
                "WHERE match_date >= ?",
                (self.started_utc_date,),
            ).fetchall()
        now = time.time()
        for condition_id, token_0, token_1, match_date, tour, market_type in rows:
            fresh = bool(match_date and str(match_date) >= self.started_utc_date)
            for index, token in enumerate((token_0, token_1)):
                if token:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO identities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (condition_id, str(token), index, match_date, tour, market_type, now, fresh),
                    )
        return [Identity(*row[:-1], bool(row[-1])) for row in self.conn.execute(
            "SELECT condition_id, token_id, token_index, match_date, tour, market_type, fresh_cohort "
            "FROM identities ORDER BY condition_id, token_index"
        )]

    def get_progress(self, key: str, default: str = "0") -> str:
        row = self.conn.execute("SELECT value FROM progress WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_progress(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO progress VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def record_observation(
        self, *, cycle_id: str, request_ts: float, received_ts: float | None,
        duration_seconds: float, method: str, endpoint: str,
        parameters: dict[str, Any] | None, identity: dict[str, Any],
        status_code: int | None, headers: dict[str, str] | None,
        body: bytes | None, error: str | None,
    ) -> str:
        observation_id = str(uuid.uuid4())
        digest = hashlib.sha256(body).hexdigest() if body is not None else None
        self.conn.execute(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observation_id, cycle_id, request_ts, received_ts, duration_seconds,
             method, endpoint, json.dumps(parameters, sort_keys=True) if parameters else None,
             json.dumps(identity, sort_keys=True), status_code,
             json.dumps(headers, sort_keys=True) if headers is not None else None, body, digest, error),
        )
        return observation_id

    def record_observation_error(self, observation_id: str, error: str) -> None:
        self.conn.execute(
            "INSERT INTO observation_errors VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), observation_id, time.time(), error),
        )

    def record_cycle_error(self, cycle_id: str, condition_id: str | None, error: str) -> None:
        self.conn.execute(
            "INSERT INTO cycle_errors VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), cycle_id, condition_id, time.time(), error),
        )


class EvidenceCollector:
    def __init__(self, source_db: str | Path, store: EvidenceStore, *, timeout: float = DEFAULT_TIMEOUT,
                 request_pause: float = DEFAULT_REQUEST_PAUSE, max_requests: int = DEFAULT_MAX_REQUESTS,
                 client: httpx.Client | None = None) -> None:
        self.source_db = Path(source_db)
        self.store = store
        self.request_pause = request_pause
        self.max_requests = max_requests
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "polymarket-tennis/0.1"})
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def capture_raw(self, cycle_id: str, method: str, endpoint: str, *,
                    params: dict[str, Any] | None = None, json_body: Any = None,
                    identity: dict[str, Any] | None = None,
                    expect_json: bool = False) -> httpx.Response | None:
        """Capture exact response bytes and clocks before any interpretation."""
        request_ts = time.time()
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            response = self.client.request(method, endpoint, params=params, json=json_body)
            received_ts = time.time()
            duration = time.monotonic() - started
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.store.record_observation(
                cycle_id=cycle_id, request_ts=request_ts, received_ts=None,
                duration_seconds=time.monotonic() - started, method=method, endpoint=endpoint,
                parameters={"query": params, "json": json_body}, identity=identity or {},
                status_code=None, headers=None, body=None, error=error,
            )
            if self.request_pause:
                time.sleep(self.request_pause)
            return None
        body = response.content if response is not None else None
        observation_id = self.store.record_observation(
            cycle_id=cycle_id, request_ts=request_ts, received_ts=received_ts,
            duration_seconds=duration, method=method, endpoint=endpoint,
            parameters={"query": params, "json": json_body}, identity=identity or {},
            status_code=response.status_code if response is not None else None,
            headers=dict(response.headers) if response is not None else None, body=body, error=None,
        )
        try:
            response.raise_for_status()
            if expect_json:
                response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.store.record_observation_error(observation_id, f"{type(exc).__name__}: {exc}")
            response = None
        if self.request_pause:
            time.sleep(self.request_pause)
        return response

    def _collect_condition(self, cycle_id: str, condition_id: str,
                           members: list[Identity]) -> int:
        requests = 0
        common = {"condition_id": condition_id, "fresh_cohort": members[0].fresh_cohort}
        for item in members:
            ident = {**common, "token_id": item.token_id, "token_index": item.token_index}
            self.capture_raw(cycle_id, "GET", f"{CLOB}/fee-rate",
                             params={"token_id": item.token_id}, identity=ident, expect_json=True)
            requests += 1
        self.capture_raw(cycle_id, "GET", f"{CLOB}/markets/{condition_id}",
                         identity=common, expect_json=True)
        self.capture_raw(cycle_id, "GET", f"{GAMMA}/markets",
                         params={"condition_ids": condition_id, "limit": 1},
                         identity=common, expect_json=True)
        requests += 2

        def rpc(method: str, params: list[Any]) -> Any:
            nonlocal requests
            request_id = str(uuid.uuid4())
            response = self.capture_raw(
                cycle_id, "POST", RPC_URL,
                json_body={"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params},
                identity={**common, "kind": "chain_payout_slots"},
            )
            requests += 1
            if response is None:
                return None
            try:
                reply = response.json()
            except ValueError:
                return None
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                return None
            return reply

        chain = capture_payout_slots(rpc, condition_id)
        log.info(
            "chain payout slots %s: %s (token mapping %s)",
            condition_id,
            chain["status"],
            chain["token_mapping"],
        )
        return requests

    def run_cycle(self) -> None:
        cycle_id = str(uuid.uuid4())
        started_epoch = time.time()
        started_mono = time.monotonic()
        self.store.conn.execute("INSERT INTO cycles(cycle_id, started_ts) VALUES (?, ?)", (cycle_id, started_epoch))
        requests = 0
        error: str | None = None
        identities: list[Identity] = []
        try:
            identities = self.store.refresh_identities(self.source_db)
            for url in OFFICIAL_DOCUMENTS:
                key = "official_doc_ts:" + hashlib.sha256(url.encode()).hexdigest()
                last_doc = float(self.store.get_progress(key))
                if started_epoch - last_doc >= DOC_INTERVAL:
                    response = self.capture_raw(
                        cycle_id, "GET", url, identity={"kind": "official_document"}
                    )
                    requests += 1
                    if response is not None:
                        self.store.set_progress(key, str(started_epoch))

            by_condition: dict[str, list[Identity]] = {}
            for item in identities:
                by_condition.setdefault(item.condition_id, []).append(item)
            conditions = sorted(by_condition)
            if conditions:
                cursor = int(self.store.get_progress("condition_cursor")) % len(conditions)
                ordered = conditions[cursor:] + conditions[:cursor]
                consumed = 0
                for condition_id in ordered:
                    members = by_condition[condition_id]
                    # Reserve the helper's worst case before starting a condition.
                    # A short unresolved/error path gives unused budget back naturally.
                    required = len(members) + 2 + MAX_RPC_REQUESTS
                    if requests + required > self.max_requests:
                        break
                    try:
                        requests += self._collect_condition(cycle_id, condition_id, members)
                    except Exception as exc:
                        # The condition owned its full reservation once started.
                        # This remains bounded even when failure happens after
                        # requests whose count the helper could not return.
                        requests += required
                        condition_error = f"{type(exc).__name__}: {exc}"
                        self.store.record_cycle_error(cycle_id, condition_id, condition_error)
                        error = error or condition_error
                        log.warning("evidence condition %s failed: %s", condition_id, condition_error)
                    finally:
                        consumed += 1
                        self.store.set_progress(
                            "condition_cursor", str((cursor + consumed) % len(conditions))
                        )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("evidence cycle failed: %s", error)
        finally:
            recorded_requests = int(self.store.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()[0])
            self.store.conn.execute(
                "UPDATE cycles SET finished_ts=?, duration_seconds=?, identity_count=?, request_count=?, error=? "
                "WHERE cycle_id=?",
                (time.time(), time.monotonic() - started_mono, len(identities),
                 recorded_requests, error, cycle_id),
            )

    def run(self, interval: float = DEFAULT_INTERVAL, once: bool = False) -> None:
        try:
            while True:
                started = time.monotonic()
                self.run_cycle()
                if once:
                    return
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        finally:
            self.close()
