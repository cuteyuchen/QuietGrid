<script setup lang="ts">
import type { LiquidityCandidate } from '../../mock'

defineProps<{
  candidates: LiquidityCandidate[]
  limit?: number
}>()

function pct(value: number | null | undefined, digits = 2) {
  if (value == null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function klineLabel(item: LiquidityCandidate) {
  if (item.klineActualCount == null && item.klineRequiredCount == null) return '—'
  return `${item.klineActualCount ?? '—'}/${item.klineRequiredCount ?? '—'}`
}

function ageLabel(item: LiquidityCandidate) {
  if (item.klineAgeSeconds == null || Number.isNaN(item.klineAgeSeconds)) return '—'
  return `${Math.round(item.klineAgeSeconds)}s`
}

function gridLabel(item: LiquidityCandidate) {
  const count = item.gridPreview?.gridCount
  const levels = item.gridPreview?.levelCount
  if (count == null) return '—'
  return `${count} 格 / ${levels ?? count + 1} 层`
}

function number(value: number | null | undefined, digits = 3) {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toFixed(digits)
}

function directionLabel(value: string | null | undefined) {
  if (!value) return '—'
  return {
    LONG: '做多',
    SHORT: '做空',
    NEUTRAL: '中性',
  }[String(value).toUpperCase()] || String(value)
}

function marketStateLabel(value: string | null | undefined) {
  return {
    QUIET_RANGE: '震荡区间',
    TREND_UP: '上升趋势',
    TREND_DOWN: '下降趋势',
    TREND: '趋势行情',
    VOLATILE: '高波动',
    ILLIQUID: '流动性不足',
    EVENT_RISK: '事件风险',
    UNKNOWN_DATA: '数据异常',
  }[String(value || '').toUpperCase()] || String(value || '—')
}

function resultLabel(item: LiquidityCandidate) {
  const value = item.verdict || (item.thresholdMet ? 'ALLOWED' : item.blockCode || '')
  const labels: Record<string, string> = {
    ALLOWED: '准入通过',
    BLOCKED_SCORE: '评分不足',
    BLOCKED_COST: '成本不成立',
    BLOCKED_ECONOMICS: '网格经济性不成立',
    BLOCKED_RISK: '风险预算不足',
    BLOCKED_HARD: '硬条件阻断',
    BLOCKED_DATA: '数据阻断',
  }
  if (value) return labels[value] || value
  return item.thresholdMet ? 'ALLOWED' : item.blockCode || '未通过'
}
</script>

<template>
  <div class="candidate-eval-table">
    <table>
      <thead>
        <tr>
          <th>标的</th>
          <th>阶段</th>
          <th>K线</th>
          <th>数据年龄</th>
          <th>市场状态</th>
          <th>评分</th>
          <th>方向</th>
          <th>网格方案</th>
          <th>实际格距</th>
          <th>硬成本</th>
          <th>手续费后净边际</th>
          <th>预计穿越/小时</th>
          <th>目标值</th>
          <th>每格金额/门槛</th>
          <th>本金/最低所需</th>
          <th>最坏损失/预算</th>
          <th>准入结果</th>
          <th>原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in candidates.slice(0, limit ?? 12)" :key="item.symbol">
          <td>{{ item.symbol }}</td>
          <td>{{ item.stage || '—' }}</td>
          <td>{{ klineLabel(item) }}</td>
          <td>{{ ageLabel(item) }}</td>
          <td>{{ marketStateLabel(item.marketState) }}</td>
          <td>{{ item.regimeScore == null ? '—' : item.regimeScore.toFixed(0) }}</td>
          <td>{{ directionLabel(item.economics?.directionMode) }}</td>
          <td>{{ gridLabel(item) }}</td>
          <td>{{ pct(item.economics?.grossStepPct, 3) }}</td>
          <td
            :title="item.economics?.makerFeeSource
              ? `Maker 费率来源：${item.economics.makerFeeSource}`
              : ''"
          >
            {{ pct(item.economics?.hardCostPct, 3) }}
          </td>
          <td>{{ pct(item.economics?.feeNetEdgePct, 3) }}</td>
          <td>{{ number(item.economics?.estimatedCrossingsPerHour, 2) }}</td>
          <td>{{ number(item.economics?.objectiveValue, 5) }}</td>
          <td>
            {{ number(item.economics?.plannedMinOrderNotional, 2) }}
            /
            {{ number(item.economics?.minimumOrderNotional, 2) }} USDT
          </td>
          <td>
            {{ number(item.economics?.configuredCapital, 2) }}
            /
            {{ number(item.economics?.minimumRequiredCapital, 2) }} USDT
          </td>
          <td>
            {{ number(item.economics?.worstCaseStopLoss, 2) }}
            /
            {{ number(item.economics?.riskBudget, 2) }} USDT
          </td>
          <td>
            <span
              class="verdict"
              :class="{ allowed: item.thresholdMet, blocked: !item.thresholdMet }"
            >
              {{ resultLabel(item) }}
            </span>
          </td>
          <td>{{ item.economics?.rejectedReason || item.blockCode || item.error || '—' }}</td>
        </tr>
        <tr v-if="!candidates.length">
          <td colspan="18">暂无候选评估</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.candidate-eval-table {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
th,
td {
  text-align: left;
  padding: 0.45rem 0.5rem;
  border-bottom: 1px solid rgba(148, 163, 184, 0.12);
  white-space: nowrap;
}
th {
  opacity: 0.75;
  font-weight: 600;
}
.verdict {
  display: inline-flex;
  min-height: 1.5rem;
  align-items: center;
  border-radius: 999px;
  padding: 0 0.5rem;
  font-size: 0.75rem;
  font-weight: 700;
}
.verdict.allowed {
  color: #6ee7b7;
  background: rgba(16, 185, 129, 0.12);
}
.verdict.blocked {
  color: #fbbf24;
  background: rgba(245, 158, 11, 0.12);
}
</style>
