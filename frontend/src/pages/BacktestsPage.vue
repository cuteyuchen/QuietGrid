<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  Database,
  FlaskConical,
  Play,
  RefreshCw,
} from '@lucide/vue'
import {
  loadV2BacktestDatasets,
  loadV2BacktestDetail,
  loadV2Backtests,
  startV2Backtest,
  type V2BacktestDataset,
  type V2BacktestDetail,
  type V2BacktestRun,
} from '../api'
import MetricCard from '../components/MetricCard.vue'
import MiniLineChart from '../components/MiniLineChart.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{
  accountId: string
}>()

const datasets = ref<V2BacktestDataset[]>([])
const runs = ref<V2BacktestRun[]>([])
const selected = ref<V2BacktestDetail | null>(null)
const loading = ref(false)
const running = ref(false)
const error = ref('')
const form = ref({
  dataset: '',
  symbol: 'BTCUSDT',
  observeRows: 30,
  capital: 200,
  leverage: 1,
  makerFeeRate: 0,
  fillModel: 'L0_CONSERVATIVE',
})

const equityValues = computed(() => selected.value?.report?.equity_curve
  ?.map((point) => Number(point.equity || 0)) || [])
const drawdownValues = computed(() => selected.value?.report?.equity_curve
  ?.map((point) => Number(point.drawdown || 0)) || [])
const fills = computed(() => selected.value?.report?.fills || [])
const validation = computed(() => selected.value?.report?.validation)
const walkForward = computed(() => validation.value?.walk_forward)
const monteCarlo = computed(() => validation.value?.monte_carlo)

onMounted(refresh)
watch(() => props.accountId, refresh)

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const [datasetItems, runItems] = await Promise.all([
      loadV2BacktestDatasets(props.accountId),
      loadV2Backtests(props.accountId),
    ])
    datasets.value = datasetItems
    runs.value = runItems
    if (!form.value.dataset && datasetItems.length) {
      form.value.dataset = datasetItems[0].relativePath
    }
    if (selected.value) {
      const match = runItems.find((item) => item.runId === selected.value?.runId)
      if (!match) selected.value = null
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法加载回测中心'
  } finally {
    loading.value = false
  }
}

