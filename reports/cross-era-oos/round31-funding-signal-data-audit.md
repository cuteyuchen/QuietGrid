# Round 31：数据复用审计

本轮不下载新数据，也不扩展授权月份。价格输入复用 Round 29 已冻结的 Binance USD-M 1h 官方 Kline 数据；funding 输入复用 Round 22 已冻结的 Binance USD-M funding 数据。评估器启动时重新校验两类 manifest、CSV SHA、官方 source archive 审计、事件顺序与隔离月份约束。

只计算 DEVELOPMENT、VALIDATION、POSTHISTORY 三个授权段。2023-07 至 2024-07 隔离区间、CURRENT Final OOS 以及既有 robustness/spot_robustness 结果不进入 Round 31 的信号、选参或收益统计。
