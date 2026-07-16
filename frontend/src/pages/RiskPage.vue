<script setup lang="ts">
import { computed } from 'vue'
import {
  Ban,
  CheckCircle2,
  CirclePause,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from '@lucide/vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { V2DashboardData } from '../api'
import type { ControlState } from '../mock'

const props = defineProps<{
  dashboard: V2DashboardData
  control: ControlState
}>()

const emit = defineEmits<{
  action: [action: string]
}>()

const budgetPercent = computed(() => {
  if (props.dashboard.windowLossBudget <= 0) return 0
  return Math.max(0, Math.min(
    100,
    (1 - props.dashboard.windowLossBudgetRemaining / props.dashboard.windowLossBudget) * 100,
  ))
})

const inventoryPercent = computed(() => Math.max(
  0,
  Math.min(100, (props.dashboard.latestInventory?.utilization || 0) * 100),
))

const sessionLossBudget = computed(
  () => props.dashboard.equity * props.dashboard.riskPolicy.max_session_loss_pct,
)
const symbolInventoryCap = computed(
  () => props.dashboard.equity * props.dashboard.riskPolicy.max_symbol_inventory_pct,
)
const groupNotionalCap = computed(
  () => props.dashboard.equity * props.dashboard.riskPolicy.max_group_notional_pct,
)

const recoveryChecks = computed(() => [
  {
    label: '市场状态重新允许',
    ok: Boolean(props.dashboard.latestRegime?.allowed),
    detail: props.dashboard.latestRegime
      ? `当前评分 ${props.dashboard.latestRegime.gridScore.toFixed(0)}`
      : '等待评分',
  },
  {
    label: '数据保持新鲜',
    ok: !['STALE', 'ERROR', 'UNHEALTHY'].includes(props.dashboard.dataHealth.toUpperCase()),
    detail: props.dashboard.dataHealth,
  },
  {
    label: '窗口仍有损失预算',
    ok: props.dashboard.windowLossBudgetRemaining > 0,
    detail: `剩余 ${money(props.dashboard.windowLossBudgetRemaining)}`,
  },
  {
    label: '库存未达到高风险',
    ok: inventoryPercent.value < 70
      && !['HIGH', 'CRITICAL'].includes((props.dashboard.latestInventory?.riskLevel || '').toUpperCase()),
    detail: `利用率 ${inventoryPercent.value.toFixed(0)}% · ${props.dashboard.latestInventory?.riskLevel || '暂无库存'}`,
  },
  {
    label: '窗口止损次数未熔断',
    ok: props.dashboard.riskPolicy.max_window_stop_count <= 0
      || props.dashboard.windowStopCount < props.dashboard.riskPolicy.max_window_stop_count,
    detail: props.dashboard.riskPolicy.max_window_stop_count > 0
      ? `${props.dashboard.windowStopCount} / ${props.dashboard.riskPolicy.max_window_stop_count}`
      : '未配置次数上限',
  },
])

function money(value: number | null | undefined) {
  return value == null ? '—' : `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(2)}`
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Risk Center</p>
        <h2>风险预算与熔断器</h2>
        <p>这里展示最坏情况和操作边界，而不是用盈利掩盖风险。</p>
      </div>
      <StatusBadge
        :tone="dashboard.globalRiskLevel === 'LOW' ? 'good' : dashboard.globalRiskLevel === 'CRITICAL' ? 'danger' : 'warning'"
        :label="`全局风险 ${dashboard.globalRiskLevel}`"
      />
    </section>

    <div class="content-grid content-grid--risk">
      <section class="panel" aria-labelledby="budget-tree-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">预算树</p>
            <h2 id="budget-tree-title">亏损到哪里会停</h2>
          </div>
        </div>
        <div class="budget-tree">
          <div class="budget-node budget-node--root">
            <span>账户权益</span>
            <strong>{{ money(dashboard.equity) }}</strong>
          </div>
          <div class="budget-branch" />
          <div class="budget-node">
            <span>周末窗口损失预算</span>
            <strong>{{ money(dashboard.windowLossBudget) }}</strong>
            <div class="progress-track progress-track--large">
              <span :style="{ width: `${budgetPercent}%` }" />
            </div>
            <small>已用 {{ budgetPercent.toFixed(0) }}% · 剩余 {{ money(dashboard.windowLossBudgetRemaining) }}</small>
          </div>
          <div class="budget-children">
            <div class="budget-node budget-node--child">
              <span>单会话损失预算</span>
              <strong>{{ money(sessionLossBudget) }}</strong>
              <small>
                {{ dashboard.latestRisk?.sessionId ? `#${dashboard.latestRisk.sessionId}` : '暂无会话' }}
                · 当前 {{ money(dashboard.latestRisk?.sessionPnl) }}
              </small>
            </div>
            <div class="budget-node budget-node--child">
              <span>单标的库存名义上限</span>
              <strong>{{ money(symbolInventoryCap) }}</strong>
              <small>当前利用率 {{ inventoryPercent.toFixed(0) }}%</small>
            </div>
            <div class="budget-node budget-node--child">
              <span>相关性组名义上限</span>
              <strong>{{ money(groupNotionalCap) }}</strong>
              <small>按账户权益统一约束同类风险敞口</small>
            </div>
            <div class="budget-node budget-node--child">
              <span>有效杠杆上限</span>
              <strong>{{ dashboard.riskPolicy.effective_leverage_cap.toFixed(2) }}×</strong>
              <small>运行中只允许维持或降低风险</small>
            </div>
          </div>
        </div>
      </section>

      <section class="panel" aria-labelledby="breaker-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">Circuit Breaker</p>
            <h2 id="breaker-title">当前风控决定</h2>
          </div>
          <StatusBadge
            :tone="dashboard.latestRisk?.action === 'ALLOW' ? 'good' : dashboard.latestRisk ? 'warning' : 'neutral'"
            :label="dashboard.latestRisk?.action || '等待快照'"
          />
        </div>
        <div v-if="dashboard.latestRisk" class="risk-decision">
          <span class="risk-decision__icon" :class="dashboard.latestRisk.action === 'ALLOW' ? 'good' : 'warning'">
            <ShieldCheck v-if="dashboard.latestRisk.action === 'ALLOW'" :size="25" />
            <ShieldAlert v-else :size="25" />
          </span>
          <div>
            <strong>{{ dashboard.latestRisk.reason || '风控规则已评估' }}</strong>
            <p>更新时间 {{ dashboard.latestRisk.asOfTime }}</p>
          </div>
        </div>
        <div v-else class="empty-state empty-state--compact">
          <CirclePause :size="28" />
          <p>等待第一份风险快照</p>
        </div>
        <dl class="metadata-grid">
          <div><dt>全局风险</dt><dd>{{ dashboard.globalRiskLevel }}</dd></div>
          <div><dt>本窗口盈亏</dt><dd>{{ money(dashboard.windowPnl) }}</dd></div>
          <div><dt>库存利用率</dt><dd>{{ inventoryPercent.toFixed(1) }}%</dd></div>
          <div><dt>新增风险</dt><dd>{{ control.newEntriesPaused ? '已暂停' : '允许评估' }}</dd></div>
          <div>
            <dt>窗口止损次数</dt>
            <dd>
              {{ dashboard.windowStopCount }}
              / {{ dashboard.riskPolicy.max_window_stop_count || '∞' }}
            </dd>
          </div>
          <div>
            <dt>连续亏损上限</dt>
            <dd>{{ dashboard.riskPolicy.max_consecutive_session_losses || '未配置' }}</dd>
          </div>
          <div>
            <dt>周末预算比例</dt>
            <dd>{{ (dashboard.riskPolicy.max_weekend_loss_pct * 100).toFixed(2) }}%</dd>
          </div>
          <div>
            <dt>风险热更新</dt>
            <dd>{{ dashboard.riskPolicy.block_risk_increase_hot_reload ? '禁止提高' : '允许' }}</dd>
          </div>
        </dl>
      </section>
    </div>

    <section class="panel" aria-labelledby="recovery-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">恢复条件</p>
          <h2 id="recovery-title">不是冷却结束就自动重启</h2>
        </div>
      </div>
      <div class="recovery-grid">
        <div v-for="check in recoveryChecks" :key="check.label" class="recovery-check" :class="{ passed: check.ok }">
          <CheckCircle2 v-if="check.ok" :size="20" />
          <TriangleAlert v-else :size="20" />
          <div><strong>{{ check.label }}</strong><span>{{ check.detail }}</span></div>
        </div>
      </div>
    </section>

    <section class="panel danger-zone" aria-labelledby="risk-actions-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">受控操作</p>
          <h2 id="risk-actions-title">降低风险</h2>
          <p>网页只提交命令；交易进程会重新校验并记录审计。</p>
        </div>
      </div>
      <div class="risk-actions">
        <button
          v-if="control.newEntriesPaused"
          class="action-tile"
          type="button"
          @click="emit('action', 'resume')"
        >
          <CheckCircle2 :size="22" />
          <span><strong>请求恢复新开仓</strong><small>仅在全部恢复条件通过时执行</small></span>
        </button>
        <button v-else class="action-tile" type="button" @click="emit('action', 'pause')">
          <Ban :size="22" />
          <span><strong>暂停所有新开仓</strong><small>保留现有仓位的风控与退出</small></span>
        </button>
        <button class="action-tile action-tile--danger" type="button" @click="emit('action', 'stop-all')">
          <ShieldAlert :size="22" />
          <span><strong>请求关闭全部会话</strong><small>将由交易进程逐会话安全退出</small></span>
        </button>
        <button class="action-tile action-tile--danger" type="button" @click="emit('action', 'safety-sweep')">
          <Trash2 :size="22" />
          <span><strong>安全清扫</strong><small>撤单、平仓并核对残留</small></span>
        </button>
      </div>
    </section>
  </div>
</template>