async function runBacktest() {
  if (!form.value.dataset || running.value) return
  running.value = true
  error.value = ''
  try {
    selected.value = await startV2Backtest({ ...form.value }, props.accountId)
    await refresh()
    if (selected.value?.runId) {
      selected.value = await loadV2BacktestDetail(selected.value.runId, props.accountId)
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '回测运行失败'
  } finally {
    running.value = false
  }
}

async function openRun(run: V2BacktestRun) {
  loading.value = true
  error.value = ''
  try {
    selected.value = await loadV2BacktestDetail(run.runId, props.accountId)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取回测报告'
  } finally {
    loading.value = false
  }
}

function metric(name: string) {
  const raw = selected.value?.metrics[name]
  return typeof raw === 'number' ? raw : Number(raw || 0)
}

function money(value: number) {
  return `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(2)}`
}

function pct(value: number) {
  return `${(value * 100).toFixed(1)}%`
}

function time(value: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Backtest Lab</p>
        <h2>回测与策略验证</h2>
        <p>回测用于排除未来函数、成本遗漏和尾部风险，不用于承诺收益。</p>
      </div>
      <button class="button button--secondary" type="button" :disabled="loading" @click="refresh">
        <RefreshCw :size="17" :class="{ spin: loading }" />
        刷新
      </button>
    </section>

    <div v-if="error" class="inline-alert inline-alert--danger" role="alert">
      <AlertTriangle :size="20" />
      <span><strong>回测中心暂不可用</strong>{{ error }}</span>
    </div>

    <section class="panel" aria-labelledby="new-backtest-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">新实验</p>
          <h2 id="new-backtest-title">运行保守回测</h2>
          <p>信号在下一可交易时点执行；同 Bar 冲突优先按止损处理。</p>
        </div>
        <StatusBadge tone="info" label="样本外标记需人工确认" />
      </div>
      <form class="backtest-form" @submit.prevent="runBacktest">
        <label class="field field--wide">
          <span>历史数据集</span>
          <select v-model="form.dataset" required>
            <option value="" disabled>选择 data/backtests 中的 CSV</option>
            <option v-for="dataset in datasets" :key="dataset.relativePath" :value="dataset.relativePath">
              {{ dataset.name }} · {{ (dataset.sizeBytes / 1024).toFixed(1) }} KB
            </option>
          </select>
          <small v-if="!datasets.length">将 CSV 放入 data/backtests 后即可选择。</small>
        </label>
        <label class="field">
          <span>标的</span>
          <input v-model.trim="form.symbol" type="text" required autocomplete="off">
        </label>
        <label class="field">
          <span>观察期 K 线数</span>
          <input v-model.number="form.observeRows" type="number" min="30" step="1" required>
        </label>
        <label class="field">
          <span>测试资金（USDT）</span>
          <input v-model.number="form.capital" type="number" min="1" step="1" required>
        </label>
        <label class="field">
          <span>杠杆</span>
          <input v-model.number="form.leverage" type="number" min="1" max="2" step="0.1" required>
          <small>研究默认 1 倍，上限 2 倍。</small>
        </label>
        <label class="field">
          <span>Maker 费率</span>
          <input v-model.number="form.makerFeeRate" type="number" min="0" max="0.01" step="0.00001" required>
        </label>
        <label class="field">
          <span>成交模型</span>
          <select v-model="form.fillModel">
            <option value="L0_CONSERVATIVE">L0 保守 K 线模型</option>
          </select>
        </label>
        <button class="button button--primary backtest-submit" type="submit" :disabled="running || !form.dataset">
          <Play :size="17" />
          {{ running ? '正在回测…' : '开始回测' }}
        </button>
      </form>
    </section>

    <div class="research-layout">
      <section class="panel run-list-panel" aria-labelledby="run-list-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">实验记录</p>
            <h2 id="run-list-title">历史回测</h2>
          </div>
          <span class="muted">{{ runs.length }} 次</span>
        </div>
        <div v-if="runs.length" class="run-list">
          <button
            v-for="run in runs"
            :key="run.runId"
            type="button"
            :class="{ active: selected?.runId === run.runId }"
            @click="openRun(run)"
          >
            <span>
              <strong>{{ run.symbol }}</strong>
              <StatusBadge :tone="run.status === 'COMPLETED' ? 'good' : run.status === 'FAILED' ? 'danger' : 'warning'" :label="run.status" />
            </span>
            <small>{{ time(run.startedAt) }} · {{ run.fillModel }}</small>
            <b :class="Number(run.metrics.total_pnl || 0) >= 0 ? 'positive' : 'negative'">
              {{ money(Number(run.metrics.total_pnl || 0)) }}
            </b>
            <ChevronRight :size="18" />
          </button>
        </div>
        <div v-else class="empty-state">
          <FlaskConical :size="30" />
          <h3>还没有回测记录</h3>
          <p>选择数据集并运行第一个保守回测。</p>
        </div>
      </section>

      <section class="panel report-panel" aria-labelledby="report-title">
        <template v-if="selected">
          <div class="panel__header">
            <div>
              <p class="eyebrow">回测报告</p>
              <h2 id="report-title">{{ selected.symbol }} · {{ selected.runId.slice(0, 8) }}</h2>
              <p>{{ selected.fillModel }} · 参数 {{ selected.parameterVersion || '未标记' }}</p>
            </div>
            <StatusBadge
              :tone="selected.status === 'COMPLETED' ? 'good' : 'warning'"
              :label="selected.status"
            />
          </div>

          <div class="validation-banner">
            <Database :size="20" />
            <span>
              <strong>数据与模型声明</strong>
              当前报告默认视为开发/验证结果；只有冻结参数后未触碰的数据区间才能标记为样本外。
            </span>
          </div>

          <div class="metric-grid metric-grid--report">
            <MetricCard label="净收益" :value="money(metric('total_pnl'))" :tone="metric('total_pnl') >= 0 ? 'good' : 'danger'" />
            <MetricCard label="最大回撤" :value="money(metric('max_drawdown'))" tone="warning" />
            <MetricCard label="Profit Factor" :value="metric('profit_factor').toFixed(2)" />
            <MetricCard label="胜率" :value="pct(metric('win_rate'))" />
            <MetricCard label="成交数" :value="metric('fills').toFixed(0)" />
            <MetricCard label="库存 P99" :value="pct(metric('inventory_p99'))" />
          </div>

          <div class="chart-block">
            <div class="panel__header">
              <div><h3>权益曲线</h3><p>已实现与保守未实现盈亏合计</p></div>
            </div>
            <MiniLineChart :values="equityValues" label="回测权益曲线" :tone="metric('total_pnl') >= 0 ? 'good' : 'danger'" />
          </div>
          <div class="chart-block">
            <div class="panel__header">
              <div><h3>回撤曲线</h3><p>越高代表离历史峰值越远</p></div>
            </div>
            <MiniLineChart :values="drawdownValues" label="回测回撤曲线" tone="danger" />
          </div>

          <div class="validation-grid">
            <section class="validation-card">
              <div class="panel__header">
                <div>
                  <p class="eyebrow">Walk-Forward</p>
                  <h3>滚动样本外折</h3>
                </div>
                <StatusBadge
                  :tone="walkForward?.status === 'COMPLETED' ? 'good' : 'warning'"
                  :label="walkForward?.status || '未运行'"
                />
              </div>
              <dl class="metadata-grid">
                <div><dt>折数</dt><dd>{{ walkForward?.fold_count || 0 }}</dd></div>
                <div><dt>盈利折比例</dt><dd>{{ pct(Number(walkForward?.profitable_fold_ratio || 0)) }}</dd></div>
                <div><dt>最差折盈亏</dt><dd>{{ money(Number(walkForward?.worst_fold_pnl || 0)) }}</dd></div>
                <div><dt>最差折回撤</dt><dd>{{ money(Number(walkForward?.worst_fold_drawdown || 0)) }}</dd></div>
              </dl>
            </section>

            <section class="validation-card">
              <div class="panel__header">
                <div>
                  <p class="eyebrow">Monte Carlo</p>
                  <h3>尾部结果分布</h3>
                </div>
                <StatusBadge
                  :tone="monteCarlo?.status === 'COMPLETED' ? 'good' : 'warning'"
                  :label="`${monteCarlo?.simulations || 0} 次`"
                />
              </div>
              <dl class="metadata-grid">
                <div><dt>P05 总盈亏</dt><dd>{{ money(Number(monteCarlo?.total_pnl_p05 || 0)) }}</dd></div>
                <div><dt>P50 总盈亏</dt><dd>{{ money(Number(monteCarlo?.total_pnl_p50 || 0)) }}</dd></div>
                <div><dt>亏损概率</dt><dd>{{ pct(Number(monteCarlo?.loss_probability || 0)) }}</dd></div>
                <div><dt>P99 最大回撤</dt><dd>{{ money(Number(monteCarlo?.max_drawdown_p99 || 0)) }}</dd></div>
              </dl>
            </section>
          </div>

          <div v-if="validation?.warning" class="inline-alert">
            <AlertTriangle :size="19" />
            {{ validation.warning }}
          </div>

          <details class="disclosure">
            <summary><BarChart3 :size="18" />查看 {{ fills.length }} 条模拟成交</summary>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Bar</th><th>时间</th><th>方向</th><th>格位</th><th>价格</th><th>数量</th><th>网格利润</th></tr></thead>
                <tbody>
                  <tr v-for="(fill, index) in fills" :key="index">
                    <td>{{ fill.bar_index }}</td>
                    <td>{{ fill.timestamp || '—' }}</td>
                    <td>{{ fill.side }}</td>
                    <td>#{{ fill.grid_index }}</td>
                    <td>{{ Number(fill.price || 0).toFixed(4) }}</td>
                    <td>{{ Number(fill.qty || 0).toFixed(6) }}</td>
                    <td>{{ money(Number(fill.grid_pnl || 0)) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </template>
        <div v-else class="empty-state">
          <CheckCircle2 :size="32" />
          <h2 id="report-title">选择一份回测报告</h2>
          <p>报告会展示净收益、最大回撤、成交模型、权益曲线与库存尾部指标。</p>
        </div>
      </section>
    </div>
  </div>
</template>
