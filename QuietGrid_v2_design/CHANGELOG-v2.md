# v2.0 设计变更摘要

相对原始 QuietGrid 计划，主要变化：

1. 将 ATR/观察期判断升级为独立 Regime Engine；
2. 新增 Inventory Manager 与未配对库存 lot；
3. 网格数量改为由区间与成本地板反推；
4. 引入风险预算、连续亏损、周末损失和冷却恢复门控；
5. 将原始 10 倍参数降为历史配置，v2 示例默认 1 倍有效杠杆；
6. 回测增加未来函数防护、保守 Maker 成交、Walk-Forward 和 Monte Carlo；
7. 数据库增加事件、特征、Regime、GridPlan、Inventory、Risk 和参数版本；
8. API 写操作改为命令队列；
9. 前端增加 Market、Inventory、Risk、Backtest、Replay 和 Operations 页面；
10. 采用 feature flag 分阶段迁移，不进行一次性重写。
