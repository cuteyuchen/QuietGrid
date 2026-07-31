# Semiconductor Grid v2.8 Final Report

## Conclusion

`PASS_STABLE_PARAMETER_REGION_RESEARCH_ONLY`

- Phase 1 runs: 14016
- Phase 2 R1 runs: 5520
- Phase 3 local profiles: 32
- Stable neighbor profiles: 8
- EXPOSED_LATE passing profiles: 16
- Forward OOS: INSUFFICIENT_FORWARD_OOS (0/8 complete windows)

## Required Questions

1. 稳定盈利区域：有；2 个 EXPOSED_LATE 通过 profile 同时满足邻域稳定性与时间门槛。
2. 关键交互（PRIMARY_ZERO_MAKER 每个交互取绝对均值收益最大单元）：AxB(3,1) mean_total_pnl=7.51、AxE(2,5) mean_total_pnl=8.50、BxD(1,1) mean_total_pnl=4.88、CxD(1,1) mean_total_pnl=5.06
3. 宽区间与止损路径：A1/A2 假突破 0.031、真实趋势尾部 0.266；A3/A4 假突破 0.093、真实趋势尾部 0.051；该比较是路径诊断，不等同于因果证明。
4. 利润保护：按 C 等级的保护动作、止损、窗口末退出和库存实现损益为：C1: actions=0, stop=1306, window_close=126, inventory_pnl=-3723.89; C2: actions=0, stop=609, window_close=6, inventory_pnl=-1444.00; C3: actions=11655, stop=768, window_close=22, inventory_pnl=-1786.74; C4: actions=1386, stop=568, window_close=0, inventory_pnl=-1303.05。
5. 库存控制：按 D 等级的配对网格收益与库存拖累为：D1: paired=4900.64, inventory_drag=3893.92, ratio=0.795; D2: paired=1712.84, inventory_drag=1611.42, ratio=0.941; D3: paired=2820.42, inventory_drag=2404.30, ratio=0.852; D4: paired=25.53, inventory_drag=51.58, ratio=2.020。
6. 密集网格零 Maker：B3: net=-283.53, inventory_drag=955.59, ratio=1.328; B4: covering array 中无有效准入运行。
7. 标的差异：Phase 1 PRIMARY 按标的最高累计净收益 profile 为：MUUSDT: 31132-NEUTRAL (22.32); SKHYNIXUSDT: 11111-LONG (54.52); SNDKUSDT: 31111-NEUTRAL (71.78); SOXLUSDT: 31111-NEUTRAL (44.67)。
8. EXECUTION_STRESS 仍有效的 profile 数量：8；最高五个为：31111-NEUTRAL (26.61), 31121-NEUTRAL (26.61), 31211-NEUTRAL (20.09), 31221-NEUTRAL (20.09), 32111-NEUTRAL (16.20)。
9. Maker 依赖：2/2 个稳定 late profile 在 MAKER_PROMO_OFF 下非负；因此当前稳定区域不依赖 Maker 免费。
10. Forward OOS：INSUFFICIENT_FORWARD_OOS，当前 0/8 个完整窗口，不能做正式候选判断。

## Aggregate Stop Classification

- false-break ratio: 0.061
- true-break ratio: 0.163
