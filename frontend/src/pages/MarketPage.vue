<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { CheckCircle2, Clock3, RefreshCw, ShieldX, TriangleAlert } from '@lucide/vue'
import MiniLineChart from '../components/MiniLineChart.vue'
import StatusBadge from '../components/StatusBadge.vue'
import {
  loadV2RegimeHistory,
  type V2DashboardData,
  type V2RegimeDecision,
} from '../api'
import type { LiquidityCandidate } from '../mock'

const props = defineProps<{
  accountId: string
  dashboard: V2DashboardData
  candidates: LiquidityCandidate[]
}>()

const selectedSymbol = ref('')
const history = ref<V2RegimeDecision[]>([])
const loadingHistory = ref(false)
const historyError = ref('')

const componentLabels: Record<string, string> = {
  volatility: '波动率',
  trend: '趋势强度',
  liquidity: '流动性',
  mean_reversion: '均值回归',
  cost: '交易成本',
  event: '事件风险',
}

const stateLabels: Record<string, string> = {
  QUIET_RANGE: '震荡区间',
  TREND_UP: '上升趋势',
  TREND_DOWN: '下降趋势',
  VOLATILE: '高波动',
  ILLIQUID: '流动性不足',
  EVENT_RISK: '事件风险',
  UNKNOWN_DATA: '数据状态未知',
  UNKNOWN: '历史数据状态未知',
}

const verdictLabels: Record<string, string> = {
  ALLOWED: '准入通过',
  BLOCKED_SCORE: '评分未通过',
  BLOCKED_COST: '交易经济性不足',
  BLOCKED_ECONOMICS: '网格经济性不足',
  BLOCKED_RISK: '风险预算不足',
  BLOCKED_HARD: '硬条件阻断',
  BLOCKED_DATA: '数据阻断',
}

const availableSymbols = computed(() => Array.from(new Set([
  props.dashboard.latestRegime?.symbol,
  ...props.candidates.map((item) => item.symbol),
].filter((item): item is string => Boolean(item)))))

const currentRegime = computed(() => history.value.at(-1)
  || (props.dashboard.latestRegime?.symbol === selectedSymbol.value
    ? props.dashboard.latestRegime
    : null))

const componentRows = computed(() => Object.entries(
  currentRegime.value?.componentScores || {},
).map(([key, value]) => {
  const unavailable = key === 'event' && !currentRegime.value?.eventSourceAvailable
  return {
    key,
    label: componentLabels[key] || key,
    value: unavailable || value == null ? null : Math.max(0, Math.min(100, Number(value))),
    weight: Number(currentRegime.value?.effectiveWeights[key] || 0),
    contribution: Number(currentRegime.value?.scoreContributions[key] || 0),
  }
}))

const scoreHistory = computed(() => history.value.map((item) => item.gridScore))
const marketStateLabel = computed(() => {
  const regime = currentRegime.value
  if (!regime) return '等待首个快照'
  if (regime.state === 'UNKNOWN' && regime.verdict === 'BLOCKED_SCORE') {
    return '历史评分未通过'
  }
  if (regime.state === 'UNKNOWN' && regime.verdict === 'ALLOWED') {
    return '历史评分已通过'
  }
  return stateLabels[regime.state] || regime.state
})
const verdictLabel = computed(() => verdictLabels[currentRegime.value?.verdict || ''] || currentRegime.value?.verdict || '等待准入判断')
const cost = computed(() => currentRegime.value?.costBreakdown || {})
const costScore = computed(() => finiteCostValue('cost_score'))
const costEvaluationLabel = computed(() => costScore.value == null
  ? '成本未评估'
  : `成本得分 ${costScore.value.toFixed(1)}`)

onMounted(ensureSelectedSymbol)
watch(availableSymbols, ensureSelectedSymbol)
watch(
  () => [props.accountId, selectedSymbol.value],
  () => void refreshHistory(),
)

function ensureSelectedSymbol() {
  if (!selectedSymbol.value || !availableSymbols.value.includes(selectedSymbol.value)) {
    selectedSymbol.value = props.dashboard.latestRegime?.symbol
      || availableSymbols.value[0]
      || ''
  }
}

async function refreshHistory() {
  if (!selectedSymbol.value) {
    history.value = []
    return
  }
  loadingHistory.value = true
  historyError.value = ''
  try {
    history.value = await loadV2RegimeHistory(selectedSymbol.value, props.accountId)
  } catch (reason) {
    history.value = []
    historyError.value = reason instanceof Error ? reason.message : '无法加载市场状态历史'
  } finally {
    loadingHistory.value = false
  }
}

