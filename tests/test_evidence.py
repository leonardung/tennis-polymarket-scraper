from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from polymarket.evidence import EvidenceCollector, EvidenceStore, protocol_settings
from polymarket.evidence_chain import RPC_URL


def source_db(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE markets (condition_id TEXT, token_0 TEXT, token_1 TEXT, "
            "match_date TEXT, tour TEXT, market_type TEXT)"
        )
        conn.executemany(
            "INSERT INTO markets VALUES (?, ?, ?, ?, 'atp', 'moneyline')", rows
        )


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.db"
        self.output = self.root / "evidence.db"
        self.settings = {"interval": 300.0, "timeout": 1.0}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_start_restart_and_fresh_identity_retention(self) -> None:
        source_db(
            self.source,
            [("old", "o0", "o1", "2000-01-01"), ("fresh", "f0", "f1", "2999-01-01")],
        )
        before = self.source.read_bytes()
        with EvidenceStore(self.output, self.source, self.settings) as store:
            started = store.conn.execute("SELECT started_ts FROM protocol").fetchone()[0]
            identities = store.refresh_identities(self.source)
            self.assertEqual({i.condition_id for i in identities}, {"fresh"})
        self.assertEqual(self.source.read_bytes(), before)
        with sqlite3.connect(self.source) as conn:
            conn.execute("DELETE FROM markets WHERE condition_id = 'fresh'")
        with EvidenceStore(self.output, self.source, self.settings) as store:
            self.assertEqual(store.conn.execute("SELECT started_ts FROM protocol").fetchone()[0], started)
            self.assertEqual({i.condition_id for i in store.refresh_identities(self.source)}, {"fresh"})
        with self.assertRaisesRegex(ValueError, "differ"):
            EvidenceStore(self.output, self.source, {"interval": 60.0})

    def test_output_cannot_alias_source(self) -> None:
        source_db(self.source, [])
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "must differ"):
            EvidenceStore(self.source, self.source, self.settings)
        self.assertEqual(self.source.read_bytes(), before)
        alias = self.root / "source-hardlink.db"
        os.link(self.source, alias)
        with self.assertRaisesRegex(ValueError, "must differ"):
            EvidenceStore(alias, self.source, self.settings)
        self.assertEqual(self.source.read_bytes(), before)

    def test_invalid_protocol_settings_fail_before_creating_an_artifact(self) -> None:
        invalid = (
            {"interval": 0, "timeout": 1, "request_pause": 0, "max_requests": 9},
            {"interval": float("inf"), "timeout": 1, "request_pause": 0,
             "max_requests": 9},
            {"interval": 1, "timeout": float("nan"), "request_pause": 0,
             "max_requests": 9},
            {"interval": 1, "timeout": 1, "request_pause": -1, "max_requests": 9},
            {"interval": 1, "timeout": 1, "request_pause": 0, "max_requests": 8},
            {"interval": 1, "timeout": 1, "request_pause": 0, "max_requests": True},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                protocol_settings(**values)
        self.assertFalse(self.output.exists())

    def test_raw_hash_headers_clocks_errors_and_continued_polling(self) -> None:
        source_db(self.source, [])
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/timeout":
                raise httpx.ReadTimeout("late", request=request)
            if request.url.path == "/http-error":
                return httpx.Response(503, content=b"unavailable", headers={"retry-after": "1"})
            if request.url.path == "/bad":
                return httpx.Response(200, content=b"not-json", headers={"x-proof": "kept"})
            return httpx.Response(200, content=b'{"ok":true}', headers={"x-proof": "kept"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with EvidenceStore(self.output, self.source, self.settings) as store:
            collector = EvidenceCollector(self.source, store, client=client, request_pause=0)
            self.assertIsNone(collector.capture_raw("cycle", "GET", "https://example.test/timeout"))
            self.assertIsNone(collector.capture_raw("cycle", "GET", "https://example.test/http-error"))
            self.assertIsNone(collector.capture_raw("cycle", "GET", "https://example.test/bad", expect_json=True))
            response = collector.capture_raw(
                "cycle", "POST", "https://example.test/good", json_body={"rpc": "call"}, expect_json=True
            )
            self.assertEqual(response.status_code, 200)
            rows = store.conn.execute(
                "SELECT request_ts, received_ts, duration_seconds, response_body, response_sha256, "
                "response_headers_json, COALESCE(o.error, e.error) FROM observations o "
                "LEFT JOIN observation_errors e USING (observation_id) ORDER BY request_ts"
            ).fetchall()
        client.close()
        self.assertEqual(calls, ["/timeout", "/http-error", "/bad", "/good"])
        self.assertIn("ReadTimeout", rows[0][6])
        self.assertIsNone(rows[0][1])
        self.assertIn("HTTPStatusError", rows[1][6])
        self.assertIsNotNone(rows[1][1])
        self.assertEqual(rows[1][3], b"unavailable")
        self.assertIn("JSONDecodeError", rows[2][6])
        self.assertEqual(rows[2][3], b"not-json")
        self.assertEqual(rows[2][4], hashlib.sha256(b"not-json").hexdigest())
        self.assertIn("x-proof", rows[2][5])
        self.assertGreaterEqual(rows[2][1], rows[2][0])
        self.assertGreaterEqual(rows[2][2], 0)

    def test_stored_body_is_httpx_decoded_entity_and_headers_keep_content_encoding(self) -> None:
        source_db(self.source, [])
        entity = b'{"feeSchedule":{"rate":0.05}}'
        compressed = gzip.compress(entity)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=compressed, headers={"content-encoding": "gzip"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with EvidenceStore(self.output, self.source, self.settings) as store:
            collector = EvidenceCollector(self.source, store, client=client, request_pause=0)
            collector.capture_raw("cycle", "GET", "https://example.test/gzip", expect_json=True)
            body, digest, headers = store.conn.execute(
                "SELECT response_body, response_sha256, response_headers_json FROM observations"
            ).fetchone()
        client.close()
        self.assertEqual(body, entity)
        self.assertEqual(digest, hashlib.sha256(entity).hexdigest())
        self.assertEqual(json.loads(headers)["content-encoding"], "gzip")

    def test_cycle_records_source_read_failure_and_keeps_prior_observations(self) -> None:
        source_db(self.source, [])
        with EvidenceStore(self.output, self.source, self.settings) as store:
            store.record_observation(
                cycle_id="prior", request_ts=1, received_ts=2, duration_seconds=1,
                method="GET", endpoint="saved", parameters=None, identity={}, status_code=200,
                headers={}, body=b"raw", error=None,
            )
            self.source.unlink()
            collector = EvidenceCollector(self.source, store, request_pause=0)
            collector.run_cycle()
            collector.close()
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
            error = store.conn.execute("SELECT error FROM cycles ORDER BY started_ts DESC LIMIT 1").fetchone()[0]
            self.assertIn("OperationalError", error)

    def test_cycle_captures_unresolved_chain_slots_after_raw_rpc_persistence(self) -> None:
        condition = "0x" + "12" * 32
        source_db(self.source, [(condition, "t0", "t1", "2999-01-01")])
        rpc_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == RPC_URL:
                payload = __import__("json").loads(request.content)
                rpc_ids.append(payload["id"])
                results = {
                    "eth_chainId": "0x89",
                    "eth_getBlockByNumber": {"number": "0x123", "hash": "0x" + "ab" * 32},
                    "eth_call": "0x" + "0" * 64,
                }
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                                 "result": results[payload["method"]]})
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with EvidenceStore(self.output, self.source, self.settings) as store:
            collector = EvidenceCollector(
                self.source, store, client=client, request_pause=0, max_requests=20
            )
            collector.run_cycle()
            chain_rows = store.conn.execute(
                "SELECT response_body FROM observations WHERE identity_json LIKE '%chain_payout_slots%'"
            ).fetchall()
            self.assertEqual(len(chain_rows), 3)
            self.assertEqual(len(set(rpc_ids)), 3)
            self.assertEqual(store.conn.execute(
                "SELECT request_count FROM cycles ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()[0], 10)
        client.close()

    def test_condition_failure_is_durable_and_does_not_starve_next_condition(self) -> None:
        first = "0x" + "11" * 32
        second = "0x" + "22" * 32
        source_db(self.source, [
            (first, "a0", "a1", "2999-01-01"),
            (second, "b0", "b1", "2999-01-01"),
        ])

        class FailingCollector(EvidenceCollector):
            first_calls = 0

            def capture_raw(self, cycle_id, method, endpoint, **kwargs):
                if kwargs.get("identity", {}).get("condition_id") == first:
                    self.first_calls += 1
                    if self.first_calls == 3:
                        raise RuntimeError("bad condition fixture")
                return super().capture_raw(cycle_id, method, endpoint, **kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == RPC_URL:
                payload = json.loads(request.content)
                values = {
                    "eth_chainId": "0x89",
                    "eth_getBlockByNumber": {"number": "0x1", "hash": "0x" + "ab" * 32},
                    "eth_call": "0x" + "0" * 64,
                }
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                                 "result": values[payload["method"]]})
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with EvidenceStore(self.output, self.source, self.settings) as store:
            collector = FailingCollector(
                self.source, store, client=client, request_pause=0, max_requests=21
            )
            collector.run_cycle()
            self.assertEqual(store.conn.execute(
                "SELECT condition_id FROM cycle_errors"
            ).fetchone()[0], first)
            self.assertIn("bad condition fixture", store.conn.execute(
                "SELECT error FROM cycles ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()[0])
            self.assertGreater(store.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE identity_json LIKE ?",
                (f'%"condition_id": "{second}"%',),
            ).fetchone()[0], 0)
            self.assertEqual(store.get_progress("condition_cursor"), "0")
            request_count = store.conn.execute(
                "SELECT request_count FROM cycles ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()[0]
            self.assertLessEqual(request_count, 21)
            self.assertEqual(request_count, store.conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0])
        client.close()


if __name__ == "__main__":
    unittest.main()
