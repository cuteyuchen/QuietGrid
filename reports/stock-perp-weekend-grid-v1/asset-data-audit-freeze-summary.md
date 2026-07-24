# Stock Perpetual 数据冻结摘要

- 协议：`docs/codex-stock-perp-weekend-grid-backtest-v2.5.md`
- 边界：`FIRST_COMPLETE_UTC_DAY`
- `data_previously_viewed`：`true`
- Tier A-Core：`8`
- 数据审计：`PASS`
- W/O/R 重叠审计：`PASS`

每个标的的 1m、Funding、Mark、Premium 和 aggTrades 均保留本地 SHA-256；上线日的零散分钟被排除，数据从首个完整 UTC 日开始。裁剪过程只使用此前已完成官方 CHECKSUM 校验的本地文件，不插值、不新增市场数据。

正式 H1 结论：`STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED`。H1 失败后未运行 B0–B5、任何参数搜索、Validation 或 Short OOS。
