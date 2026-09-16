# Exploratory evidence protocol

This sidecar collects evidence needed to study timing, fees, and settlement without changing the live recorder. It opens `data/tennis.db` read-only and writes append-only HTTP observations to `evidence-data/exploratory-evidence.db`.

Request and receipt timestamps describe availability to this collector. They do not identify when an upstream system created the response, so they cannot establish a strict under-ten-second latency claim. Existing `feed_polls` and book timestamps remain authoritative for source capture timing.

The protocol start is written once. Markets whose `match_date` is on or after its UTC date are admitted to the fresh cohort. That date rule can include a match already live or ended when the service first starts, so cohort membership does not prove an untouched pre-match sample. Markets dated earlier are not admitted. Once admitted, both token identities are retained across restarts and catalog retirement, and resolution endpoints continue to be sampled after the sports match ends.

Every observation stores endpoint, parameters, identity, request epoch seconds, monotonic duration, HTTP status, response bytes, SHA256, and any error. `response_body` is the HTTP entity after HTTPX has decoded transport content encoding; its SHA256 covers those decoded entity bytes. Original response headers remain transport metadata, including `Content-Encoding`. Receipt epoch seconds exist only when an HTTP response arrived; a timeout cannot fabricate one. HTTP failures with a response and invalid JSON remain evidence rows. Cycles record duration and health independently, so one failed request does not stop later identities.

Official fee documentation currently states `fee = C * feeRate * p * (1-p)`, a Sports rate of `0.05`, and rounding to five decimals. The CLOB `fee-rate` endpoint has also returned a raw `base_fee` value. Their mapping and effective interval are unresolved, so this collector preserves both without normalization. Gamma `feeSchedule`, `feeType`, token winner flags, quoted `outcomePrices`, and other metadata are stored only inside the full raw response. Winner flags and quoted prices are observations, not exact payout vectors or final profitability qualification.

Settlement evidence reads Polygon chain 137 through a public RPC. It pins calls to a finalized block, then reads the Conditional Tokens Framework payout denominator and two indexed numerator slots. Those slots are captured without claiming which market token maps to which slot; the collector emits no authoritative token payout row. RPC replies and queries remain raw evidence.

Cycles target 300 seconds and make at most 120 requests. The cursor advances after every attempted condition and survives restart. As cohort size grows, one market's revisit interval can therefore exceed 300 seconds.

Run one smoke cycle:

```bash
uv run polymarket evidence --db data/tennis.db --output /tmp/exploratory-evidence-smoke.db --once
```

Start or restart only the sidecar:

```bash
mkdir -p evidence-data
docker compose -f docker-compose.exploratory.yml up -d --build evidence
docker compose -f docker-compose.exploratory.yml restart evidence
```

Stop it without touching capture:

```bash
docker compose -f docker-compose.exploratory.yml stop evidence
```

Health and recent errors:

```sql
SELECT started_ts, started_utc_date, source_db, settings_json FROM protocol;
SELECT cycle_id, started_ts, duration_seconds, identity_count, request_count, error
FROM cycles ORDER BY started_ts DESC LIMIT 20;
SELECT o.received_ts, o.endpoint, o.status_code, COALESCE(o.error, e.error) AS error
FROM observations o LEFT JOIN observation_errors e USING (observation_id)
WHERE o.error IS NOT NULL OR e.error IS NOT NULL
ORDER BY o.request_ts DESC LIMIT 20;
```

This protocol performs collection and research only. It does not choose strategies, execute performance tests, trade, or qualify profitability.
