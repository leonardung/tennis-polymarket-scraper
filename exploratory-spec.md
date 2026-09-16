---
title: Start exploratory evidence collection
type: feature
created: '2026-09-13'
status: done
baseline_commit: 9dc581f9107c06cb5f0f3977e6e53583307a8018
review_loop_iteration: 0
context: [agents/project-context.md, agents/architecture.md]
---

<frozen-after-approval reason="User authorized remote collection and exploratory protocol in conversation">

## Intent

Start ongoing exploratory timing, fee, and payout evidence acquisition on SSH host fractal at /home/leo/projects/polymarket. Existing recorder already captures measured receipt times and feed polls. Missing historical receipts cannot be reconstructed. New protocol uses observed receipt availability and explicitly unknown source latency, without claiming strict final profitability qualification.

## Boundaries & Constraints

Always preserve ongoing book recording. Run an independent evidence service reading tennis.db read-only and writing its own SQLite evidence database. Retain raw HTTP responses with request/receipt epoch seconds, monotonic elapsed duration, status, endpoint/parameters, identity, hash and errors. Never overwrite evidence. Persist protocol start and settings once, retaining them across restart. Fresh cohort means matches scheduled on or after protocol start UTC date; already underway or observed outcomes remain exploratory only. Preserve unresolved members across restarts and catalog retirement. Reuse repository httpx/resolver/config and CLI patterns. Only public unauthenticated requests. No trading, spending, predictive model changes, strict replay gate changes, hand-edited data or fabricated timestamps. Existing source capture stays authoritative for books/scores/timing.

Ask first only for paid services or scope beyond authorized remote deployment. User authorization overrides skill intermediate approvals and remote-operation prohibition. Do not commit or push.

## I/O & Edge-Case Matrix

| Scenario | Expected behavior |
|---|---|
| Fresh startup | Persist immutable exploratory protocol marker and discover eligible condition/token identities from read-only capture. |
| Restart | Preserve original protocol start and cohort; resume collection without erasing observations. |
| Valid API reply | Store exact response bytes, SHA256 and measured clocks before interpretation. |
| Timeout, HTTP error, invalid JSON | Preserve available raw/error evidence, continue other requests; never fabricate success. |
| Unresolved/closed market | Continue collecting both tokens and resolution metadata after sports match ends; closed prices alone do not become payouts. |
| Quiet catalog or read failure | Record health/cycle error, bounded retry; never alter source DB or crash existing recorder. |
| Missing fee terms | Preserve raw base_fee, explicitly leave full formula and effective interval unqualified. |

</frozen-after-approval>

## Code Map

- Target checkout: /tmp/polymarket-exploratory-20260913, cloned from remote baseline. Parent deploys verified files later.
- src/polymarket/api.py: httpx Polymarket client, CLOB/GAMMA constants. Public GET /fee-rate?token_id= returns base_fee only; GET /markets/{condition_id} supplies tokens and winner flags. Gamma GET /markets?condition_ids=...&limit=1 corroborates metadata but outcomePrices are not payouts.
- src/polymarket/resolver.py: existing DNS workaround required for API access; inspect CLI main initialization and reuse.
- src/polymarket/telemetry.py: established epoch request/response timestamps and monotonic durations; raw body storage requires separate owner.
- src/polymarket/store.py: source tables markets(condition_id,token_0,token_1,match_date,state,...) and feed_polls; source is never mutated by sidecar.
- src/polymarket/cli.py: argparse dispatch owner; add evidence subcommand here.
- docker-compose.yml/Dockerfile: existing capture/dashboard share image; add separate service/image tag in an override compose file. Build/start only evidence, no capture recreation.
- tests/test_offline.py: native assertion script. New focused stdlib unittest file acceptable, executed with uv run.

## Tasks & Acceptance

**Execution:**
- [x] Add src/polymarket/evidence.py for durable protocol, cohort, raw observation storage and bounded sequential polling. Keep implementation small; no general plugin framework. Target 5-minute sweeps, short request timeout, rate-limit sequential calls, persist progress fairly so failures cannot starve other markets. Cohort includes all eligible source market types and both tours, with identities retained. Track actual cycle duration rather than promising exact cadence.
- [x] Add CLI evidence command with --db source, --output evidence DB, --once smoke option, explicit interval and operational logging to stderr.
- [x] Add docker-compose.exploratory.yml service using separate image tag, source mount read-only and evidence output mount writable; restart unless stopped. No host ports or secrets. Parent handles build and deployment.
- [x] Add docs/exploratory-protocol.md explaining receipt-order timing, unknown upstream delay, no strict under-10-second claim, fee/payout uncertainties, cohort, start marker, paths, health query and stop/restart commands. Protocol is collection/research only; strategy selection and performance tests are not run here.
- [x] Add focused failing-first offline tests covering matrix, source preservation, continued polling, restart, errors and hashes. Run existing offline regression script and relevant lint.

