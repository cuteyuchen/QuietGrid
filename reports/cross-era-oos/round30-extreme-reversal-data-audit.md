# Round 30：数据复用审计

本轮不下载新数据，也不扩展授权月份。价格输入复用 Round 29 已冻结的 Binance USD-M 1h 官方 Kline 数据；funding 输入复用 Round 22 已冻结的 Binance USD-M funding 数据。Round 29 的官方 archive checksum、CSV SHA、字段审计和隔离月份约束必须在评估器启动时重新验证；Round 22 funding manifest/CSV SHA 与事件顺序也必须重新验证。

只计算 DEVELOPMENT、VALIDATION、POSTHISTORY 三个授权段。2023-07 至 2024-07 隔离区间、CURRENT Final OOS 以及既有 robustness/spot_robustness 结果不进入 Round 30 的信号、选参或收益统计。
