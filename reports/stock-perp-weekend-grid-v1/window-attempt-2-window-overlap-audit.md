# W/O/R 窗口重叠审计

- observation_rows：`180`
- force_close_minutes：`120`
- 随机种子：`3, 10, 17, 31, 59, 97`
- W/O/R 总窗口：`2058`
- READY：`490`；SKIPPED：`1568`
- 重叠：`0`；通过：`True`

W 由 NyseWindowSlicer + Scheduler 的 NYSE 会话边界生成；O 使用同一 Scheduler 的普通工作日隔夜边界；R 在同标的/月份/阶段/持续时间/UTC 小时约束下固定抽样。
