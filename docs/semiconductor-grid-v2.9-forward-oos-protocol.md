# QuietGrid Semiconductor Grid v2.9 Forward OOS Protocol

状态：`FROZEN_CANDIDATE_FORWARD_OOS`

v2.9 只验证 v2.8 冻结结果，不执行新的参数搜索。正式主候选是
`31111-NEUTRAL`（A3 + B1 + C1 + D1 + E1）。`31121-NEUTRAL` 仅作为
`DIAGNOSTIC_CONTROL_ONLY`，不计为第二个主候选；`31111-NEUTRAL-EX-MU`
如注册，只能从新的冻结点开始，历史验证为 `NOT_CLAIMED`。

## 暴露边界

```text
EXPOSURE_CUTOFF = max(
  latest_timestamp_present_in_any_research_input,
  latest_window_seen_by_any_phase,
  latest_window_seen_by_regime_or_gate,
  candidate_freeze_time_utc,
)
```

Forward OOS 只接受完整休市窗口，并要求窗口开始时间严格晚于 cutoff。窗口
已经开始、只有部分数据、被 Regime/Gate 看过、或 funding/rules/force-close
不完整时，状态都是 `INCOMPLETE_WINDOW` 或 `EXPOSED_HISTORY`，不能进入分母。

## 固定执行矩阵

每个合法窗口固定运行以下三个场景和六个 seed：

```text
PRIMARY_ZERO_MAKER  EXECUTION_STRESS  MAKER_PROMO_OFF
3 10 17 31 59 97
```

同一 `candidate + window + scenario + seed` 必须可复现。候选哈希改变会追加
`SEQUENCE_INVALIDATED`，旧 OOS 序列不再计入任何统计。

## 验收

0--3 个完整窗口：`INSUFFICIENT_FORWARD_OOS`；4--7 个：
`FORWARD_OOS_ACCUMULATING`；至少 8 个后才评估收益、风险、库存、集中度和
执行压力门槛。窗口数按唯一 canonical `market_calendar:start:end` 计数，
不能用 symbol、seed 或 scenario 的行数替代。

运行器：

```bash
python -m scripts.semiconductor_grid_forward_oos_v29 \
  --freeze-time-utc <UTC timestamp>
```

运行器只读取冻结 v2.8 输入，不修改生产配置；`startup_auto_entry`、测试网
强制窗口、快速观察均保持关闭，经济杠杆保持 1x。

冻结后新增完整窗口只能走追加入口：

```bash
python -m scripts.semiconductor_grid_forward_oos_v29_append \
  --run-time-utc <UTC timestamp>
```

追加入口从 `candidate-31111-freeze.json`、`config-freeze.json` 和冻结交易规则
重建运行上下文，校验候选/配置/代码哈希后才向 CSV/JSON ledger 追加新的
`candidate + window + scenario + seed` 行。普通哈希变化会写入
`SEQUENCE_INVALIDATED`；只有显式的新 `CANDIDATE_FREEZE` 才能开启新序列。
EX-MU 使用独立候选哈希和独立统计，不会改变主候选序列。
