# Frozen Runtime Parity

- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`
- Freeze artifact blob SHA: `8d78533ef4601439cb9d14bb0b4628b596cf4fd0`
- Effective strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Effective runtime config SHA: `a4758ab22db600504a48c4e88ca8c2952db61573bebe7fc03cbd962d3a5dfaf6`
- Real `TradingController` parity: PASS

The compiler maps the frozen combination `31111` directly:

- A3 range: multiplier `2.0`
- B1 grid: `5` to `10` grids, normal step `1.5x`
- C1 profit: disabled, `take_profit_usdt = 0`
- D1 inventory: caution `0.4`, reduce-only `0.8`
- E1 stop: baseline, no additional ATR buffer
- SOXL capital multiplier: `0.5`
- Economic leverage: `1x`

SKHYNIXUSDT remains `RESEARCH_ONLY` and is not promoted to the controller live allowlist.
