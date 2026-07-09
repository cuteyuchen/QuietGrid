# 美股代币网格交易系统 · 需求规格文档

> **版本**：v1.0  
> **日期**：2026-07-03  
> **状态**：已确认，可进入开发阶段  
> **文档性质**：系统设计与需求规格，不构成投资建议

---

## 目录

1. [项目背景与设计思想](#1-项目背景与设计思想)
2. [核心策略逻辑](#2-核心策略逻辑)
3. [已确认参数总览](#3-已确认参数总览)
4. [功能需求详细说明](#4-功能需求详细说明)
   - 4.1 [时间窗口调度器（M1）](#41-时间窗口调度器m1)
   - 4.2 [交易所接入层（M2）](#42-交易所接入层m2)
   - 4.3 [选币模块（M3）](#43-选币模块m3)
   - 4.4 [观察期与动态区间计算（M4）](#44-观察期与动态区间计算m4)
   - 4.5 [网格执行引擎（M5）](#45-网格执行引擎m5)
   - 4.6 [击穿-冷静期状态机（M6）](#46-击穿-冷静期状态机m6)
   - 4.7 [风控模块（M7）](#47-风控模块m7)
   - 4.8 [数据持久化（M8）](#48-数据持久化m8)
   - 4.9 [Web 监控界面（M9）](#49-web-监控界面m9)
5. [技术架构](#5-技术架构)
   - 5.1 [整体架构图](#51-整体架构图)
   - 5.2 [进程模型](#52-进程模型)
   - 5.3 [目录结构](#53-目录结构)
   - 5.4 [技术栈选型](#54-技术栈选型)
6. [关键数据结构与接口设计](#6-关键数据结构与接口设计)
   - 6.1 [数据库表结构](#61-数据库表结构)
   - 6.2 [核心数据类](#62-核心数据类)
   - 6.3 [模块间接口](#63-模块间接口)
7. [状态机详细设计](#7-状态机详细设计)
8. [风控规则全集](#8-风控规则全集)
9. [部署与运维](#9-部署与运维)
10. [开发路线与 TODO](#10-开发路线与-todo)
11. [风险提示](#11-风险提示)

---

## 1. 项目背景与设计思想

### 1.1 背景

币安平台上的美股代币永续合约（TradFi Perpetual Futures）在美股真实市场休市期间（周末及节假日）仍可正常交易。由于缺乏真实市场的价格发现机制，休市时段的价格波动幅度极小，呈现出典型的"窄幅低波动震荡"特征。网格交易策略（Grid Trading）天然适配这种行情：在一个价格区间内均匀布置买卖挂单，反复吃到上下来回的小幅波动差价。

与此同时，币安针对 TradFi 永续合约推出了 Maker（挂单方）零手续费活动，使得密集网格在手续费层面具备了可行性——原本最大的成本阻力被大幅削减。

本系统正是基于上述两点利好而设计，同时以"开盘前强制离场"为最核心的风控手段，从根源上规避真实开盘时可能出现的跳空风险。

### 1.2 设计思想

#### 1.2.1 核心理念：利用信息不对称时间窗口

美股休市期间，市场处于"信息真空+低流动性"状态，价格围绕前一日收盘价做小幅均值回归式震荡。这一特征是网格策略的理想土壤：不需要预测方向，只需要价格在一个区间内来回振荡。

#### 1.2.2 三层防护体系

```
第一层（最高优先级）：时间窗口硬约束
  └─ 只在休市窗口内交易，临近开盘提前2小时强制清仓离场
     彻底消除跳空风险，是整个系统安全的根基

第二层（常规退出）：击穿-冷静期机制
  └─ 价格突破网格区间时，主动撤单平仓进入观望
     等待 ATR 回落确认趋稳后，重新建立网格
     应对休市期间的中枢漂移和异常波动

第三层（极端兜底）：动态止损
  └─ 止损线跟随网格下限动态设置（下限以下加缓冲）
     作为冷静期机制失灵时的最后防线
     防止程序异常或瞬间插针导致无法平仓
```

#### 1.2.3 自适应优先于写死参数

所有关键参数均追求"数据驱动、动态计算"：
- 网格区间和间距：观察期实测数据计算，不写死
- 止损线：跟随网格下限动态移动，不固定金额
- 标的选择：每窗口重新评估流动性，不写死某只标的
- 网格数量：反推自波动率，不手动指定

#### 1.2.4 零手续费的利用

所有网格挂单强制使用 `POST_ONLY`（只做 Maker）模式，利用币安 TradFi 永续合约 Maker 零手续费政策。每格的盈利安全垫仅需覆盖资金费 + 点差 + 少量滑点，大幅降低了盈亏平衡门槛。

#### 1.2.5 小而精，先跑通再扩展

v1.0 设计原则：单系统单策略，功能够用即可，不过度设计。Web 界面 v1 只做只读监控，通知模块列为 TODO，先把核心交易逻辑跑稳，再逐步完善周边。

---

## 2. 核心策略逻辑

### 2.1 策略整体流程

```
系统启动
    │
    ▼
[调度器] 持续监测时间窗口
    │
    ├─ 非休市窗口 ──► 等待，每5分钟检查一次
    │
    └─ 进入休市窗口
            │
            ▼
    [M3 选币] 每窗口开始前：
    按24h成交额+订单簿深度选出流动性前N的标的
    （剔除排除名单，最多选 max_concurrent 个）
            │
            ▼
    [M4 观察期] 空跑3小时，只采集1分钟K线，不交易
            │
            ▼
    [M4 区间计算] 用观察期数据动态算：
    ① 区间上下沿（均值 ± k×标准差 或分位数）
    ② ATR 当前基准值（后续用于冷静期判断）
    ③ 等比网格间距（波动率反推，覆盖成本安全垫）
    ④ 网格数量（间距反推上限内取值）
            │
            ▼
    [M5 网格引擎] 多标的并发，每个标的独立：
    ① 在区间内挂满等比限价单（POST_ONLY）
    ② WebSocket 监听成交事件
    ③ 成交后自动补挂对侧单（维持网格密度）
    ④ 实时统计每格收益
            │
            ├─ [M7] 触发止盈（盈利≥+10 USDT）──► 平仓退出
            │
            ├─ [M6] 触发击穿（价格超出区间边界）──► 冷静期
            │         │
            │         ├─ 撤掉所有挂单
            │         ├─ 平掉该标的持仓
            │         ├─ 持续监测 ATR
            │         └─ ATR回落+横盘达标 ──► 重新回到观察期
            │
            ├─ [M7] 触发动态止损（价格跌破 止损线）──► 强制平仓
            │
            └─ [M1] 触发临近开盘（盘前2h前）──► 全局强制清仓
                        ▼
                所有标的撤单+平仓，本窗口结束
                等待下一个休市窗口
```

### 2.2 网格示意图（等比）

```
价格
 ▲
 │   ─── 区间上沿（止盈线 / 冷静期上触发线）
 │        卖单 @  P × r^n
 │        卖单 @  P × r^3
 │        卖单 @  P × r^2
 │        卖单 @  P × r^1
 │   ─── 基准价 P（区间中枢）
 │        买单 @  P / r^1
 │        买单 @  P / r^2
 │        买单 @  P / r^3
 │        买单 @  P / r^n
 │   ─── 区间下沿（冷静期下触发线）
 │   ─── 动态止损线（下沿 × (1 - 缓冲%)）
 └──────────────────────────────── 时间
```

其中 `r = 1 + step_pct`，`step_pct` 由观察期波动率动态计算。

### 2.3 冷静期与止损的递进关系

```
价格下行方向
──────────────────────────────────────────────────────
网格区间内     │ 正常运行网格
──────────────┼───────────────────────────────────────
跌破区间下沿  │ ① 触发冷静期
              │    撤单 + 平仓 + 等待ATR回落
              │    回落后 → 重启网格
──────────────┼───────────────────────────────────────
继续下跌，    │ ② 触发动态止损（兜底防线）
跌破止损线    │    强制市价平仓，本轮彻底退出
              │    （应对冷静期来不及平仓 / 插针等极端情况）
──────────────────────────────────────────────────────
```

---

## 3. 已确认参数总览

### 3.1 核心交易参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 杠杆倍数 | 10x | 币安 USDS-M 合约，逐仓模式 |
| 单标的本金 | 200 USDT | 杠杆后名义仓位 2000 USDT |
| 最大并发标的数 | 3~5 个（建议先用3） | 配置项 `max_concurrent` |
| 总资金上限 | 1000 USDT | = 200 × 5，全局风控硬限制 |
| 止盈阈值 | +10 USDT（单标的） | 已实现盈利到达即平仓 |
| 止损方式 | 动态：跟随网格下限 | 止损价 = 下沿 × (1 - `stop_buffer_pct`) |
| 止损缓冲比例 | 默认 1~2%（可配置） | `stop_buffer_pct` 参数 |
| 挂单模式 | POST_ONLY（只做Maker） | 确保享受零手续费 |
| 合约类型 | USDS-M 永续合约 | 美股代币（如 AAPLUSDT 等） |

### 3.2 策略时序参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 观察期时长 | 3 小时 | 窗口开始后静默采集，不交易 |
| K 线周期 | 1 分钟 | 观察期和区间计算用 |
| 提前离场时间 | 盘前（pre-market）开始前 2 小时 | 提前离场，不等正式开盘 |
| 轮询间隔 | 10 秒 | 主循环检查周期 |
| 调度检查间隔 | 5 分钟 | 非窗口期等待时的检查频率 |

### 3.3 区间与网格计算参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 区间算法 | 均值 ± k×标准差 | k 默认 1.8，可调；备选分位数法(5%~95%) |
| 最小样本量 | 30 根1分钟K线 | 不足则延长观察或跳过 |
| 等比间距 | 动态，由 ATR 反推 | 确保每格价差 > 资金费+点差安全垫 |
| 最小每格价差 | 资金费率的3~4倍（约0.15%~0.3%） | `min_step_pct` 参数 |
| 网格数量上限 | 由间距反推，绝对上限20 | 防止过密 |

### 3.4 冷静期 ATR 参数（默认值）

| 参数 | 值 | 说明 |
|------|-----|------|
| ATR 周期 | 14 | 标准14周期ATR |
| 趋稳判定窗口 | 30 分钟 | 观察最近30分钟价格行为 |
| ATR 回落阈值 | < 击穿前基准ATR的80% | ATR 需回落到基准水平以下 |
| 窄幅横盘阈值 | 最近30分钟振幅 < min_step_pct×2 | 价格足够稳定 |
| 最短冷静期 | 15 分钟 | 即便 ATR 立刻回落，也需等待最短时间 |

### 3.5 选币参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 评分方式 | 24h成交额(70%) + 订单簿深度(30%) | 综合流动性评分 |
| 选币时机 | 每个休市窗口开始前 | 不跨窗口复用选币结果 |
| 排除名单 | 可配置列表 | 手动排除不想交易的标的 |
| 候选池 | 所有 `xxxUSDT` 美股代币合约 | 自动拉取合约列表 |

---

## 4. 功能需求详细说明

### 4.1 时间窗口调度器（M1）

#### 4.1.1 功能描述

系统的"总开关"，控制整个策略在何时启动、何时停止。

#### 4.1.2 核心功能

**窗口判断**
- 使用 `pandas_market_calendars` 的 NYSE 日历判断当前是否处于美股休市时段
- 休市窗口定义：非交易日（周末 + NYSE 法定节假日）以及交易日的收盘后时段
- 需正确处理半日市（如感恩节次日 13:00 ET 提前收盘）
- 全程使用 UTC 时间计算，避免夏令时（DST）导致开盘时间算错

**开盘前强制清仓触发**
- 实时计算距下次真实开盘的剩余分钟数
- 当剩余时间 ≤ `pre_market_buffer_minutes`（= 美股盘前开始前2小时）时，触发全局强制离场信号
- 美股常规盘前开始时间：ET 04:00（北京时间夏令时 16:00，冬令时 17:00）
- 因此强制离场触发时点：ET 02:00（北京时间夏令时 14:00，冬令时 15:00）

**状态输出**
- `is_in_window() -> bool`：当前是否处于可交易休市窗口
- `minutes_to_next_open() -> float`：距下次开盘分钟数
- `should_force_close() -> bool`：是否应触发强制离场
- `get_next_window_start() -> datetime`：下次休市窗口开始时间

#### 4.1.3 边界情况处理

| 情况 | 处理方式 |
|------|---------|
| 节假日前一天是半日市 | 提前收盘时间需从日历精确获取，不能用固定16:00 |
| 跨时区夏令时切换 | 全程UTC，最终展示时转换本地时区 |
| 连续多天假期 | 窗口开始时间 = 上次收盘，离场触发 = 复市前2h |
| 系统在窗口期中间重启 | 重启时检查当前是否在窗口内，是则直接进入观察期 |

---

### 4.2 交易所接入层（M2）

#### 4.2.1 功能描述

封装所有与币安 API 的通信，向上层模块提供简洁的接口。

#### 4.2.2 核心功能

**连接与认证**
- 支持通过 HTTP 代理连接（SOCKS5 或 HTTP 均可配置）
- API Key 和 Secret 从环境变量或配置文件读取，不硬编码
- 连接失败时自动重试（指数退避，最多3次）
- WebSocket 断线后自动重连（心跳检测 + 重连机制）

**REST API 接口封装**
```
set_leverage(symbol, leverage)           # 设置杠杆
set_margin_type(symbol, ISOLATED)        # 设置逐仓模式
get_account_balance() -> float           # 查询可用余额
get_position(symbol) -> PositionInfo     # 查询单标的持仓
get_open_orders(symbol) -> List[Order]   # 查询未成交挂单
place_limit_order_post_only(...)         # 挂 POST_ONLY 限价单
place_market_order(...)                  # 市价单（用于紧急平仓）
cancel_order(symbol, order_id)           # 撤单
cancel_all_orders(symbol)               # 撤销某标的所有挂单
get_klines(symbol, interval, limit)      # 获取K线数据
get_24h_ticker(symbol) -> TickerInfo     # 获取24h行情
get_orderbook_depth(symbol, limit)       # 获取订单簿深度
get_funding_rate(symbol) -> float        # 获取当前资金费率
```

**WebSocket 实时数据**
- 订阅用户账户推送（成交回报、持仓变化）
- 订阅标的价格推送（实时 mark price 或 last price）
- 事件回调接口：`on_order_filled(callback)`、`on_price_update(callback)`

**代理配置**
```python
# 示例配置
PROXY_CONFIG = {
    "http":  "socks5://127.0.0.1:7890",
    "https": "socks5://127.0.0.1:7890"
}
```

#### 4.2.3 POST_ONLY 保障机制

所有网格挂单必须加 `timeInForce=GTX`（币安合约 POST_ONLY 参数）。若因市场波动导致该订单会立即成交（即会变成 Taker），交易所会自动拒绝此单（返回错误码），系统捕获后记录日志并在下一轮重新挂单，绝不以 Taker 身份成交网格单。

---

### 4.3 选币模块（M3）

#### 4.3.1 功能描述

每个休市窗口开始前，自动评估所有美股代币合约的流动性，选出最合适的标的。

#### 4.3.2 核心功能

**候选池获取**
- 调用币安 API 拉取所有 `xxxUSDT` 美股代币永续合约列表
- 自动过滤状态异常（如已下架、停牌）的合约
- 剔除排除名单（`symbol_blacklist`）中的标的

**流动性评分**
```
综合评分 = 0.7 × 成交额标准化分 + 0.3 × 深度标准化分

成交额分 = symbol_24h_volume / max_24h_volume_in_pool
深度分   = (bid_depth_5 + ask_depth_5) / max_depth_in_pool

其中 bid_depth_5 = 买一到买五的总挂单量（USDT计价）
     ask_depth_5 = 卖一到卖五的总挂单量（USDT计价）
```

**选币结果**
- 按综合评分降序排列
- 选出前 `max_concurrent` 个（3~5，可配置）
- 输出每个标的的选币理由（评分构成）并写入日志

**配置项**
```yaml
selection:
  max_concurrent: 3          # 最大并发标的数
  symbol_blacklist:           # 排除名单
    - TSLAPREUSDT
    - XXXYYYUSDT
  volume_weight: 0.7          # 成交额权重
  depth_weight: 0.3           # 深度权重
  depth_levels: 5             # 订单簿取几档
```

---

### 4.4 观察期与动态区间计算（M4）

#### 4.4.1 功能描述

窗口开始后，先静默采集数据，再用统计方法动态计算最适合当前行情的网格参数。

#### 4.4.2 观察期

- 时长：3小时（`observe_hours = 3`，可配置）
- 行为：每分钟采集一根1分钟K线，不下任何单
- 样本量检查：观察期结束时，有效K线数量需 ≥ 30，否则延长观察30分钟再检查（直到临近开盘时放弃本窗口）
- 同时记录观察期的ATR基准值（14周期），作为后续冷静期判断的参考基线

#### 4.4.3 区间计算

**方法A（默认）：均值 ± k×标准差**
```python
prices = [k.close for k in klines]
mu = mean(prices)
sigma = std(prices)
upper = mu + k * sigma   # k 默认 1.8
lower = mu - k * sigma
```

**方法B（备选）：分位数**
```python
upper = percentile(prices, 95)
lower = percentile(prices, 5)
```

两种方法均可配置，分位数法对极值更鲁棒，在有明显毛刺时优先选用。

#### 4.4.4 动态网格间距计算

```python
# 1. 计算区间宽度
range_pct = (upper - lower) / lower

# 2. 计算最小每格价差（需覆盖资金费+点差+滑点安全垫）
#    资金费通常 0.01%/8h，每天3次，每格至少覆盖半天资金费
funding_rate = get_current_funding_rate(symbol)
min_step_pct = max(funding_rate * safety_multiplier, ABSOLUTE_MIN_STEP)
# safety_multiplier 默认 3.5，ABSOLUTE_MIN_STEP 默认 0.15%

# 3. 反推网格数上限
max_grids = int(range_pct / min_step_pct)
grid_num = min(max_grids, MAX_GRID_LIMIT)   # MAX_GRID_LIMIT = 20

# 4. 实际间距（等比公式）
actual_step_pct = range_pct / grid_num
```

#### 4.4.5 区间合理性校验

以下任一情况发生，放弃本次建仓，延长观察或等待下个窗口：
- 样本量 < 30
- 区间宽度 < `min_step_pct`（连一格都放不下）
- 区间宽度 > 5%（波动异常，不适合低波动策略）
- 当前价格不在区间内（价格已经漂移出去）

---

### 4.5 网格执行引擎（M5）

#### 4.5.1 功能描述

策略的执行核心，负责在区间内挂满限价单，并在成交后自动维护网格完整性。

#### 4.5.2 网格初始化

```
输入：区间 [lower, upper]，当前价格 current_price，间距 step_pct，本金 capital

1. 计算所有网格价位（等比序列）
   grid_prices = [lower × r^0, lower × r^1, ..., lower × r^n]
   其中 r = 1 + step_pct

2. 以 current_price 为分界：
   - 当前价格以下的格位 → 挂买单（做多方向）
   - 当前价格以上的格位 → 挂卖单（平多方向 / 做空方向）

3. 计算每格下单量：
   qty_per_grid = (capital × leverage) / (current_price × grid_num)
   取交易所最小下单精度向下取整

4. 批量提交所有挂单（POST_ONLY 限价单）
   每单记录：{grid_index, side, price, qty, order_id, status}
```

#### 4.5.3 成交后补单逻辑

```
监听 WebSocket 成交回报：

当买单在 price_i 成交：
  → 挂对应的卖单 @ price_i × r（上一格）
  → 更新持仓记录：新增一份多仓 @price_i
  → 记录成交到数据库

当卖单在 price_j 成交：
  → 挂对应的买单 @ price_j / r（下一格）
  → 更新持仓记录：减少一份多仓
  → 计算本次网格盈利 = (price_j - price_i) × qty，写入数据库
  → 累计盈利 += 本次网格盈利
```

#### 4.5.4 并发管理

- 每个标的运行独立的网格引擎实例
- 通过 `asyncio` 异步并发，或每标的一个线程
- 各标的的状态机、订单、盈亏完全隔离，互不干扰
- 全局资金检查：开启新标的前检查总已用资金 + 新标的本金 ≤ `total_capital_limit`

#### 4.5.5 订单状态管理

维护内存中的订单状态字典，定期（每30秒）与交易所同步一次，防止 WebSocket 漏推导致内存状态与实际不一致：

```python
class OrderState(Enum):
    PENDING    = "pending"     # 已提交，待确认
    OPEN       = "open"        # 已挂单，等待成交
    FILLED     = "filled"      # 已成交
    CANCELLED  = "cancelled"   # 已撤销
    REJECTED   = "rejected"    # 被拒绝（含POST_ONLY被拒）
```

---

### 4.6 击穿-冷静期状态机（M6）

#### 4.6.1 状态定义

```python
class GridState(Enum):
    IDLE        = "空闲"        # 未在窗口内，或等待窗口
    OBSERVING   = "观察期"      # 窗口内，采集数据，不交易
    RUNNING     = "网格运行"    # 正常运行网格
    COOLDOWN    = "冷静期"      # 被击穿，撤单平仓，等待趋稳
    CLOSING     = "强制离场"    # 临近开盘，强制清仓
    STOPPED     = "已停止"      # 本窗口交易已结束
```

#### 4.6.2 状态转换矩阵

| 当前状态 | 触发事件 | 目标状态 | 附带动作 |
|---------|---------|---------|---------|
| IDLE | 进入休市窗口 | OBSERVING | 启动选币，开始采集K线 |
| OBSERVING | 3小时观察期结束 | RUNNING | 计算区间，建立网格，挂单 |
| OBSERVING | 临近开盘触发 | CLOSING | 无仓位，直接结束 |
| RUNNING | 价格突破区间边界 | COOLDOWN | 撤单，平仓 |
| RUNNING | 盈利 ≥ +10 USDT | CLOSING | 平仓止盈 |
| RUNNING | 动态止损触发 | CLOSING | 强制市价平仓 |
| RUNNING | 临近开盘触发 | CLOSING | 撤单，市价平仓 |
| COOLDOWN | ATR回落+横盘达标 | OBSERVING | 重新开始观察期（缩短版，1小时） |
| COOLDOWN | 临近开盘触发 | CLOSING | 撤单，平掉残余仓位 |
| COOLDOWN | 动态止损触发 | CLOSING | 强制市价平仓 |
| CLOSING | 清仓完成 | STOPPED | 记录本轮统计 |
| STOPPED | 新窗口开始 | OBSERVING | （等下一个窗口，重新开始） |

#### 4.6.3 ATR 冷静期趋稳判定算法

```python
def is_calm_enough(recent_klines, baseline_atr, config) -> bool:
    """
    判断是否从击穿中恢复到可重启网格的状态
    
    baseline_atr: 观察期结束时计算的ATR基准值
    recent_klines: 最近 calm_window_minutes 根1分钟K线
    """
    if len(recent_klines) < config.min_calm_samples:  # 最短冷静期检查
        return False
    
    # 条件1：当前ATR已回落到基准水平的80%以下
    current_atr = calc_atr(recent_klines, period=14)
    if current_atr >= baseline_atr * config.atr_recovery_ratio:  # 0.80
        return False
    
    # 条件2：最近 calm_window_minutes 分钟内价格振幅足够小
    recent_high = max(k.high for k in recent_klines[-config.calm_window_minutes:])
    recent_low  = min(k.low  for k in recent_klines[-config.calm_window_minutes:])
    amplitude_pct = (recent_high - recent_low) / recent_low
    if amplitude_pct >= config.min_step_pct * 2:
        return False
    
    # 两个条件都满足，认为已趋于平稳
    return True
```

#### 4.6.4 重启后的观察期（缩短版）

冷静期结束后重启，不再等3小时，而是用**缩短版观察期（1小时）**重新采集近期数据，重新计算区间（以反映新的价格中枢），然后建立新的网格。这确保重建的网格区间和间距能准确反映当前行情，而不是使用已经过时的3小时前的数据。

---

### 4.7 风控模块（M7）

#### 4.7.1 止盈逻辑

```python
# 每次网格成交后检查
if realized_pnl_usdt >= TAKE_PROFIT_USDT:  # 10 USDT
    trigger_close(symbol, reason="止盈")
```

止盈是"已实现盈利"的累计，不包含持仓浮盈，避免虚假止盈。

#### 4.7.2 动态止损逻辑

```python
# 实时价格监控
stop_loss_price = grid_lower × (1 - stop_buffer_pct)  # 默认 1~2%

if current_price <= stop_loss_price:
    trigger_force_close(symbol, reason="动态止损")
```

止损线跟随网格下限动态更新（每次重建网格时重算），不固定金额。

#### 4.7.3 全局资金风控

```python
# 开启新标的前检查
total_in_use = sum(capital for all active symbols)
if total_in_use + NEW_SYMBOL_CAPITAL > TOTAL_CAPITAL_LIMIT:  # 1000 USDT
    skip_this_symbol(reason="超出总资金上限")
```

#### 4.7.4 最大并发限制

```python
active_count = count(symbols with state in [OBSERVING, RUNNING, COOLDOWN])
if active_count >= max_concurrent:  # 3~5
    skip_remaining_candidates()
```

#### 4.7.5 异常订单处理

| 异常类型 | 处理方式 |
|---------|---------|
| POST_ONLY 被拒 | 记录日志，下轮重新挂单，不报警 |
| 挂单返回错误 | 重试最多3次，失败后记录告警日志 |
| 平仓市价单失败 | 立刻重试，同时触发邮件/日志告警 |
| 持仓量与预期不符 | 每30秒同步一次，差异超过阈值时告警 |
| 交易所连接断开 | 自动重连；重连期间持仓保持不变（有硬止损单托底） |

#### 4.7.6 交易所端止损单（双保险）

**强烈建议**：在代码端动态止损之外，同时通过 API 在交易所端挂一个**止损限价单（STOP_MARKET）**，作为程序宕机时的兜底保险：

```python
place_stop_market_order(
    symbol    = symbol,
    side      = "SELL",
    stopPrice = grid_lower * (1 - stop_buffer_pct * 1.5),  # 比代码端再宽松一点
    closePosition = True
)
```

---

### 4.8 数据持久化（M8）

#### 4.8.1 数据库选型

使用 **SQLite**（`sqlite3` 标准库）：
- 无需独立数据库服务，适合家用服务器
- 单文件，备份方便
- 对写入频率要求不高（网格成交非高频）
- Web 界面只读查询，无并发写冲突问题

数据库文件路径：`data/trading.db`

#### 4.8.2 表结构设计

**窗口记录表 `windows`**
```sql
CREATE TABLE windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start    DATETIME NOT NULL,   -- 窗口开始时间（UTC）
    window_end      DATETIME,            -- 窗口结束时间
    status          TEXT NOT NULL,       -- open / closed
    total_pnl       REAL DEFAULT 0,      -- 本窗口总盈亏（USDT）
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**标的会话表 `sessions`**
```sql
CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       INTEGER REFERENCES windows(id),
    symbol          TEXT NOT NULL,       -- 如 AAPLUSDT
    state           TEXT NOT NULL,       -- 当前状态机状态
    grid_upper      REAL,                -- 网格上沿
    grid_lower      REAL,                -- 网格下沿
    grid_num        INTEGER,             -- 网格数量
    step_pct        REAL,                -- 每格间距(%)
    baseline_atr    REAL,                -- 观察期基准ATR
    stop_loss_price REAL,                -- 当前动态止损价
    capital         REAL,                -- 投入本金
    leverage        INTEGER,             -- 杠杆倍数
    realized_pnl    REAL DEFAULT 0,      -- 已实现盈亏
    open_time       DATETIME,            -- 开始时间
    close_time      DATETIME,            -- 结束时间
    close_reason    TEXT,                -- 结束原因
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**网格成交记录表 `trades`**
```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    order_id        TEXT NOT NULL,       -- 交易所订单ID
    side            TEXT NOT NULL,       -- BUY / SELL
    price           REAL NOT NULL,       -- 成交价
    qty             REAL NOT NULL,       -- 成交数量
    quote_qty       REAL NOT NULL,       -- 成交金额（USDT）
    grid_index      INTEGER,             -- 属于第几格
    grid_pnl        REAL,                -- 本次网格收益（卖单触发时计算）
    fee             REAL DEFAULT 0,      -- 实际手续费（Maker应为0）
    funding_fee     REAL DEFAULT 0,      -- 资金费（结算时记录）
    trade_time      DATETIME NOT NULL,   -- 成交时间
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**状态机日志表 `state_logs`**
```sql
CREATE TABLE state_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    from_state      TEXT NOT NULL,       -- 转换前状态
    to_state        TEXT NOT NULL,       -- 转换后状态
    trigger         TEXT NOT NULL,       -- 触发事件
    detail          TEXT,                -- 详细信息（JSON）
    log_time        DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**系统日志表 `system_logs`**
```sql
CREATE TABLE system_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL,       -- INFO / WARN / ERROR
    module          TEXT NOT NULL,       -- 来源模块
    message         TEXT NOT NULL,       -- 日志内容
    detail          TEXT,                -- 额外信息（JSON）
    log_time        DATETIME NOT NULL
);
```

---

### 4.9 Web 监控界面（M9）

#### 4.9.1 技术方案

- **后端**：FastAPI（异步，轻量，自带 OpenAPI 文档）
- **前端**：Vue 3 + Vite（轻量，中文无障碍）或直接使用 Streamlit（更简单，纯Python）
- **通信**：REST API + WebSocket 推送（实时日志和盈亏更新）
- **认证**：v1 不做复杂认证，局域网访问，可配置简单 token
- **端口**：默认 `8080`，可配置

**简化方案（推荐 v1 先用）**：使用 Streamlit，几十行 Python 代码即可实现中文监控页，无需写前端，后端交易进程和 Streamlit 进程各跑一个，通过读取 SQLite 数据库共享状态。

#### 4.9.2 页面结构

**页面1：总览仪表板**
- 系统状态：当前是否在窗口内、当前窗口开始时间、距下次开盘时间
- 今日/本窗口总盈亏（USDT）
- 活跃标的数 / 最大并发数
- 总资金使用情况（已用 / 上限）
- 所有标的的简要状态卡片

**页面2：标的详情**
- 下拉选择某个标的
- 当前状态机状态（图示）
- 当前网格区间可视化（价格尺 + 当前价 + 网格线）
- 当前持仓和持仓均价
- 当前所有挂单列表（价格、数量、方向、状态）
- 本标的本窗口盈亏曲线（折线图）

**页面3：成交记录**
- 按窗口/标的筛选
- 表格展示：时间、标的、方向、成交价、数量、网格收益
- 统计：本窗口成交笔数、总手续费（应为0）、总网格收益、资金费合计

**页面4：历史复盘**
- 按窗口汇总：开始时间、结束时间、标的列表、总盈亏、结束原因
- 查看某个历史窗口的详细记录

**页面5：实时日志**
- 滚动显示最新100条日志
- 颜色区分：INFO（灰）、WARN（黄）、ERROR（红）
- 支持按模块过滤

#### 4.9.3 数据刷新策略

- 仪表板：每10秒自动刷新
- 挂单列表：每5秒刷新
- 实时盈亏：WebSocket 推送（有成交时立即更新）
- 历史记录：手动刷新或每分钟刷新

---

## 5. 技术架构

### 5.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户（浏览器）                             │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP / WebSocket
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Web 监控界面进程                               │
│           FastAPI / Streamlit  (Port: 8080)                     │
│  ┌─────────────┐ ┌──────────────┐ ┌──────────────────────────┐ │
│  │  总览仪表板  │ │  标的详情页  │ │  成交记录 / 历史复盘      │ │
│  └─────────────┘ └──────────────┘ └──────────────────────────┘ │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 只读查询
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      SQLite 数据库                               │
│              data/trading.db                                    │
│   windows / sessions / trades / state_logs / system_logs        │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 读写
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    核心交易进程（主进程）                          │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐  │
│  │  M1 调度器   │   │  M3 选币模块  │   │  M4 观察期+区间计算 │  │
│  │ (时间窗口)   │   │ (流动性评分)  │   │  (统计+ATR基准)    │  │
│  └──────┬───────┘   └──────┬───────┘   └─────────┬──────────┘  │
│         │                  │                     │             │
│         └──────────────────┼─────────────────────┘             │
│                            ▼                                   │
│         ┌──────────────────────────────────────────────────┐   │
│         │            M6 状态机调度器                         │   │
│         │   标的A状态机  标的B状态机  标的C状态机  ...        │   │
│         └──────────────────────┬───────────────────────────┘   │
│                                │                               │
│         ┌──────────────────────▼───────────────────────────┐   │
│         │            M5 网格执行引擎                         │   │
│         │   (挂单 / 补单 / 撤单 / 持仓管理)                  │   │
│         └──────────────────────┬───────────────────────────┘   │
│                                │                               │
│         ┌──────────────────────▼───────────────────────────┐   │
│         │            M7 风控模块                             │   │
│         │   (止盈 / 动态止损 / 资金限制 / 并发限制)           │   │
│         └──────────────────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────────────┘
                              │ REST API + WebSocket（通过代理）
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                 M2 交易所接入层                                   │
│         python-binance / ccxt  +  代理配置                       │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTPS / WSS（通过 SOCKS5/HTTP 代理）
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    币安交易所                                     │
│         USDS-M 永续合约 API  (TradFi美股代币)                    │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 进程模型

系统运行两个独立进程，通过 SQLite 数据库共享状态：

```
主交易进程（trader.py）
├── 主事件循环（asyncio）
├── 调度协程：每5分钟检查窗口状态
├── 每个活跃标的：独立协程
│   ├── 状态机协程
│   ├── 网格执行协程
│   └── 价格监控协程
└── WebSocket 管理器：统一管理所有 WS 连接

Web监控进程（web.py）
├── FastAPI 或 Streamlit 进程
├── 只读访问 SQLite
└── 推送实时更新（WebSocket）
```

启动方式：
```bash
# 启动交易进程（后台）
nohup python trader.py > logs/trader.log 2>&1 &

# 启动 Web 监控（后台）
nohup python web.py > logs/web.log 2>&1 &
```

### 5.3 目录结构

```
grid_trader/
│
├── config/
│   ├── config.yaml              # 主配置文件
│   └── config.example.yaml     # 配置示例（不含密钥）
│
├── core/
│   ├── __init__.py
│   ├── scheduler.py             # M1 时间窗口调度器
│   ├── exchange.py              # M2 交易所接入层
│   ├── selector.py              # M3 选币模块
│   ├── observer.py              # M4 观察期与区间计算
│   ├── engine.py                # M5 网格执行引擎
│   ├── state_machine.py         # M6 状态机
│   ├── risk.py                  # M7 风控模块
│   └── models.py                # 数据模型（dataclass）
│
├── db/
│   ├── __init__.py
│   ├── database.py              # SQLite 连接和操作封装
│   └── migrations/
│       └── 001_init.sql         # 初始化表结构
│
├── web/
│   ├── __init__.py
│   ├── app.py                   # FastAPI 应用 或 Streamlit app
│   ├── routers/
│   │   ├── dashboard.py
│   │   ├── sessions.py
│   │   └── trades.py
│   └── static/                  # 前端静态文件（如用Vue）
│
├── utils/
│   ├── __init__.py
│   ├── logger.py                # 统一日志配置
│   ├── indicators.py            # ATR等技术指标计算
│   └── time_utils.py            # 时区和时间工具
│
├── data/
│   └── trading.db               # SQLite 数据库文件（运行时生成）
│
├── logs/
│   ├── trader.log               # 交易进程日志
│   └── web.log                  # Web进程日志
│
├── tests/
│   ├── test_scheduler.py
│   ├── test_observer.py
│   ├── test_engine.py
│   └── test_state_machine.py
│
├── trader.py                    # 交易进程入口
├── web.py                       # Web监控进程入口
├── requirements.txt             # Python 依赖
├── .env.example                 # 环境变量示例
├── .env                         # 环境变量（含密钥，不入git）
├── .gitignore
└── README.md
```

### 5.4 技术栈选型

| 层次 | 技术 | 版本 | 理由 |
|------|------|------|------|
| 语言 | Python | 3.10+ | 生态丰富，asyncio成熟 |
| 交易所 API | python-binance | latest | 成熟的币安封装库 |
| 交易所备选 | ccxt | latest | 若python-binance不支持某接口 |
| 交易日历 | pandas_market_calendars | latest | NYSE日历，支持半日市 |
| 数值计算 | numpy, pandas | latest | K线数据处理和统计计算 |
| 数据库 | SQLite3 | 内置 | 轻量，适合家用服务器 |
| 异步框架 | asyncio | 内置 | 并发多标的 |
| Web后端 | FastAPI | latest | 轻量异步，自带文档 |
| Web前端v1 | Streamlit | latest | 纯Python，快速出界面 |
| 配置管理 | pyyaml + python-dotenv | latest | yaml配置+env密钥分离 |
| 日志 | loguru | latest | 比标准logging更好用 |
| 代理支持 | httpx[socks] + aiohttp | latest | 支持SOCKS5代理 |
| 进程守护 | systemd 或 supervisor | 系统自带 | 保证进程崩溃后自动重启 |

**依赖清单（requirements.txt）**
```
python-binance>=1.0.19
ccxt>=4.0.0
pandas_market_calendars>=4.0.0
pandas>=2.0.0
numpy>=1.24.0
fastapi>=0.100.0
uvicorn>=0.23.0
streamlit>=1.28.0
pyyaml>=6.0
python-dotenv>=1.0.0
loguru>=0.7.0
httpx[socks]>=0.25.0
aiohttp>=3.8.0
websockets>=11.0
```

---

## 6. 关键数据结构与接口设计

### 6.1 数据库表结构

（已在 4.8.2 节详细说明，此处给出完整 DDL）

```sql
-- 001_init.sql

PRAGMA journal_mode = WAL;  -- 支持读写并发（Web读 + 交易写）

CREATE TABLE IF NOT EXISTS windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start    DATETIME NOT NULL,
    window_end      DATETIME,
    status          TEXT NOT NULL DEFAULT 'open',
    total_pnl       REAL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       INTEGER REFERENCES windows(id),
    symbol          TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'IDLE',
    grid_upper      REAL,
    grid_lower      REAL,
    grid_num        INTEGER,
    step_pct        REAL,
    baseline_atr    REAL,
    stop_loss_price REAL,
    capital         REAL DEFAULT 200,
    leverage        INTEGER DEFAULT 10,
    realized_pnl    REAL DEFAULT 0,
    open_time       DATETIME,
    close_time      DATETIME,
    close_reason    TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    qty             REAL NOT NULL,
    quote_qty       REAL NOT NULL,
    grid_index      INTEGER,
    grid_pnl        REAL,
    fee             REAL DEFAULT 0,
    funding_fee     REAL DEFAULT 0,
    trade_time      DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS state_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    detail          TEXT,
    log_time        DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL,
    module          TEXT NOT NULL,
    message         TEXT NOT NULL,
    detail          TEXT,
    log_time        DATETIME NOT NULL
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_sessions_window   ON sessions(window_id);
CREATE INDEX IF NOT EXISTS idx_sessions_symbol   ON sessions(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_session    ON trades(session_id);
CREATE INDEX IF NOT EXISTS idx_trades_time       ON trades(trade_time);
CREATE INDEX IF NOT EXISTS idx_state_logs_session ON state_logs(session_id);
```

### 6.2 核心数据类

```python
# core/models.py
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
from enum import Enum

class GridState(Enum):
    IDLE      = "空闲"
    OBSERVING = "观察期"
    RUNNING   = "网格运行"
    COOLDOWN  = "冷静期"
    CLOSING   = "强制离场"
    STOPPED   = "已停止"

class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"

class OrderStatus(Enum):
    PENDING   = "pending"
    OPEN      = "open"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"

@dataclass
class GridParams:
    """动态计算出的网格参数"""
    symbol:          str
    upper:           float        # 区间上沿
    lower:           float        # 区间下沿
    center:          float        # 区间中枢（均值）
    grid_num:        int          # 网格数量
    step_pct:        float        # 每格百分比间距
    grid_prices:     List[float]  # 所有网格价位
    baseline_atr:    float        # 观察期ATR基准
    stop_loss_price: float        # 动态止损价
    calculated_at:   datetime     # 计算时间

@dataclass
class GridOrder:
    """单个网格挂单"""
    symbol:      str
    order_id:    str
    client_id:   str
    grid_index:  int
    side:        OrderSide
    price:       float
    qty:         float
    status:      OrderStatus
    created_at:  datetime
    filled_at:   Optional[datetime] = None
    fill_price:  Optional[float]    = None

@dataclass
class SymbolSession:
    """单个标的的完整会话状态"""
    session_id:    int
    symbol:        str
    state:         GridState
    params:        Optional[GridParams]
    orders:        List[GridOrder]
    realized_pnl:  float
    capital:       float
    leverage:      int
    open_time:     datetime
    kline_buffer:  List[dict] = field(default_factory=list)  # 观察期K线缓存

@dataclass
class TickerInfo:
    """标的行情快照"""
    symbol:       str
    last_price:   float
    bid_price:    float
    ask_price:    float
    volume_24h:   float        # USDT计价24h成交额
    bid_qty_5:    float        # 买一到买五总量（USDT）
    ask_qty_5:    float        # 卖一到卖五总量（USDT）
    funding_rate: float        # 当前资金费率
    timestamp:    datetime
```

### 6.3 模块间接口

```python
# 调度器 → 主控
class Scheduler:
    def is_in_window(self) -> bool: ...
    def should_force_close(self) -> bool: ...
    def minutes_to_next_open(self) -> float: ...

# 选币模块 → 主控
class Selector:
    def select(self, max_n: int, blacklist: List[str]) -> List[str]: ...

# 观察期 → 主控
class Observer:
    async def run(self, symbol: str, duration_hours: float) -> Optional[GridParams]: ...

# 网格引擎 ← 主控调用
class GridEngine:
    async def start(self, session: SymbolSession) -> None: ...
    async def stop(self, symbol: str, reason: str) -> None: ...
    async def force_close(self, symbol: str) -> None: ...

# 状态机 ← 主控调用
class StateMachine:
    def transition(self, symbol: str, event: str) -> GridState: ...
    def get_state(self, symbol: str) -> GridState: ...
```

---

## 7. 状态机详细设计

### 7.1 完整状态转换图

```
                    ┌─────────────────┐
                    │      IDLE       │◄──────────────────────┐
                    │     空闲        │                       │
                    └────────┬────────┘                       │
                             │ 进入休市窗口                    │
                             ▼                               │
                    ┌─────────────────┐                      │
                    │   OBSERVING     │◄──────────────┐      │
                    │    观察期       │               │      │
                    └────────┬────────┘               │      │
                             │ 3h后计算区间成功        │      │
                             │                        │      │
                    ┌────────▼────────┐               │      │
                    │    RUNNING      │               │      │
                    │   网格运行      │               │      │
                    └────────┬────────┘               │      │
                  ┌──────────┼───────────┐            │      │
         价格击穿  │          │止盈/止损   │临近开盘    │ATR   │窗口
                  ▼          ▼           ▼          回落    结束
         ┌────────────┐  ┌──────────────────┐       │      │
         │  COOLDOWN  │  │    CLOSING       │       │      │
         │  冷静期    │  │   强制离场       │       │      │
         └────────────┘  └────────┬─────────┘       │      │
                │                 │                  │      │
                │ATR回落          ▼                  │      │
                └──────►┌────────────────┐           │      │
         临近开盘触发    │   STOPPED      │───────────┘      │
                         │   已停止      │──────────────────┘
                         └────────────────┘
```

### 7.2 各状态的入口动作和出口动作

| 状态 | 进入时执行 | 持续执行 | 退出时执行 |
|------|----------|---------|---------|
| IDLE | 无 | 每5min检查时间窗口 | 触发选币流程 |
| OBSERVING | 初始化K线缓存 | 每分钟采集K线 | 计算区间参数 |
| RUNNING | 挂满初始网格单 | 监听成交，补单，检查风控 | 撤所有挂单 |
| COOLDOWN | 撤单+平仓 | 每分钟检查ATR趋稳 | 清理缓存，准备重启 |
| CLOSING | 撤单+市价平仓 | 等待平仓确认 | 写入最终统计到DB |
| STOPPED | 写入窗口结束记录 | 等待下个窗口 | 重置所有状态 |

---

## 8. 风控规则全集

| 规则编号 | 规则名称 | 触发条件 | 动作 | 优先级 |
|---------|---------|---------|------|-------|
| R1 | 开盘强制离场 | 距美股盘前开始 ≤ 120分钟 | 撤单+市价平仓+停止 | 最高 |
| R2 | 止盈 | 单标的已实现盈利 ≥ +10 USDT | 撤单+市价平仓 | 高 |
| R3 | 动态止损 | 价格 ≤ grid_lower×(1-stop_buffer) | 强制市价平仓 | 高 |
| R4 | 区间击穿进冷静 | 价格突破区间上/下沿 | 撤单+平仓+冷静期 | 中 |
| R5 | 总资金上限 | 已用 + 新标的本金 > 1000 USDT | 跳过新标的 | 高 |
| R6 | 并发数上限 | 活跃标的数 ≥ max_concurrent | 跳过新标的 | 中 |
| R7 | 样本不足 | 观察期K线 < 30根 | 延长观察 | 中 |
| R8 | 区间异常 | 区间宽度 < min_step 或 > 5% | 跳过建仓 | 中 |
| R9 | POST_ONLY被拒 | 挂单会立即成交 | 放弃本次挂单 | 低 |
| R10 | 持仓对账 | 内存持仓与交易所差异超阈值 | 告警+强制同步 | 中 |
| R11 | 交易所端止损单 | 系统启动建仓时 | 自动挂STOP_MARKET | 最高（兜底） |

---

## 9. 部署与运维

### 9.1 环境要求

- OS：Linux（Ubuntu 22.04 LTS 推荐）
- Python：3.10+
- 内存：≥ 512MB（4GB推荐）
- 磁盘：≥ 5GB（日志和数据库增长空间）
- 网络：需能通过代理访问币安 API

### 9.2 配置文件（config/config.yaml）

```yaml
# 交易配置
trading:
  leverage: 10
  capital_per_symbol: 200          # 单标的本金（USDT）
  total_capital_limit: 1000        # 总资金上限（USDT）
  take_profit_usdt: 10             # 单标的止盈阈值
  stop_buffer_pct: 0.015           # 止损缓冲比例（1.5%）
  max_concurrent: 3                # 最大并发标的数

# 时间参数
timing:
  observe_hours: 3                 # 观察期时长
  observe_kline_interval: "1m"     # 观察期K线周期
  force_close_minutes: 120         # 盘前开始前多少分钟强制离场
  cooldown_re_observe_hours: 1     # 冷静期结束后缩短版观察期时长
  min_calm_minutes: 15             # 最短冷静期（分钟）

# 区间计算参数
grid:
  range_method: "std"              # std（标准差法）或 quantile（分位数法）
  std_k: 1.8                       # 标准差法的k值
  quantile_upper: 0.95             # 分位数法上分位
  quantile_lower: 0.05             # 分位数法下分位
  min_step_pct: 0.0015             # 每格最小价差（0.15%）
  safety_multiplier: 3.5           # 安全倍数（相对资金费率）
  max_grid_num: 20                 # 最大网格数

# 冷静期参数
cooldown:
  atr_period: 14
  calm_window_minutes: 30
  atr_recovery_ratio: 0.80         # ATR需回落到基准的80%以下
  amplitude_multiplier: 2.0        # 振幅需小于 min_step_pct×2

# 选币参数
selection:
  volume_weight: 0.7
  depth_weight: 0.3
  depth_levels: 5
  symbol_blacklist: []             # 排除名单

# 代理配置
proxy:
  enabled: true
  http:  "socks5://127.0.0.1:7890"
  https: "socks5://127.0.0.1:7890"

# Web界面
web:
  port: 8080
  auth_token: ""                   # 留空不认证，填入token则需认证

# 数据库
database:
  path: "data/trading.db"

# 日志
logging:
  level: "INFO"                    # DEBUG / INFO / WARN / ERROR
  file: "logs/trader.log"
  rotation: "100 MB"
  retention: "30 days"
```

### 9.3 环境变量（.env）

```bash
# 币安API密钥（只读+交易权限，禁止提现权限）
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# 是否使用测试网（开发阶段务必开启）
BINANCE_TESTNET=true
```

### 9.4 进程守护（systemd）

```ini
# /etc/systemd/system/grid-trader.service
[Unit]
Description=Grid Trader - Trading Process
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/grid_trader
ExecStart=/home/ubuntu/grid_trader/.venv/bin/python trader.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/grid_trader/logs/trader.log
StandardError=append:/home/ubuntu/grid_trader/logs/trader.log
EnvironmentFile=/home/ubuntu/grid_trader/.env

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/grid-web.service
[Unit]
Description=Grid Trader - Web Monitor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/grid_trader
ExecStart=/home/ubuntu/grid_trader/.venv/bin/python web.py
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/grid_trader/.env

[Install]
WantedBy=multi-user.target
```

```bash
# 启用和启动
sudo systemctl enable grid-trader grid-web
sudo systemctl start grid-trader grid-web

# 查看状态
sudo systemctl status grid-trader
sudo journalctl -u grid-trader -f
```

### 9.5 开发和测试流程

```bash
# 1. 克隆和安装依赖
git clone <your-repo>
cd grid_trader
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入测试网 API Key

# 3. 确认 config.yaml 中 BINANCE_TESTNET=true

# 4. 初始化数据库
python -c "from db.database import init_db; init_db()"

# 5. 启动交易进程（前台，方便看日志）
python trader.py

# 6. 另开终端启动 Web 监控
python web.py
# 浏览器打开 http://localhost:8080

# 7. 跑通测试网后，改为 BINANCE_TESTNET=false
# 8. 用极小资金跑1-2个真实窗口（周末），验证全链路
# 9. 验证通过后，切换到 systemd 守护进程模式
```

---

## 10. 开发路线与 TODO

### 10.1 v1.0 开发任务（本期完成）

- [x] 需求确认
- [x] M1 时间窗口调度器
- [x] M2 交易所接入层（含代理）
- [x] M3 选币模块
- [x] M4 观察期与动态区间计算
- [x] M5 网格执行引擎
- [x] M6 击穿-冷静期状态机
- [x] M7 风控模块
- [x] M8 SQLite 数据持久化
- [x] M9 Web 监控界面（Streamlit 只读版）
- [ ] 测试网全链路测试
- [ ] 实盘小资金验证（2~3个窗口）

### 10.2 v1.1 TODO（优先级高）

- [x] 钉钉/Telegram 通知：成交、止盈止损、击穿、程序异常等关键事件推送
- [x] Web 界面操作功能：在网页上手动停止/启动某标的网格
- [x] Web 界面参数修改：不重启进程修改参数（止盈、并发数等）
- [x] 回测模块：用历史休市时段数据验证策略表现
- [x] 费率监控：自动检测 Maker 零费率活动是否仍有效，费率变化时告警

### 10.3 v1.2 TODO（中期优化）

- [x] 滚动重算区间：每隔一段时间（如2小时）用最新数据滚动更新区间，从静态网格进化为自适应网格
- [x] Web 界面手动平仓：安全的手动平仓操作，带二次确认
- [x] 多账户支持：同时管理多个 API Key 的账户
- [x] 性能优化：高并发下的订单管理和数据库写入优化

### 10.4 当前剩余验收项

- [x] 用 `--binance-check`、`--binance-position-smoke`、`--binance-safety-sweep` 完成测试网前置验证
- [x] 用 `--binance-test-run --loop-seconds <seconds>` 完成单账户有界测试网运行
- [x] 在当前 `default` 单账户配置下，用 `--all-accounts --binance-check` 和短时 `--all-accounts --binance-test-run` 验证并发入口兼容
- [ ] 配置第二套测试网 API Key 后，用 `--all-accounts --binance-check` 和短时 `--all-accounts --binance-test-run` 验证多账户隔离
- [x] 确认测试网运行结束后，所有 allowlist 标的挂单和仓位残留为 0
- [ ] 在测试网稳定后，再安排极小资金、低杠杆、单标的实盘验证

2026-07-09 前置验收记录：

- `--binance-check` 通过：测试网连接、余额读取、交易规则读取和 BTCUSDT/ETHUSDT/BCHUSDT Maker 费率健康检查均正常。
- `--binance-position-smoke` 通过：Hedge Mode 下 BTCUSDT/ETHUSDT/BCHUSDT 净仓位、LONG、SHORT、普通挂单和 Algo 条件单均为 0。
- `--binance-safety-sweep` 通过：清扫前后 BTCUSDT/ETHUSDT/BCHUSDT 普通挂单、Algo 条件单和仓位残留均为 0。

2026-07-09 单账户短时测试网运行记录：

- `--binance-test-run --loop-seconds 60` 通过：前置持仓检查正常，交易 loop 按 60 秒上限结束，安全清扫成功，后置持仓检查正常。
- 运行结束后再次执行 `--binance-position-smoke` 通过：BTCUSDT/ETHUSDT/BCHUSDT 净仓位、LONG、SHORT、普通挂单和 Algo 条件单均为 0。

2026-07-09 单账户延长测试网运行记录：

- `--binance-test-run --loop-seconds 180` 通过：前置持仓检查正常，交易 loop 按 180 秒上限结束，期间关闭过 BCHUSDT 活动会话，最终安全清扫成功，后置持仓检查正常。
- 运行结束后再次执行 `--binance-position-smoke` 通过：BTCUSDT/ETHUSDT/BCHUSDT 净仓位、LONG、SHORT、普通挂单和 Algo 条件单均为 0。

2026-07-09 单账户 10 分钟测试网运行记录：

- `--binance-test-run --loop-seconds 600` 通过：前置持仓检查正常，交易 loop 按 600 秒上限结束，期间关闭过 BCHUSDT 活动会话，最终安全清扫成功，后置持仓检查正常。
- 运行结束后再次执行 `--binance-position-smoke` 通过：BTCUSDT/ETHUSDT/BCHUSDT 净仓位、LONG、SHORT、普通挂单和 Algo 条件单均为 0。

2026-07-09 `--all-accounts` 入口测试网运行记录：

- 当前仅配置 `default` 一个账户；`--all-accounts --binance-check` 通过，结果按 `default` 聚合返回。
- `--all-accounts --binance-test-run --loop-seconds 30` 通过：前置持仓检查正常，交易 loop 按 30 秒上限结束，安全清扫成功，后置持仓检查正常。
- 运行结束后再次执行 `--binance-position-smoke` 通过：BTCUSDT/ETHUSDT/BCHUSDT 净仓位、LONG、SHORT、普通挂单和 Algo 条件单均为 0。
- 由于尚未配置第二套测试网 API Key，真实多账户隔离验证仍需后续补测。

### 10.5 长期 Backlog

- [ ] 更智能的标的评分：加入价格稳定性指标、历史休市波动率统计
- [ ] 参数自动优化：基于历史数据自动建议 k 值、冷静期参数等
- [ ] 风险报告：每周自动生成策略绩效报告（Sharpe、最大回撤、盈亏比）

---

## 11. 风险提示

> ⚠️ **以下风险提示是系统设计的一部分，请在开发和使用中始终牢记。**

### 11.1 本系统无法规避的系统性风险

1. **10倍杠杆的本质风险**：杠杆放大盈利的同时也放大亏损。10倍杠杆下，价格反向移动10%即可导致爆仓。美股代币合约本身也存在溢价/折价、强制结算等机制风险。

2. **低流动性下的止损失效**：节假日和周末期间，订单簿深度有限。当止损被触发时，市价平仓可能因深度不足产生严重滑点，导致实际止损价远低于设定止损价。

3. **程序宕机的裸奔风险**：若交易进程宕机且未及时发现，仓位将裸奔到开盘时遭遇跳空。**交易所端止损单（R11）是最重要的程序外兜底，必须实现。**

4. **Maker零费率政策的时效性**：该政策是限时活动，一旦结束恢复正常费率，策略的盈利模型将改变，需及时重新评估参数。

5. **历史样本稀缺**：美股节假日和周末合计一年约60+个窗口，相对其他策略样本极少，统计检验置信度有限，存在"过拟合运气"的可能性。

### 11.2 开发阶段的硬性要求

- **必须先在测试网跑通完整逻辑，再动用真实资金**
- **真实资金第一阶段：极小资金（全流程验证用，亏了不心疼）+ 低杠杆（2~3倍）+ 单标的**
- **在确认净收益持续为正之前，不增加杠杆、不增加并发数、不增加资金**
- **本文档是系统设计规格，不构成任何投资建议**

---

*文档结束*

*最后更新：2026-07-03*  
*下一步：进入开发阶段，从 M1（时间窗口调度器）开始，逐模块实现和测试*
