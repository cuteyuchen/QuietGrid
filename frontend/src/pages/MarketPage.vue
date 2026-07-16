<script setup lang="ts">
import { computed } from 'vue'
import { CheckCircle2, Clock3, ShieldX, TriangleAlert } from '@lucide/vue'
import MiniLineChart from '../components/MiniLineChart.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { V2DashboardData } from '../api'
import type { LiquidityCandidate } from '../mock'

const props = defineProps<{
  dashboard: V2DashboardData
  candidates: LiquidityCandidate[]
}>()

const componentLabels: Record<string, string> = {
  volatility: '波动率',
  trend: '趋势强度',
  liquidity: '流动性',
  mean_reversion: '均值回归',
  cost: '交易成本',
  event: '事件风险',
}

const componentRows = computed(() => Object.entries(
  props.dashboard.latestRegime?.componentScores || {},
).map(([key, value]) => ({
  key,
  label: componentLabels[key] || key,
  value: Math.max(0, Math.min(100, Number(value || 0))),
})))

const syntheticHistory = computed(() => {
  const score = props.dashboard.latestRegime?.gridScore
  if (score == null) {
    return []
  }
  return [score]
})

function money(value: number | null) {
  if (value == null) return '—'
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`
  return `$${value.toFixed(0)}`
}

function pct(value: number | null) {
  return value == null ? '—' : `${(value * 100).toFixed(3)}%`
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
        :tone="dashboard.latestRegime?.allowed ? 'good' : 'warning'"
        :label="dashboard.latestRegime?.allowed ? '允许网格' : '暂不允许'"
      />
    </section>

    <div class="content-grid content-grid--market">
      <section class="panel regime-score-panel" aria-labelledby="regime-score-title">
        <div class="score-ring" :style="{ '--score': dashboard.latestRegime?.gridScore || 0 }">
          <div>
            <strong>{{ dashboard.latestRegime?.gridScore.toFixed(0) || '—' }}</strong>
            <span>/ 100</span>
          </div>
        </div>
        <div class="score-copy">
          <p class="eyebrow">综合评分</p>
          <h2 id="regime-score-title">{{ dashboard.latestRegime?.state || '等待首个快照' }}</h2>
          <p v-if="dashboard.latestRegime">
            {{ dashboard.latestRegime.allowed
              ? '评分达到进入阈值，仍需通过风险预算和库存检查。'
              : '至少一个条件未满足，系统不会新增网格风险。' }}
          </p>
          <p v-else>交易进程产生市场状态快照后，这里会显示可解释的评分。</p>
          <dl class="metadata-list">
            <div><dt>数据时间</dt><dd>{{ dashboard.latestRegime?.asOfTime || '—' }}</dd></div>
            <div><dt>模型版本</dt><dd>{{ dashboard.latestRegime?.modelVersion || '—' }}</dd></div>
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
            <div><span>{{ row.label }}</span><strong>{{ row.value.toFixed(0) }}</strong></div>
            <div class="progress-track"><span :style="{ width: `${row.value}%` }" /></div>
          </div>
        </div>
        <div v-else class="empty-state empty-state--compact">
          <Clock3 :size="26" />
          <p>等待特征引擎生成子分数</p>
        </div>
      </section>
    </div>

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
            <ul v-if="dashboard.latestRegime?.reasons.length" class="plain-list">
              <li v-for="reason in dashboard.latestRegime.reasons" :key="reason">{{ reason }}</li>
            </ul>
            <p v-else class="muted">暂无解释数据。</p>
          </div>
          <div>
            <h3><ShieldX :size="18" />硬阻断</h3>
            <ul v-if="dashboard.latestRegime?.hardBlocks.length" class="plain-list plain-list--danger">
              <li v-for="block in dashboard.latestRegime.hardBlocks" :key="block">{{ block }}</li>
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
        <MiniLineChart :values="syntheticHistory" label="市场网格评分" />
        <p class="panel-note">
          历史曲线会在积累多个 Regime 快照后自动出现；单点不会伪造成趋势。
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
                <span class="inline-status" :class="{ 'inline-status--danger': item.dataStale }">
                  <TriangleAlert v-if="item.dataStale" :size="15" />
                  {{ item.dataStale ? '过期' : '新鲜' }}
                </span>
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
