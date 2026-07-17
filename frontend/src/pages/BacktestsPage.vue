<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  Database,
  Download,
  FlaskConical,
  Play,
  RefreshCw,
  Settings2,
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
import BacktestDataPanel from '../components/backtest/BacktestDataPanel.vue'

const props = defineProps<{
  accountId: string
}>()

const datasets = ref<V2BacktestDataset[]>([])
const runs = ref<V2BacktestRun[]>([])
const selected = ref<V2BacktestDetail | null>(null)
const reportTab = ref<'overview' | 'validation' | 'execution'>('overview')
const loading = ref(false)
const running = ref(false)
const error = ref('')
const form = ref({
  dataset: '',
  datasetId: '',
  symbol: 'BTCUSDT',
  observeRows: 30,
  capital: 200,
  leverage: 1,
  makerFeeRate: 0,
  fillModel: 'L0_CONSERVATIVE',
  makerFillProbability: 0.65,
  maxFillsPerBar: 2,
  takerFeeRate: 0.0005,
  stopSlippageBps: 10,
  fundingRatePerBar: 0,
  walkForwardTestRows: 12,
  monteCarloSimulations: 1000,
  monteCarloMissingFillProbability: 0.10,
  monteCarloLossMultiplier: 1.25,
  distributionWindowRows: 60,
  sampleLabel: 'DEVELOPMENT',
  parametersFrozen: false,
})

const equityValues = computed(() => selected.value?.report?.equity_curve
  ?.map((point) => Number(point.equity || 0)) || [])
const drawdownValues = computed(() => selected.value?.report?.equity_curve
  ?.map((point) => Number(point.drawdown || 0)) || [])
const inventoryValues = computed(() => selected.value?.report?.equity_curve
  ?.map((point) => Number(point.inventory_utilization || 0)) || [])
const fills = computed(() => selected.value?.report?.fills || [])
const validation = computed(() => selected.value?.report?.validation)
const metadata = computed(() => selected.value?.report?.metadata)
const summary = computed(() => selected.value?.report?.summary || {})
const walkForward = computed(() => validation.value?.walk_forward)
const walkForwardFolds = computed(() => walkForward.value?.folds || [])
const monteCarlo = computed(() => validation.value?.monte_carlo)
const monteCarloValues = computed(() => [
  Number(monteCarlo.value?.total_pnl_p05 || 0),
  Number(monteCarlo.value?.total_pnl_p50 || 0),
  Number(monteCarlo.value?.total_pnl_p95 || 0),
])
const sensitivity = computed(() => validation.value?.cost_sensitivity)
const windowDistribution = computed(() => validation.value?.window_distribution)
const windowValues = computed(() => windowDistribution.value?.values || [])
const selectedDataset = computed(() => datasets.value.find((item) => (
  form.value.datasetId
    ? item.datasetId === form.value.datasetId
    : !item.datasetId && item.relativePath === form.value.dataset
)) || null)
const selectedDatasetKey = computed(() => {
  if (form.value.datasetId) return `id:${form.value.datasetId}`
  return form.value.dataset ? `path:${form.value.dataset}` : ''
})
const hasDataset = computed(() => Boolean(form.value.datasetId || form.value.dataset))
const datasetReady = computed(() => Boolean(
  selectedDataset.value && isDatasetRunnable(selectedDataset.value),
))
const canRun = computed(() => hasDataset.value && datasetReady.value)

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
    const selectionStillExists = datasetItems.some(
      (item) => datasetKey(item) === selectedDatasetKey.value,
    )
    if (!hasDataset.value || !selectionStillExists) {
      const runnableDataset = datasetItems.find(isDatasetRunnable)
      if (runnableDataset) {
        selectDataset(runnableDataset)
      } else {
        form.value.dataset = ''
        form.value.datasetId = ''
      }
    }
    if (selected.value && !runItems.some((item) => item.runId === selected.value?.runId)) {
      selected.value = null
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法加载回测中心'
  } finally {
    loading.value = false
  }
}