**Acceptance Criteria:**
- Given receipt-aware source recorder, when evidence service starts, source recorder retains same process identity and continues writing while new raw fee and resolution observations persist.
- Given a restart, when service resumes, protocol start stays unchanged and prior evidence remains intact.
- Given incomplete authoritative economics, when raw observations arrive, neither fee-rate basis nor quoted prices are mislabeled as complete economic qualification.

## Design Notes

Store raw evidence separately rather than modifying recorder hot path. Existing feed_polls supplies timing evidence; record protocol boundary that identifies corresponding source range. CLOB winner flags and Gamma resolution metadata are observations, not exact on-chain payout vectors. Additional verified official fee document/chain sources may be added by parent during integration; do not invent their semantics. Do not emit audit-compatible fee_schedule/resolutions with invented fields.

## Verification

- uv run python -m unittest discover -s tests -p test_evidence.py
- uv run python tests/test_offline.py
- uv run ruff check src/polymarket/evidence.py tests/test_evidence.py src/polymarket/cli.py (if available; do not change dependencies solely for lint)
- Parent deployment: source process unchanged, live raw receipts, immutable protocol restart proof, healthy service and source recorder.

## Verified acquisition sources

Capture official https://docs.polymarket.com/trading/fees.md,
https://docs.polymarket.com/market-data/market-details.md and
https://docs.polymarket.com/resources/contracts.md as raw versioned observations.
Gamma includes feeSchedule (rate/exponent/takerOnly/rebateRate); do not assume
base_fee uses the same scale. No historical effective-from is established.

Add small evidence_chain.py helper and focused tests for raw Polygon payout
acquisition. Public RPC https://polygon-bor-rpc.publicnode.com supports chain ID
0x89 and finalized block headers. Official CTF contract is
0x4D97DCd97eC945f40cF65F87097ACe5EA0476045. Getter selectors are 0xdd34de67 for
payoutDenominator(bytes32) and 0x0504c814 for payoutNumerators(bytes32,uint256),
corroborated by official Polymarket/conditional-tokens-contracts Solidity source.
Fetch chain ID, finalized block header, then denominator and two binary slot
numerators using that exact block number. Persist every raw RPC request/reply
through collector. Reject wrong chain, malformed header/result, RPC error and
invalid condition ID; denominator zero means unresolved. Query each cycle for
tracked binary conditions, with existing rate limiting. Do not emit normalized
token payouts: slot-to-token position mapping and neg-risk collateral semantics
remain unverified. Raw finalized slot vector is chain evidence only. Test block
pinning, wrong chain, RPC errors, malformed IDs/results, unresolved and resolved
vectors. No keys, wallets, paid RPC, historical cash-availability claims or new
dependencies.

## Review clarifications

HTTP response bodies mean HTTPX-decoded entity bytes, before application
normalization; SHA256 hashes those exact stored bytes. Original transport headers
are retained separately. Compressed wire bytes are not archived. This is the
same representation used in the first live cycle and all subsequent cycles.
Review additionally requires finite valid operational settings before protocol
creation, Polygon RPC DNS fallback, and durable per-condition cursor advancement
on unexpected failures. The separate Compose project is named
polymarket-exploratory to isolate lifecycle commands from the recorder project.

## Deployment verification

Continuous service is running on fractal. At 2026-09-13T11:46:49Z, 231 responses
across two completed cycles covered all 23 admitted conditions. All entity-body
hashes verified. Protocol start 1789299506.8244069 and all 117 first-cycle records
survived process restart from smoke to service. Main capture container ID and
zero restart count remain unchanged; new source book receipts continue.
Machine-generated evidence-data/deployment-proof-20260913T114649488213Z.json
records image and code hashes. Tests: 16 focused, 589 recorder checks, Ruff clean.
No commit or push. Token mapping and strict timing qualification remain outside
this exploratory acquisition protocol.

## Suggested Review Order

- Protocol settings and source preservation:
  [evidence.py](src/polymarket/evidence.py#L36).
- Exact finalized slot evidence, explicitly unmapped to tokens:
  [evidence_chain.py](src/polymarket/evidence_chain.py#L32).
- Independent deployment:
  [docker-compose.exploratory.yml](docker-compose.exploratory.yml#L1).
- Failure, restart and representation proof:
  [test_evidence.py](tests/test_evidence.py#L1).
