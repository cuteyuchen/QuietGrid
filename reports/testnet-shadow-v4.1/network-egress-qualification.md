# QuietGrid v4.1 Network Egress Qualification

Generated: 2026-08-28T23:17:00+08:00

## Identity

- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- HEAD: `aa7fdc94be23c3bb246cafd71ab1a311d2611643`
- Worktree: clean
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`

## Local Binance Client Scan

- Python processes: none.
- Node processes: Codex, CodeGraph, MCP servers, WebStorm, Playwright, and local dev servers only. No Binance, CCXT, or trading bot command lines found.
- Docker: not available on this host (`DOCKER_UNAVAILABLE`).
- Services: no Binance, CCXT, QuietGrid, or trading service found. Sangfor SSL VPN services exist, but their virtual adapters are disconnected.
- Scheduled tasks: no Binance, CCXT, QuietGrid, or trading task found. Only the built-in Windows TPM health check matched the keyword filter.
- Startup entries: `Clash for Windows` is present in HKCU Run; Clash Verge is running.
- Old QuietGrid runtime: no process, service, or scheduled task references an old QuietGrid runtime. The only QuietGrid path match is CodeGraph serving `E:\project\QuietGrid`, which is this repository.

## Proxy Environment

- Process environment variables `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`: not set.
- Windows system proxy: enabled, `127.0.0.1:7897`.
- WinHTTP proxy: direct access (not configured), but this is not authoritative because TUN traffic capture is active.
- Python `urllib.request.getproxies()`: `127.0.0.1:7897`.
- Python `httpx` environment proxies: `127.0.0.1:7897`.
- Proxy client: `clash-verge.exe` and `verge-mihomo.exe` running; `verge-mihomo` listens on `127.0.0.1:7897`.
- TUN adapter: `Meta Tunnel` is up with default routes `0.0.0.0/0` and `::/0` via `198.18.0.2`. OS-level traffic is captured by Mihomo regardless of environment variables.
- Credential values are not recorded in this report.

## Egress Stability

One non-Binance IP echo service (`ipinfo.io/json`) was used. Three samples were collected over about four minutes.

| Sample | Timestamp (Asia/Shanghai) | Egress IP | Provider |
| --- | --- | --- | --- |
| sample1 direct TUN | 2026-08-28T23:11:28 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |
| sample1 via 127.0.0.1:7897 | 2026-08-28T23:11:30 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |
| sample2 direct TUN | 2026-08-28T23:13:31 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |
| sample3 direct TUN | 2026-08-28T23:15:32 | 141.148.210.128 | AS31898 Oracle Corporation, Mumbai, IN |

The IP was stable during the observation window, but the exit is provided by a local Clash/Mihomo proxy client with an Oracle cloud IP. This cannot be proven to be a dedicated, controlled, non-shared egress.

## Egress Classification

- Egress stability: stable for the 4-minute window, not sufficient to prove dedicated use.
- Egress classification: `SHARED_OR_UNKNOWN_EGRESS`.
- Proxy/TUN active: yes.
- Dedicated/controllable egress: no.

## Cooldown Evidence

`production-public-cooldown.json` is preserved. It records HTTP 418 at stage `SNDKUSDT:ticker`, `retry_after_seconds=14732`, `cooldown_until=2026-08-28T13:47:57Z`, and `x-mbx-used-weight-1m=0`. The cooldown has expired by wall clock, but a new probe must not run while the egress remains shared/unknown.

## Safety

- Binance REST or WebSocket requests made in this task: 0.
- No proxy rotation, node switching, or ban evasion attempted.
- No credentials, API keys, tokens, or cookies recorded.
- No code modified.

## Conclusion

`VALIDATION_ENVIRONMENT_UNSUITABLE_SHARED_EGRESS`

The current environment routes all traffic through a shared Clash/Mihomo proxy node and an active TUN adapter. It does not satisfy the dedicated/stable egress requirement for formal Binance Production Public Network Validation. Do not run another probe from this egress.
