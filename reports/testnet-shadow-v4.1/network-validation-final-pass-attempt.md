# QuietGrid v4.1 Production Public Network Validation Attempt

Generated: 2026-08-29T01:05:30+08:00

## Result

`BLOCKED_V41_NETWORK_VALIDATION`

Stopped at Stage 1 (Network Egress Qualification). No Binance endpoint was accessed in this task.

## Preflight

- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- HEAD: `edf708d58f90bcd69c64c41344b9e53b3ef2c20e`
- Worktree: clean
- Remote branches: `master`, `codex/semiconductor-grid-forward-oos-monitor-v2.9.1`, `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`
- Freeze integrity: PASS
- Frozen runtime parity: PASS
- Compileall: PASS
- Pytest: `868 passed`
- `git diff --check`: PASS

## Egress Qualification

- Egress result: `VALIDATION_ENVIRONMENT_UNSUITABLE_SHARED_EGRESS`
- Egress classification: `SHARED_OR_UNKNOWN_EGRESS`
- Stability: `STABLE_EGRESS` for the observation window only (3/3 same IP)
- Proxy: Clash Verge / Mihomo, system proxy `127.0.0.1:7897`
- TUN: `Meta Tunnel` up with default routes `0.0.0.0/0` and `::/0`
- Python `urllib` proxy: `127.0.0.1:7897`
- Python `httpx` proxy: `127.0.0.1:7897`
- Other Binance consumers: none found (processes, services, scheduled tasks, Docker unavailable)
- Cooldown evidence: preserved, HTTP 418, `retry_after_seconds=14732`, `cooldown_until=2026-08-28T13:47:57Z`

### Egress Samples

| Sample | Timestamp (Asia/Shanghai) | IP | Provider |
| --- | --- | --- | --- |
| sample1 | 2026-08-29T01:02:46 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |
| sample2 | 2026-08-29T01:03:32 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |
| sample3 | 2026-08-29T01:04:19 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |

The exit is provided by a local Clash/Mihomo proxy client and cannot be proven dedicated, non-shared, or fully controlled. The formal environment requirement is not satisfied.

## Next Stages

Production Public Probe, baseline smoke, conservative smoke, WebSocket recovery, process restart, and bounded soak were not started because the egress gate did not pass.

## Boundaries

- Production Private API calls: 0
- Real orders: 0
- Binance requests made in this task: 0
- `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- `NOT_EVALUATED_FOR_PROFITABILITY`