function money(value: number | null) {
  if (value == null) return '—'
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`
  return `$${value.toFixed(0)}`
}

function pct(value: number | null) {
  return value == null ? '—' : `${(value * 100).toFixed(3)}%`
}

function scoreLabel(value: number | null) {
  return value == null ? '未接入' : value.toFixed(0)
}

function finiteCostValue(key: string) {
  const value = cost.value[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Market Regime</p>
        <h2>市场是否适合网格</h2>
        <p>评分用于决定策略能否运行，不代表价格涨跌概率。</p>
      </div>
      <StatusBadge
        :tone="currentRegime?.allowed ? 'good' : 'warning'"
        :label="verdictLabel"
      />
    </section>

    <div class="context-toolbar">
      <label class="compact-select">
        <span>分析标的</span>
        <select v-model="selectedSymbol">
          <option v-for="symbol in availableSymbols" :key="symbol" :value="symbol">{{ symbol }}</option>
        </select>
      </label>
      <button class="button button--secondary" type="button" :disabled="loadingHistory || !selectedSymbol" @click="refreshHistory">
        <RefreshCw :size="16" :class="{ spin: loadingHistory }" />
        刷新历史
      </button>
      <span class="muted">{{ history.length }} 个真实决策快照</span>
    </div>

    <div v-if="historyError" class="inline-alert inline-alert--warning" role="status">
      <TriangleAlert :size="18" />
      <span><strong>历史暂不可用</strong>{{ historyError }}；当前快照仍可查看。</span>
    </div>

    <div class="content-grid content-grid--market">
      <section class="panel regime-score-panel" aria-labelledby="regime-score-title">
        <div class="score-ring" :style="{ '--score': currentRegime?.gridScore || 0 }">
          <div>
            <strong>{{ currentRegime?.gridScore.toFixed(0) || '—' }}</strong>
            <span>/ 100</span>
          </div>
        </div>
        <div class="score-copy">
          <p class="eyebrow">综合评分</p>
          <h2 id="regime-score-title">{{ marketStateLabel }}</h2>
          <p v-if="currentRegime">
            {{ currentRegime.allowed
              ? '评分达到进入阈值，仍需通过风险预算和库存检查。'
              : `${verdictLabel}；系统保持扫描，不会新增网格风险。` }}
          </p>
          <p v-else>交易进程产生市场状态快照后，这里会显示可解释的评分。</p>
          <dl class="metadata-list">
            <div><dt>数据时间</dt><dd>{{ currentRegime?.asOfTime || '—' }}</dd></div>
            <div><dt>模型版本</dt><dd>{{ currentRegime?.modelVersion || '—' }}</dd></div>
            <div><dt>准入结果</dt><dd>{{ verdictLabel }}</dd></div>
            <div><dt>准入门槛</dt><dd>{{ currentRegime?.thresholdUsed?.toFixed(1) || '—' }}</dd></div>
            <div><dt>数据健康</dt><dd>{{ dashboard.dataHealth }}</dd></div>
          </dl>
        </div>
      </section>

      <section class="panel" aria-labelledby="component-score-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">评分拆解</p>
            <h2 id="component-score-title">每一分从哪里来</h2>
          </div>
        </div>
        <div v-if="componentRows.length" class="score-bars">
          <div v-for="row in componentRows" :key="row.key" class="score-bar">
            <div><span>{{ row.label }}</span><strong>{{ scoreLabel(row.value) }}</strong></div>
            <div class="progress-track"><span :style="{ width: `${row.value || 0}%` }" /></div>
            <small v-if="row.value == null">数据源未接入，本维度不计分</small>
            <small v-else>有效权重 {{ (row.weight * 100).toFixed(1) }}% · 贡献 {{ row.contribution.toFixed(2) }} 分</small>
          </div>
        </div>
        <div v-else class="empty-state empty-state--compact">
          <Clock3 :size="26" />
          <p>等待特征引擎生成子分数</p>
        </div>
      </section>
    </div>

    <section v-if="currentRegime" class="panel" aria-labelledby="cost-economics-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">交易经济性</p>
          <h2 id="cost-economics-title">计划格距减去全部预计成本</h2>
        </div>
        <StatusBadge
          :tone="['BLOCKED_COST', 'BLOCKED_ECONOMICS'].includes(currentRegime.verdict) ? 'danger' : 'neutral'"
          :label="costEvaluationLabel"
        />
      </div>
      <div class="metric-grid metric-grid--cost">
        <div><span>计划格距</span><strong>{{ pct(finiteCostValue('planned_step_pct')) }}</strong></div>
        <div><span>Maker 往返费</span><strong>{{ pct(finiteCostValue('maker_round_trip_pct')) }}</strong></div>
        <div><span>逆向选择缓冲</span><strong>{{ pct(finiteCostValue('adverse_selection_pct')) }}</strong></div>
        <div><span>滑点缓冲</span><strong>{{ pct(finiteCostValue('slippage_pct')) }}</strong></div>
        <div><span>安全边际</span><strong>{{ pct(finiteCostValue('safety_margin_pct')) }}</strong></div>
        <div><span>预计资金费</span><strong>{{ pct(finiteCostValue('projected_funding_pct')) }}</strong></div>
        <div><span>预计总成本</span><strong>{{ pct(finiteCostValue('total_cost_pct')) }}</strong></div>
        <div><span>净边际</span><strong>{{ pct(finiteCostValue('net_edge_pct')) }}</strong></div>
      </div>
    </section>

    <div class="content-grid">
      <section class="panel" aria-labelledby="decision-reasons-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">解释</p>
            <h2 id="decision-reasons-title">为什么允许或禁止</h2>
          </div>
        </div>
        <div class="reason-columns">
          <div>
            <h3><CheckCircle2 :size="18" />判断依据</h3>
            <ul v-if="currentRegime?.reasons.length" class="plain-list">
              <li v-for="reason in currentRegime.reasons" :key="reason">{{ reason }}</li>
            </ul>
            <p v-else class="muted">暂无解释数据。</p>
          </div>
          <div>
            <h3><ShieldX :size="18" />硬阻断</h3>
            <ul v-if="currentRegime?.hardBlocks.length" class="plain-list plain-list--danger">
              <li v-for="block in currentRegime.hardBlocks" :key="block">{{ block }}</li>
            </ul>
            <p v-else class="muted">当前没有硬阻断。</p>
          </div>
        </div>
      </section>

      <section class="panel" aria-labelledby="regime-history-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">最近 24 小时</p>
            <h2 id="regime-history-title">评分变化</h2>
          </div>
        </div>
        <MiniLineChart :values="scoreHistory" label="市场网格评分" />
        <div v-if="history.length" class="history-summary">
          <span>起点 {{ history[0].gridScore.toFixed(0) }}</span>
          <span>当前 {{ history[history.length - 1].gridScore.toFixed(0) }}</span>
          <span>最低 {{ Math.min(...scoreHistory).toFixed(0) }}</span>
          <span>最高 {{ Math.max(...scoreHistory).toFixed(0) }}</span>
        </div>
        <p class="panel-note">
          曲线只使用数据库中的真实决策快照；单点不会伪造成趋势。
        </p>
      </section>
    </div>

    <section class="panel" aria-labelledby="candidate-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">候选标的</p>
          <h2 id="candidate-title">流动性与波动筛选</h2>
        </div>
        <span class="muted">{{ candidates.length }} 个候选</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>排名</th>
              <th>标的</th>
              <th>筛选状态</th>
              <th>综合分</th>
              <th>24h 成交额</th>
              <th>前档深度</th>
              <th>点差</th>
              <th>当前波动</th>
              <th>数据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in candidates" :key="`${item.rank}-${item.symbol}`">
              <td>{{ item.rank }}</td>
              <td><strong>{{ item.symbol }}</strong></td>
              <td>
                <StatusBadge
                  :tone="item.selected ? 'good' : item.disabled ? 'danger' : 'neutral'"
                  :label="item.selected ? '已选择' : item.disabled ? '已禁用' : item.status || '候选'"
                />
              </td>
              <td>{{ item.score?.toFixed(1) || '—' }}</td>
              <td>{{ money(item.volume24h) }}</td>
              <td>{{ money(item.depthUsdt) }}</td>
              <td>{{ pct(item.spreadPct) }}</td>
              <td>{{ pct(item.currentVolatility) }}</td>
              <td>
                <div class="data-evidence">
                  <span class="inline-status" :class="{ 'inline-status--danger': item.dataStale }">
                    <TriangleAlert v-if="item.dataStale" :size="15" />
                    {{ item.dataStale ? '过期' : item.klineQualityStatus || '新鲜' }}
                  </span>
                  <small>Binance Futures REST + WebSocket</small>
                  <small>末根 {{ item.lastKlineCloseAt || '—' }} · 年龄 {{ item.klineAgeSeconds == null ? '—' : `${Math.round(item.klineAgeSeconds)}s` }}</small>
                  <small>缺口 {{ item.klineMissingCount ?? '—' }} · {{ item.blockCode || '无错误码' }}</small>
                </div>
              </td>
            </tr>
            <tr v-if="!candidates.length">
              <td colspan="9"><div class="empty-inline">等待下一次候选扫描</div></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.score-bar small {
  color: var(--text-muted, #94a3b8);
  font-size: 0.75rem;
}

.metric-grid--cost > div {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
  padding: 0.85rem;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 0.65rem;
  background: rgba(15, 23, 42, 0.28);
}

.metric-grid--cost span {
  color: var(--text-muted, #94a3b8);
  font-size: 0.78rem;
}

.metric-grid--cost strong {
  font-variant-numeric: tabular-nums;
}

.data-evidence {
  display: grid;
  gap: 0.15rem;
  min-width: 13rem;
}

.data-evidence small {
  color: var(--text-muted, #94a3b8);
  white-space: nowrap;
}
</style>
