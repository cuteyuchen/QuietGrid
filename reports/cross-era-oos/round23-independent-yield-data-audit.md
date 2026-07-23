# Round 23：独立收益数据可得性审计

审计日期：2026-07-23

## 订单簿微观结构

- Binance USD-M `bookDepth` 官方日归档从 2023 年可见，单日约 0.43–0.52 MB；
- 字段仅为 `timestamp,percentage,depth,notional`，每约 30 秒记录 ±1% 至 ±5% 累计深度；
- 数据不包含 best bid、best ask、订单队列位置或逐笔成交，不能计算真实 Maker spread capture 或成交概率；
- `bookTicker` 包含所需 top-of-book，但单日 BTC 压缩文件约 110–128 MB、ETH 约 90 MB，且可见覆盖主要集中在 2023 年中至 2024 年初，无法形成跨年代、体量可控的严格研究集。

结论：不冻结订单簿数据，不用 `bookDepth` 伪装成 Maker 收益证据。

## 期权波动率 Carry

- Binance Options `EOHSummary` 官方归档仅发现 2023-05-18 至 2023-10-23；
- 该区间落入既有 CURRENT Final OOS，不能用于参数选择或新 family 开发；
- `BVOLIndex` 覆盖更长，但没有同期完整期权链、成交价格和可复制 payoff，单独使用指数不能回测可执行波动率 carry。

结论：当前不冻结期权数据，不用隐含波动指数替代真实期权 PnL。

## 永续基差收敛

- Binance USD-M `premiumIndexKlines` 月度官方归档在 BTCUSDT、ETHUSDT 的 2020-01、2023-06、2024-08、2026-06 均可访问；
- 单月压缩文件约 0.5–0.9 MB，含 1m `open/high/low/close` premium index，官方 `.CHECKSUM` 可验证；
- 数据可与 Round 22 已冻结的实际 funding rate 结合，在不下载 Spot/永续完整价格前先计算一个明显偏乐观的基差收敛经济上界；
- 授权月份继续固定为 `2020-01..2023-06` 与 `2024-08..2026-06`，明确排除 `2023-07..2024-07`。

结论：选择 premium-index basis convergence 作为下一独立 family；先冻结仅包含授权周末窗口的分钟 premium close，再另行预注册上界。