async function runBacktest() {
  if (!canRun.value || running.value) return
  running.value = true
  error.value = ''
  try {
    selected.value = await startV2Backtest({ ...form.value }, props.accountId)
    reportTab.value = 'overview'
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

function selectDataset(dataset: V2BacktestDataset) {
  if (dataset.datasetId) {
    form.value.datasetId = dataset.datasetId
    form.value.dataset = ''
  } else {
    form.value.datasetId = ''
    form.value.dataset = dataset.relativePath
  }
  if (dataset.symbol) form.value.symbol = dataset.symbol
}

function datasetKey(dataset: V2BacktestDataset) {
  return dataset.datasetId ? `id:${dataset.datasetId}` : `path:${dataset.relativePath}`
}

function isDatasetRunnable(dataset: V2BacktestDataset) {
  if (dataset.sourceType === 'LEGACY_CSV') return true
  return ['READY', 'READY_WITH_WARNINGS'].includes(dataset.status.toUpperCase())
}

function scrollToData() {
  document.getElementById('data-preparation-title')?.scrollIntoView({ behavior: 'smooth' })
}

async function openRun(run: V2BacktestRun) {
  loading.value = true
  error.value = ''
  try {
    selected.value = await loadV2BacktestDetail(run.runId, props.accountId)
    reportTab.value = 'overview'
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取回测报告'
  } finally {
    loading.value = false
  }
}

function sampleLabel(value: unknown) {
  const labels: Record<string, string> = {
    DEVELOPMENT: '开发集',
    VALIDATION: '验证集',
    OOS_FROZEN: '冻结参数样本外',
  }
  return labels[String(value || '')] || String(value || '未标记')
}

function metric(name: string) {
  const raw = selected.value?.metrics[name]
  return typeof raw === 'number' ? raw : Number(raw || 0)
}

function summaryMetric(name: string) {
  return Number(summary.value[name] || 0)
}

function money(value: number) {
  return `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(2)}`
}

function pct(value: number, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`
}

function number(value: unknown, digits = 2) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—'
}

function time(value: string | number | null | undefined) {
  if (!value) return '—'
  const raw = String(value)
  const date = /^\d{12,}$/.test(raw) ? new Date(Number(raw)) : new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString('zh-CN', { hour12: false })
}

function downloadReport() {
  if (!selected.value) return
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob(
    [JSON.stringify(selected.value, null, 2)],
    { type: 'application/json;charset=utf-8' },
  ))
  link.download = `${selected.value.runId}.json`
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(link.href), 0)
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Backtest Lab</p>
        <h2>回测中心</h2>
        <p>从可复现数据集到专业验证报告，按一个连续流程完成；高级能力完整保留，需要时再展开。</p>
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

    <ol class="backtest-flow" aria-label="回测工作流">
      <li class="active"><span>1</span><div><strong>准备数据</strong><small>下载、上传或复用</small></div></li>
      <li :class="{ active: hasDataset }"><span>2</span><div><strong>设置参数</strong><small>策略、成交与验证</small></div></li>
      <li :class="{ active: running || runs.length }"><span>3</span><div><strong>运行实验</strong><small>保守撮合与压力验证</small></div></li>
      <li :class="{ active: selected }"><span>4</span><div><strong>阅读报告</strong><small>收益、风险与可复现性</small></div></li>
    </ol>

    <BacktestDataPanel
      :account-id="accountId"
      :datasets="datasets"
      :selected-key="selectedDatasetKey"
      :loading="loading"
      @select="selectDataset"
      @refresh="refresh"
    />

    <section class="panel" aria-labelledby="new-backtest-title">
      <div class="panel__header">
        <div class="backtest-step-heading">
          <div class="backtest-step-number" aria-hidden="true">2</div>
          <div>
          <p class="eyebrow">新实验</p>
            <h2 id="new-backtest-title">设置回测与验证参数</h2>
            <p>默认值适合第一次运行；成交、成本和验证参数完整保留在高级设置中。</p>
          </div>
        </div>
        <StatusBadge tone="info" :label="sampleLabel(form.sampleLabel)" />
      </div>

      <form class="backtest-form backtest-form--primary" @submit.prevent="runBacktest">
        <div v-if="selectedDataset" class="parameter-dataset-context field--wide">
          <Database :size="20" />
          <span>
            <small>本次实验固定使用</small>
            <strong>{{ selectedDataset.symbol || selectedDataset.name }} · {{ selectedDataset.interval || '旧版 CSV' }}</strong>
          </span>
          <span class="mono">{{ selectedDataset.datasetId || selectedDataset.relativePath }}</span>
          <button class="button button--secondary button--small" type="button" @click="scrollToData">更换数据</button>
        </div>
        <div v-if="selectedDataset && !datasetReady" class="inline-alert inline-alert--danger field--wide">
          <AlertTriangle :size="19" />
          <span><strong>当前数据集不可用于回测</strong>请展开上方质量报告查看错误，或切换到状态为“可用”的冻结数据集。</span>
        </div>
        <div v-if="!selectedDataset" class="inline-alert inline-alert--warning field--wide">
          <AlertTriangle :size="19" />
          <span><strong>还没有选择数据集</strong>请先在上方完成数据准备，参数会在选择后用于该冻结数据集。</span>
        </div>
        <label class="field">
          <span>标的</span>
          <input v-model.trim="form.symbol" type="text" required autocomplete="off" :readonly="Boolean(selectedDataset?.symbol)">
          <small>{{ selectedDataset?.symbol ? '来自冻结数据集，不可在回测时替换。' : '旧版 CSV 需要手动声明标的。' }}</small>
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
          <span>数据角色</span>
          <select v-model="form.sampleLabel" @change="form.parametersFrozen = false">
            <option value="DEVELOPMENT">开发集</option>
            <option value="VALIDATION">验证集</option>
            <option value="OOS_FROZEN">冻结参数样本外</option>
          </select>
        </label>
        <label v-if="form.sampleLabel === 'OOS_FROZEN'" class="check-field field--wide">
          <input v-model="form.parametersFrozen" type="checkbox">
          <span>我确认参数已在其他数据上冻结，当前数据此前未参与调参。</span>
        </label>

        <details class="advanced-settings field--wide">
          <summary><Settings2 :size="17" />成交、成本与验证高级参数</summary>
          <div class="backtest-advanced-grid">
            <label class="field">
              <span>成交模型</span>
              <select v-model="form.fillModel">
                <option value="L0_CONSERVATIVE">L0 保守 K 线模型</option>
              </select>
            </label>
            <label class="field">
              <span>Maker 成交概率</span>
              <input v-model.number="form.makerFillProbability" type="number" min="0" max="1" step="0.01">
            </label>
            <label class="field">
              <span>单 Bar 最大成交层数</span>
              <input v-model.number="form.maxFillsPerBar" type="number" min="1" max="20" step="1">
            </label>
            <label class="field">
              <span>Maker 费率</span>
              <input v-model.number="form.makerFeeRate" type="number" min="0" max="0.01" step="0.00001">
            </label>
            <label class="field">
              <span>Taker 费率</span>
              <input v-model.number="form.takerFeeRate" type="number" min="0" max="0.02" step="0.00001">
            </label>
            <label class="field">
              <span>止损滑点（bp）</span>
              <input v-model.number="form.stopSlippageBps" type="number" min="0" max="1000" step="1">
            </label>
            <label class="field">
              <span>每 Bar 资金费率</span>
              <input v-model.number="form.fundingRatePerBar" type="number" min="-0.01" max="0.01" step="0.000001">
            </label>
            <label class="field">
              <span>Walk-Forward 测试行数</span>
              <input v-model.number="form.walkForwardTestRows" type="number" min="5" step="1">
            </label>
            <label class="field">
              <span>Monte Carlo 次数</span>
              <input v-model.number="form.monteCarloSimulations" type="number" min="100" max="10000" step="100">
            </label>
            <label class="field">
              <span>漏掉盈利成交概率</span>
              <input v-model.number="form.monteCarloMissingFillProbability" type="number" min="0" max="1" step="0.01">
            </label>
            <label class="field">
              <span>亏损放大倍数</span>
              <input v-model.number="form.monteCarloLossMultiplier" type="number" min="1" max="10" step="0.05">
            </label>
            <label class="field">
              <span>收益分布窗口行数</span>
              <input v-model.number="form.distributionWindowRows" type="number" min="5" step="5">
            </label>
          </div>
        </details>

        <button
          class="button button--primary backtest-submit"
          type="submit"
          :disabled="running || !canRun || (form.sampleLabel === 'OOS_FROZEN' && !form.parametersFrozen)"
        >
          <Play :size="17" />
          {{ running ? '正在回测与验证…' : '开始回测' }}
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
            <small>{{ sampleLabel(run.config.sample_label) }} · {{ time(run.startedAt) }}</small>
            <small v-if="run.datasetId" class="run-dataset-ref">Dataset {{ run.datasetId.slice(0, 24) }}… · {{ run.dataProvider || '—' }}</small>
            <small v-if="run.datasetChecksum" class="mono">Hash {{ run.datasetChecksum.slice(0, 12) }}… · {{ run.windowMode || 'RAW_RANGE' }}</small>
            <small>{{ run.fillModel }} · DD {{ money(Number(run.metrics.max_drawdown || 0)) }}</small>
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
              <p>{{ sampleLabel(validation?.sample_label || selected.config.sample_label) }} · {{ selected.fillModel }}</p>
            </div>
            <div class="header-actions">
              <StatusBadge
                :tone="selected.status === 'COMPLETED' ? 'good' : 'warning'"
                :label="selected.status"
              />
              <button class="button button--secondary" type="button" @click="downloadReport">
                <Download :size="16" />报告
              </button>
            </div>
          </div>

          <div class="validation-banner">
            <Database :size="20" />
            <span>
              <strong>数据与模型声明</strong>
              {{ validation?.warning || '未提供样本角色声明。' }}
            </span>
          </div>

          <nav class="subtabs report-tabs" aria-label="回测报告分区">
            <button type="button" :class="{ active: reportTab === 'overview' }" @click="reportTab = 'overview'">概览</button>
            <button type="button" :class="{ active: reportTab === 'validation' }" @click="reportTab = 'validation'">验证</button>
            <button type="button" :class="{ active: reportTab === 'execution' }" @click="reportTab = 'execution'">成交与成本</button>
          </nav>

          <div v-if="reportTab === 'overview'" class="report-section">
            <div class="metric-grid metric-grid--report">
              <MetricCard label="净收益" :value="money(metric('total_pnl'))" :tone="metric('total_pnl') >= 0 ? 'good' : 'danger'" />
              <MetricCard label="最大回撤" :value="money(metric('max_drawdown'))" tone="warning" />
              <MetricCard label="Profit Factor" :value="metric('profit_factor').toFixed(2)" />
              <MetricCard label="胜率" :value="pct(metric('win_rate'))" />
              <MetricCard label="Sortino" :value="metric('sortino').toFixed(2)" />
              <MetricCard label="CVaR 95" :value="money(metric('cvar_95'))" tone="warning" />
            </div>

            <dl class="metadata-grid metadata-grid--wide report-metadata">
              <div><dt>数据集</dt><dd>{{ metadata?.dataset || metadata?.dataset_id || selected.datasetId || selected.config.dataset || '旧版 CSV' }}</dd></div>
              <div><dt>Dataset ID</dt><dd class="mono">{{ metadata?.dataset_id || selected.datasetId || '旧版 CSV' }}</dd></div>
              <div><dt>数据哈希</dt><dd class="mono">{{ metadata?.dataset_checksum || selected.datasetChecksum || '—' }}</dd></div>
              <div><dt>Provider / 窗口</dt><dd>{{ metadata?.data_provider || selected.dataProvider || 'local' }} · {{ metadata?.window_mode || selected.windowMode || 'RAW_RANGE' }}</dd></div>
              <div><dt>数据区间</dt><dd>{{ time(metadata?.data_start) }} – {{ time(metadata?.data_end) }}</dd></div>
              <div><dt>总行数 / 执行行数</dt><dd>{{ metadata?.row_count || '—' }} / {{ metadata?.execution_rows || '—' }}</dd></div>
              <div><dt>代码提交</dt><dd class="mono">{{ metadata?.code_commit || selected.codeCommit || '—' }}</dd></div>
              <div><dt>参数版本</dt><dd>{{ selected.parameterVersion || '—' }}</dd></div>
              <div><dt>参数冻结</dt><dd>{{ validation?.parameters_frozen ? '是' : '否' }}</dd></div>
            </dl>

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

            <section class="decomposition-grid">
              <div><span>网格毛利润</span><strong>{{ money(summaryMetric('gross_grid_pnl')) }}</strong></div>
              <div><span>手续费</span><strong class="negative">{{ money(-Math.abs(summaryMetric('fees_paid'))) }}</strong></div>
              <div><span>资金费</span><strong class="negative">{{ money(-Math.abs(summaryMetric('funding_paid'))) }}</strong></div>
              <div><span>突破/止损盈亏</span><strong :class="summaryMetric('stop_exit_pnl') >= 0 ? 'positive' : 'negative'">{{ money(summaryMetric('stop_exit_pnl')) }}</strong></div>
            </section>
          </div>

          <div v-else-if="reportTab === 'validation'" class="report-section">
            <div class="validation-grid">
              <section class="validation-card">
                <div class="panel__header">
                  <div><p class="eyebrow">Walk-Forward</p><h3>滚动样本外折</h3></div>
                  <StatusBadge :tone="walkForward?.status === 'COMPLETED' ? 'good' : 'warning'" :label="walkForward?.status || '未运行'" />
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
                  <div><p class="eyebrow">Monte Carlo</p><h3>尾部结果分布</h3></div>
                  <StatusBadge :tone="monteCarlo?.status === 'COMPLETED' ? 'good' : 'warning'" :label="`${monteCarlo?.simulations || 0} 次`" />
                </div>
                <MiniLineChart :values="monteCarloValues" label="Monte Carlo P05 P50 P95" :height="110" />
                <dl class="metadata-grid">
                  <div><dt>P05 总盈亏</dt><dd>{{ money(Number(monteCarlo?.total_pnl_p05 || 0)) }}</dd></div>
                  <div><dt>P50 总盈亏</dt><dd>{{ money(Number(monteCarlo?.total_pnl_p50 || 0)) }}</dd></div>
                  <div><dt>亏损概率</dt><dd>{{ pct(Number(monteCarlo?.loss_probability || 0)) }}</dd></div>
                  <div><dt>P99 最大回撤</dt><dd>{{ money(Number(monteCarlo?.max_drawdown_p99 || 0)) }}</dd></div>
                </dl>
              </section>
            </div>

            <section class="chart-block">
              <div class="panel__header">
                <div><h3>固定窗口收益分布</h3><p>每 {{ windowDistribution?.window_rows || 0 }} 根 K 线一个窗口</p></div>
                <StatusBadge :tone="Number(windowDistribution?.positive_ratio || 0) >= 0.5 ? 'good' : 'warning'" :label="`盈利窗口 ${pct(Number(windowDistribution?.positive_ratio || 0))}`" />
              </div>
              <MiniLineChart :values="windowValues" label="固定窗口收益序列" :tone="Number(windowDistribution?.worst || 0) < 0 ? 'danger' : 'good'" />
              <dl class="metadata-grid metadata-grid--wide">
                <div><dt>P05</dt><dd>{{ money(Number(windowDistribution?.p05 || 0)) }}</dd></div>
                <div><dt>P50</dt><dd>{{ money(Number(windowDistribution?.p50 || 0)) }}</dd></div>
                <div><dt>P95</dt><dd>{{ money(Number(windowDistribution?.p95 || 0)) }}</dd></div>
                <div><dt>最差 / 最好</dt><dd>{{ money(Number(windowDistribution?.worst || 0)) }} / {{ money(Number(windowDistribution?.best || 0)) }}</dd></div>
              </dl>
            </section>

            <section class="chart-block">
              <div class="panel__header">
                <div><h3>成本与成交敏感性</h3><p>固定参数下主动恶化费用、漏单和滑点</p></div>
                <StatusBadge :tone="sensitivity?.status === 'COMPLETED' ? 'good' : 'warning'" :label="sensitivity?.status || '未运行'" />
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>场景</th><th>净收益</th><th>较基准变化</th><th>最大回撤</th><th>成交</th><th>最大库存</th><th>停止原因</th></tr></thead>
                  <tbody>
                    <tr v-for="scenario in sensitivity?.scenarios || []" :key="scenario.key">
                      <td><strong>{{ scenario.label }}</strong></td>
                      <td :class="Number(scenario.total_pnl || 0) >= 0 ? 'positive' : 'negative'">{{ money(Number(scenario.total_pnl || 0)) }}</td>
                      <td :class="Number(scenario.pnl_delta_vs_baseline || 0) >= 0 ? 'positive' : 'negative'">{{ money(Number(scenario.pnl_delta_vs_baseline || 0)) }}</td>
                      <td>{{ money(Number(scenario.max_drawdown || 0)) }}</td>
                      <td>{{ scenario.fills || 0 }}</td>
                      <td>{{ pct(Number(scenario.max_inventory_utilization || 0)) }}</td>
                      <td>{{ scenario.stopped_reason || '—' }}</td>
                    </tr>
                    <tr v-if="!sensitivity?.scenarios?.length"><td colspan="7"><div class="empty-inline">{{ sensitivity?.error || '暂无敏感性结果' }}</div></td></tr>
                  </tbody>
                </table>
              </div>
            </section>

            <details class="disclosure" open>
              <summary><BarChart3 :size="18" />查看 {{ walkForwardFolds.length }} 个 Walk-Forward 折</summary>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>折</th><th>训练区间</th><th>测试区间</th><th>状态</th><th>净收益</th><th>最大回撤</th><th>成交</th></tr></thead>
                  <tbody>
                    <tr v-for="fold in walkForwardFolds" :key="String(fold.fold)">
                      <td>#{{ fold.fold }}</td>
                      <td>{{ fold.train_start }} – {{ fold.train_end }}</td>
                      <td>{{ fold.test_start }} – {{ fold.test_end }}</td>
                      <td>{{ fold.status }}</td>
                      <td>{{ money(Number(fold.total_pnl || 0)) }}</td>
                      <td>{{ money(Number(fold.max_drawdown || 0)) }}</td>
                      <td>{{ fold.fills || 0 }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </details>

            <div class="inline-alert">
              <AlertTriangle :size="19" />
              <span><strong>Regime 诊断：{{ validation?.regime_diagnostics?.status || '未运行' }}</strong>{{ validation?.regime_diagnostics?.reason }}</span>
            </div>
          </div>

          <div v-else class="report-section">
            <div class="metric-grid metric-grid--report">
              <MetricCard label="尝试成交" :value="metric('attempted_fill_count').toFixed(0)" />
              <MetricCard label="拒绝成交" :value="metric('rejected_fill_count').toFixed(0)" tone="warning" />
              <MetricCard label="Grid Fill Ratio" :value="pct(metric('grid_fill_ratio'))" />
              <MetricCard label="Pair Completion" :value="pct(metric('pair_completion_ratio'))" />
              <MetricCard label="库存 P95 / P99" :value="`${pct(metric('inventory_p95'))} / ${pct(metric('inventory_p99'))}`" tone="warning" />
              <MetricCard label="最大库存利用率" :value="pct(metric('max_inventory_utilization'))" tone="warning" />
            </div>

            <div class="chart-block">
              <div class="panel__header">
                <div><h3>库存利用率路径</h3><p>检查网格在单边行情中的库存累积</p></div>
              </div>
              <MiniLineChart :values="inventoryValues" label="回测库存利用率" tone="danger" />
            </div>

            <dl class="metadata-grid metadata-grid--wide report-metadata">
              <div><dt>手续费</dt><dd>{{ money(summaryMetric('fees_paid')) }}</dd></div>
              <div><dt>资金费</dt><dd>{{ money(summaryMetric('funding_paid')) }}</dd></div>
              <div><dt>止损退出成本</dt><dd>{{ money(summaryMetric('stop_exit_cost')) }}</dd></div>
              <div><dt>止损退出盈亏</dt><dd>{{ money(summaryMetric('stop_exit_pnl')) }}</dd></div>
              <div><dt>未配对净数量</dt><dd>{{ number(summary.net_position_qty, 6) }}</dd></div>
              <div><dt>停止原因</dt><dd>{{ summary.stopped_reason || '正常结束' }}</dd></div>
            </dl>

            <details class="disclosure" open>
              <summary><BarChart3 :size="18" />查看 {{ fills.length }} 条模拟成交</summary>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Bar</th><th>时间</th><th>方向</th><th>格位</th><th>价格</th><th>数量</th><th>费用</th><th>网格利润</th></tr></thead>
                  <tbody>
                    <tr v-for="(fill, index) in fills" :key="index">
                      <td>{{ fill.bar_index }}</td>
                      <td>{{ fill.timestamp || '—' }}</td>
                      <td>{{ fill.side }}</td>
                      <td>#{{ fill.grid_index }}</td>
                      <td>{{ number(fill.price, 4) }}</td>
                      <td>{{ number(fill.qty, 6) }}</td>
                      <td>{{ money(Number(fill.fee || 0)) }}</td>
                      <td>{{ money(Number(fill.grid_pnl || 0)) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </details>

            <details class="disclosure">
              <summary><Settings2 :size="18" />查看完整运行参数</summary>
              <pre class="json-panel">{{ JSON.stringify(metadata?.run_config || selected.config, null, 2) }}</pre>
            </details>
          </div>
        </template>

        <div v-else class="empty-state">
          <CheckCircle2 :size="32" />
          <h2 id="report-title">选择一份回测报告</h2>
          <p>报告包含收益、回撤、库存、成本、Walk-Forward、Monte Carlo 和成交明细。</p>
        </div>
      </section>
    </div>
  </div>
</template>
