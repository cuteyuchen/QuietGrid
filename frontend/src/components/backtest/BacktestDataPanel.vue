<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  CheckCircle2,
  CloudDownload,
  Database,
  FileCheck2,
  FileUp,
  Hash,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  X,
} from '@lucide/vue'
import {
  cancelV2BacktestDatasetJob,
  createV2BacktestDatasetJob,
  deleteV2BacktestDataset,
  loadV2BacktestDataProviders,
  loadV2BacktestDatasetDetail,
  loadV2BacktestDatasetJob,
  previewV2BacktestDataset,
  searchV2BacktestSymbols,
  uploadV2BacktestDataset,
  type V2BacktestDataProvider,
  type V2BacktestDataset,
  type V2BacktestSymbol,
  type V2DatasetJob,
  type V2DatasetPreview,
  type V2DatasetRequest,
} from '../../api'
import StatusBadge from '../StatusBadge.vue'

const props = defineProps<{
  accountId: string
  datasets: V2BacktestDataset[]
  selectedKey: string
  loading?: boolean
}>()

const emit = defineEmits<{
  select: [dataset: V2BacktestDataset]
  refresh: []
}>()

type SourceTab = 'online' | 'datasets' | 'upload'

const sourceTab = ref<SourceTab>('online')
const providers = ref<V2BacktestDataProvider[]>([])
const symbols = ref<V2BacktestSymbol[]>([])
const symbolLoading = ref(false)
const symbolMessage = ref('')
const dataError = ref('')
const previewing = ref(false)
const freezing = ref(false)
const preview = ref<V2DatasetPreview | null>(null)
const job = ref<V2DatasetJob | null>(null)
const datasetSearch = ref('')
const uploadFile = ref<File | null>(null)
const uploading = ref(false)
const deletingDatasetId = ref('')
const confirmDeleteId = ref('')

const now = new Date()
const sevenDaysAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
const online = ref({
  provider: 'binance',
  symbol: 'BTCUSDT',
  interval: '1m',
  priceType: 'CONTRACT',
  startTime: toLocalInput(sevenDaysAgo),
  endTime: toLocalInput(now),
  windowMode: 'NYSE_CLOSED_ONLY' as const,
})
const upload = ref({
  symbol: 'BTCUSDT',
  interval: '1m',
  windowMode: 'NYSE_CLOSED_ONLY' as const,
})

let pollTimer: number | null = null
let symbolTimer: number | null = null

const selectedDataset = computed(() => props.datasets.find(
  (item) => datasetKey(item) === props.selectedKey,
) || null)
const frozenDatasets = computed(() => props.datasets.filter((item) => item.sourceType === 'FROZEN_DATASET'))
const legacyDatasets = computed(() => props.datasets.filter((item) => item.sourceType === 'LEGACY_CSV'))
const visibleDatasets = computed(() => {
  const query = datasetSearch.value.trim().toUpperCase()
  if (!query) return props.datasets
  return props.datasets.filter((item) => [
    item.name,
    item.symbol,
    item.interval,
    item.datasetId,
    item.relativePath,
  ].some((value) => String(value || '').toUpperCase().includes(query)))
})
const activeProvider = computed(() => providers.value.find((item) => item.id === online.value.provider))
const canPreview = computed(() => Boolean(
  online.value.symbol.trim()
    && online.value.startTime
    && online.value.endTime
    && new Date(online.value.startTime) < new Date(online.value.endTime),
))
const jobActive = computed(() => Boolean(
  job.value && !['READY', 'FAILED', 'CANCELLED'].includes(job.value.status),
))

onMounted(async () => {
  if (props.selectedKey.startsWith('id:')) sourceTab.value = 'datasets'
  await loadProviders()
})
onBeforeUnmount(stopPolling)
watch(() => props.selectedKey, (key, previousKey) => {
  if (!previousKey && key.startsWith('id:')) sourceTab.value = 'datasets'
})
watch(() => props.accountId, () => {
  stopPolling()
  preview.value = null
  job.value = null
  void loadProviders()
})
watch(
  () => [online.value.provider, online.value.symbol, online.value.interval, online.value.startTime, online.value.endTime, online.value.windowMode],
  () => {
    preview.value = null
    if (!jobActive.value) job.value = null
  },
)

async function loadProviders() {
  dataError.value = ''
  try {
    providers.value = await loadV2BacktestDataProviders(props.accountId)
  } catch (reason) {
    dataError.value = message(reason, '无法加载在线数据源')
  }
}

function queueSymbolSearch() {
  if (symbolTimer != null) window.clearTimeout(symbolTimer)
  symbolTimer = window.setTimeout(() => void loadSymbols(online.value.symbol), 280)
}

async function loadSymbols(query: string) {
  symbolLoading.value = true
  symbolMessage.value = ''
  try {
    symbols.value = await searchV2BacktestSymbols(query, props.accountId)
  } catch (reason) {
    symbols.value = []
    symbolMessage.value = `${message(reason, '标的列表暂不可用')}；仍可手动输入合约代码。`
  } finally {
    symbolLoading.value = false
  }
}

function requestPayload(): V2DatasetRequest {
  return {
    provider: online.value.provider,
    symbol: online.value.symbol.trim().toUpperCase(),
    interval: online.value.interval,
    priceType: online.value.priceType,
    startTime: new Date(online.value.startTime).toISOString(),
    endTime: new Date(online.value.endTime).toISOString(),
    windowMode: online.value.windowMode,
  }
}

async function inspectData() {
  if (!canPreview.value || previewing.value) return
  previewing.value = true
  dataError.value = ''
  try {
    preview.value = await previewV2BacktestDataset(requestPayload(), props.accountId)
  } catch (reason) {
    preview.value = null
    dataError.value = message(reason, '数据检查失败')
  } finally {
    previewing.value = false
  }
}

async function freezeData() {
  if (!canPreview.value || freezing.value || jobActive.value) return
  freezing.value = true
  dataError.value = ''
  try {
    if (!preview.value) await inspectData()
    job.value = await createV2BacktestDatasetJob(requestPayload(), props.accountId)
    if (job.value.status === 'READY') {
      await finishJob(job.value)
    } else {
      startPolling()
    }
  } catch (reason) {
    dataError.value = message(reason, '无法创建数据下载任务')
  } finally {
    freezing.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => void pollJob(), 1200)
  void pollJob()
}

function stopPolling() {
  if (pollTimer != null) window.clearInterval(pollTimer)
  pollTimer = null
}

async function pollJob() {
  if (!job.value?.jobId) return
  try {
    job.value = await loadV2BacktestDatasetJob(job.value.jobId, props.accountId)
    if (job.value.status === 'READY') {
      stopPolling()
      await finishJob(job.value)
    } else if (['FAILED', 'CANCELLED'].includes(job.value.status)) {
      stopPolling()
      if (job.value.error) dataError.value = job.value.error
    }
  } catch (reason) {
    stopPolling()
    dataError.value = message(reason, '无法读取下载进度')
  }
}

async function finishJob(completed: V2DatasetJob) {
  if (!completed.datasetId) return
  const dataset = await loadV2BacktestDatasetDetail(completed.datasetId, props.accountId)
  emit('refresh')
  emit('select', dataset)
  sourceTab.value = 'datasets'
}

async function cancelJob() {
  if (!job.value || !jobActive.value) return
  try {
    await cancelV2BacktestDatasetJob(job.value.jobId, props.accountId)
    job.value.cancelRequested = true
  } catch (reason) {
    dataError.value = message(reason, '取消任务失败')
  }
}

function handleUploadFile(event: Event) {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0] || null
}

async function importUpload() {
  if (!uploadFile.value || uploading.value || !upload.value.symbol.trim()) return
  uploading.value = true
  dataError.value = ''
  try {
    const dataset = await uploadV2BacktestDataset(
      uploadFile.value,
      {
        symbol: upload.value.symbol.trim().toUpperCase(),
        interval: upload.value.interval,
        windowMode: upload.value.windowMode,
      },
      props.accountId,
    )
    emit('refresh')
    emit('select', dataset)
    sourceTab.value = 'datasets'
    uploadFile.value = null
  } catch (reason) {
    dataError.value = message(reason, 'CSV 导入失败')
  } finally {
    uploading.value = false
  }
}

async function deleteSelectedDataset() {
  const datasetId = selectedDataset.value?.datasetId || ''
  if (!datasetId || deletingDatasetId.value) return
  if (confirmDeleteId.value !== datasetId) {
    confirmDeleteId.value = datasetId
    return
  }
  deletingDatasetId.value = datasetId
  dataError.value = ''
  try {
    await deleteV2BacktestDataset(datasetId, props.accountId)
    confirmDeleteId.value = ''
    emit('refresh')
  } catch (reason) {
    dataError.value = message(reason, '数据集删除失败')
  } finally {
    deletingDatasetId.value = ''
  }
}

function selectDataset(dataset: V2BacktestDataset) {
  emit('select', dataset)
}

function datasetKey(dataset: V2BacktestDataset) {
  return dataset.datasetId ? `id:${dataset.datasetId}` : `path:${dataset.relativePath}`
}

function statusTone(status: string) {
  if (['READY', 'PASS'].includes(status)) return 'good'
  if (['READY_WITH_WARNINGS', 'LEGACY'].includes(status)) return 'warning'
  return 'danger'
}

function qualityLabel(dataset: V2BacktestDataset) {
  if (dataset.sourceType === 'LEGACY_CSV') return '旧版 CSV'
  if (dataset.qualityStatus === 'READY_WITH_WARNINGS') return '可用，有警告'
  if (dataset.qualityStatus === 'READY') return '质量通过'
  return dataset.qualityStatus || dataset.status || '未知'
}

function stageLabel(stage: string) {
  const labels: Record<string, string> = {
    CREATED: '任务已创建',
    DOWNLOADING: '下载历史 K 线',
    NORMALIZING: '标准化字段',
    VALIDATING: '执行质量校验',
    WINDOWING: '切分 NYSE 休市窗口',
    COMPLETED: '冻结数据集完成',
    CACHE_HIT: '复用已有冻结数据',
    FAILED: '任务失败',
    CANCELLED: '任务已取消',
  }
  return labels[stage] || stage || '等待开始'
}

function bytes(value: number) {
  if (!value) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 ** 2).toFixed(1)} MB`
}

function count(value: number | null | undefined) {
  return value == null ? '待窗口切片' : value.toLocaleString('zh-CN')
}

function time(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function dateRange(dataset: V2BacktestDataset) {
  const start = dataset.actualStart || dataset.requestedStart
  const end = dataset.actualEnd || dataset.requestedEnd
  if (!start && !end) return '范围由旧版 CSV 在运行时读取'
  return `${time(start)} – ${time(end)}`
}

function shortHash(value: string) {
  return value ? `${value.slice(0, 12)}…${value.slice(-6)}` : '旧版未冻结'
}

function toLocalInput(value: Date) {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback
}
</script>

<template>
  <section class="panel backtest-data-panel" aria-labelledby="data-preparation-title">
    <div class="panel__header backtest-step-heading">
      <div class="backtest-step-number" aria-hidden="true">1</div>
      <div>
        <p class="eyebrow">数据准备</p>
        <h2 id="data-preparation-title">选择并冻结可复现的数据</h2>
        <p>在线下载、已有数据集和 CSV 上传共用同一套校验；只有质量可用的数据才能进入回测。</p>
      </div>
      <div class="dataset-counts" aria-label="数据集数量">
        <span><strong>{{ frozenDatasets.length }}</strong> 冻结</span>
        <span><strong>{{ legacyDatasets.length }}</strong> 旧版</span>
      </div>
    </div>

    <nav class="source-tabs" aria-label="历史数据来源">
      <button type="button" :class="{ active: sourceTab === 'online' }" @click="sourceTab = 'online'">
        <CloudDownload :size="18" />
        <span><strong>Binance 在线</strong><small>查询、下载并冻结</small></span>
      </button>
      <button type="button" :class="{ active: sourceTab === 'datasets' }" @click="sourceTab = 'datasets'">
        <Database :size="18" />
        <span><strong>已有数据集</strong><small>复用冻结或旧版 CSV</small></span>
      </button>
      <button type="button" :class="{ active: sourceTab === 'upload' }" @click="sourceTab = 'upload'">
        <FileUp :size="18" />
        <span><strong>上传 CSV</strong><small>先校验，再冻结</small></span>
      </button>
    </nav>

    <div v-if="dataError" class="inline-alert inline-alert--danger" role="alert">
      <AlertTriangle :size="19" />
      <span><strong>数据准备未完成</strong>{{ dataError }}</span>
      <button class="icon-button" type="button" aria-label="关闭错误提示" @click="dataError = ''"><X :size="16" /></button>
    </div>

    <div v-if="sourceTab === 'online'" class="data-source-workspace">
      <div class="data-source-form">
        <label class="field">
          <span>数据源</span>
          <select v-model="online.provider">
            <option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.label }}</option>
            <option v-if="!providers.length" value="binance">Binance USDⓈ-M Futures</option>
          </select>
          <small>{{ activeProvider?.market || 'USDS_M' }} · 仅公共只读行情接口</small>
        </label>
        <label class="field field--symbol">
          <span>合约标的</span>
          <div class="input-with-icon">
            <Search :size="16" />
            <input
              v-model.trim="online.symbol"
              list="backtest-symbols"
              type="text"
              autocomplete="off"
              placeholder="例如 BTCUSDT"
              @input="queueSymbolSearch"
              @focus="loadSymbols(online.symbol)"
            >
            <LoaderCircle v-if="symbolLoading" :size="16" class="spin" />
          </div>
          <datalist id="backtest-symbols">
            <option v-for="item in symbols" :key="item.symbol" :value="item.symbol">{{ item.baseAsset }}/{{ item.quoteAsset }}</option>
          </datalist>
          <small v-if="symbolMessage" class="field-note--warning">{{ symbolMessage }}</small>
          <small v-else>支持搜索 Binance 永续合约，也可直接输入代码。</small>
        </label>
        <label class="field">
          <span>K 线周期</span>
          <select v-model="online.interval">
            <option v-for="interval in activeProvider?.intervals || ['1m', '5m', '15m', '1h']" :key="interval" :value="interval">{{ interval }}</option>
          </select>
        </label>
        <label class="field">
          <span>价格类型</span>
          <select v-model="online.priceType" disabled><option value="CONTRACT">合约成交价</option></select>
          <small>当前版本固定 Contract Price。</small>
        </label>
        <label class="field">
          <span>开始时间</span>
          <input v-model="online.startTime" type="datetime-local">
          <small>按本地时间输入，服务端统一存 UTC。</small>
        </label>
        <label class="field">
          <span>结束时间</span>
          <input v-model="online.endTime" type="datetime-local">
        </label>
        <label class="field field--wide">
          <span>回测窗口</span>
          <select v-model="online.windowMode" disabled>
            <option value="NYSE_CLOSED_ONLY">仅周末与节假日长休市窗口</option>
          </select>
          <small>下载仍保存完整冻结数据，执行回测时只使用相邻 NYSE 交易日间隔大于 1 天的窗口。</small>
        </label>
        <label class="check-field check-field--disabled field--wide">
          <input type="checkbox" disabled>
          <span><strong>包含真实资金费</strong><small>当前尚未接入真实 funding 时间序列，保持关闭以避免伪精度。</small></span>
        </label>
      </div>

      <div class="data-actionbar">
        <div>
          <strong>{{ preview ? '数据检查已完成' : '先检查可用性，再创建冻结数据集' }}</strong>
          <small>检查不会下载全量数据；冻结任务可以安全取消。</small>
        </div>
        <button class="button button--secondary" type="button" :disabled="!canPreview || previewing || jobActive" @click="inspectData">
          <RefreshCw :size="17" :class="{ spin: previewing }" />
          {{ previewing ? '检查中…' : '检查数据' }}
        </button>
        <button class="button button--primary" type="button" :disabled="!canPreview || freezing || jobActive" @click="freezeData">
          <CloudDownload :size="17" />
          {{ freezing ? '创建任务…' : preview?.cacheHit ? '复用冻结数据' : '下载并冻结' }}
        </button>
      </div>

      <section v-if="preview" class="dataset-preview" aria-label="数据预览">
        <header>
          <span class="preview-status"><CheckCircle2 :size="18" />数据可用</span>
          <StatusBadge :tone="preview.cacheHit ? 'good' : 'info'" :label="preview.cacheHit ? '已有缓存' : '需要下载'" />
        </header>
        <dl>
          <div><dt>预计 K 线</dt><dd>{{ preview.estimatedRows.toLocaleString('zh-CN') }}</dd></div>
          <div><dt>预计请求</dt><dd>{{ preview.estimatedPages }} 页</dd></div>
          <div><dt>预计数据量</dt><dd>{{ bytes(preview.estimatedSizeBytes) }}</dd></div>
          <div><dt>预计休市窗口</dt><dd>{{ count(preview.windowCount) }}</dd></div>
        </dl>
        <ul v-if="preview.warnings.length" class="compact-warning-list">
          <li v-for="warning in preview.warnings" :key="warning"><AlertTriangle :size="15" />{{ warning }}</li>
        </ul>
      </section>

      <section v-if="job" class="dataset-job" aria-live="polite">
        <header>
          <span>
            <LoaderCircle v-if="jobActive" :size="19" class="spin" />
            <CheckCircle2 v-else-if="job.status === 'READY'" :size="19" />
            <AlertTriangle v-else :size="19" />
          </span>
          <div><strong>{{ job.symbol }} · {{ job.interval }}</strong><small>{{ stageLabel(job.stage) }}</small></div>
          <StatusBadge :tone="job.status === 'READY' ? 'good' : job.status === 'FAILED' ? 'danger' : 'info'" :label="job.status" />
        </header>
        <div class="progress-track" role="progressbar" :aria-valuenow="Math.round(job.progress * 100)" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ width: `${Math.max(2, job.progress * 100)}%` }" />
        </div>
        <div class="job-stats">
          <span><strong>{{ Math.round(job.progress * 100) }}%</strong> 完成</span>
          <span><strong>{{ job.downloadedRows.toLocaleString('zh-CN') }}</strong> 已下载</span>
          <span><strong>{{ job.currentPage }} / {{ job.totalPages || '—' }}</strong> 分页</span>
          <button v-if="jobActive" class="button button--secondary button--small" type="button" :disabled="job.cancelRequested" @click="cancelJob">
            {{ job.cancelRequested ? '等待取消…' : '取消任务' }}
          </button>
        </div>
      </section>
    </div>

    <div v-else-if="sourceTab === 'datasets'" class="dataset-library">
      <div class="dataset-library-toolbar">
        <div class="search-field">
          <Search :size="17" />
          <input v-model="datasetSearch" type="search" placeholder="搜索标的、周期、Dataset ID 或文件名">
        </div>
        <button class="button button--secondary" type="button" :disabled="loading" @click="emit('refresh')">
          <RefreshCw :size="16" :class="{ spin: loading }" />刷新列表
        </button>
      </div>
      <div v-if="visibleDatasets.length" class="dataset-card-list">
        <button
          v-for="dataset in visibleDatasets"
          :key="datasetKey(dataset)"
          type="button"
          class="dataset-card"
          :class="{ active: selectedKey === datasetKey(dataset) }"
          @click="selectDataset(dataset)"
        >
          <span class="dataset-card__icon"><Database :size="20" /></span>
          <span class="dataset-card__main">
            <span class="dataset-card__title">
              <strong>{{ dataset.symbol || dataset.name }}</strong>
              <small>{{ dataset.interval || '旧格式' }}</small>
              <StatusBadge :tone="statusTone(dataset.qualityStatus)" :label="qualityLabel(dataset)" />
            </span>
            <small>{{ dateRange(dataset) }}</small>
            <span class="dataset-card__meta">
              <small>{{ dataset.rowCount ? `${dataset.rowCount.toLocaleString('zh-CN')} 行` : bytes(dataset.sizeBytes) }}</small>
              <small>{{ dataset.provider || 'local' }}</small>
              <small class="mono">{{ shortHash(dataset.checksum) }}</small>
            </span>
          </span>
          <CheckCircle2 v-if="selectedKey === datasetKey(dataset)" :size="21" class="dataset-selected-icon" />
        </button>
      </div>
      <div v-else class="empty-state empty-state--compact">
        <Database :size="28" />
        <h3>没有匹配的数据集</h3>
        <p>切换到 Binance 在线下载，或上传一份 CSV。</p>
      </div>
    </div>

    <div v-else class="upload-workspace">
      <div class="upload-dropzone" :class="{ 'has-file': uploadFile }">
        <FileCheck2 v-if="uploadFile" :size="34" />
        <FileUp v-else :size="34" />
        <div>
          <strong>{{ uploadFile ? uploadFile.name : '选择需要校验的 CSV 文件' }}</strong>
          <p>{{ uploadFile ? `${bytes(uploadFile.size)} · 将先校验并冻结，不会直接回测` : '最大 25 MB；必须包含 high、low、close 以及 timestamp 或 open_time。' }}</p>
        </div>
        <label class="button button--secondary file-picker">
          {{ uploadFile ? '更换文件' : '选择文件' }}
          <input type="file" accept=".csv,text/csv" @change="handleUploadFile">
        </label>
      </div>
      <div class="upload-options">
        <label class="field"><span>标的</span><input v-model.trim="upload.symbol" type="text" placeholder="BTCUSDT"></label>
        <label class="field"><span>K 线周期</span><select v-model="upload.interval"><option>1m</option><option>5m</option><option>15m</option><option>1h</option></select></label>
        <label class="field"><span>回测窗口</span><select v-model="upload.windowMode" disabled><option value="NYSE_CLOSED_ONLY">仅周末与节假日长休市窗口</option></select></label>
        <button class="button button--primary" type="button" :disabled="!uploadFile || !upload.symbol.trim() || uploading" @click="importUpload">
          <LoaderCircle v-if="uploading" :size="17" class="spin" /><ShieldCheck v-else :size="17" />
          {{ uploading ? '校验并冻结中…' : '校验并导入' }}
        </button>
      </div>
    </div>

    <section v-if="selectedDataset" class="selected-dataset-summary" aria-label="当前回测数据集">
      <header>
        <div>
          <span class="selected-dataset-summary__check"><ShieldCheck :size="20" /></span>
          <span><small>当前回测数据</small><strong>{{ selectedDataset.symbol || selectedDataset.name }} · {{ selectedDataset.interval || '旧版 CSV' }}</strong></span>
        </div>
        <div class="selected-dataset-summary__actions">
          <StatusBadge :tone="statusTone(selectedDataset.qualityStatus)" :label="qualityLabel(selectedDataset)" />
          <template v-if="selectedDataset.datasetId">
            <button
              v-if="confirmDeleteId !== selectedDataset.datasetId"
              class="button button--danger-outline button--small"
              type="button"
              title="已被回测报告引用的数据集不会删除"
              @click="deleteSelectedDataset"
            >
              <Trash2 :size="15" />删除数据
            </button>
            <template v-else>
              <button class="button button--ghost button--small" type="button" :disabled="Boolean(deletingDatasetId)" @click="confirmDeleteId = ''">取消</button>
              <button class="button button--danger button--small" type="button" :disabled="Boolean(deletingDatasetId)" @click="deleteSelectedDataset">
                <LoaderCircle v-if="deletingDatasetId" :size="15" class="spin" /><Trash2 v-else :size="15" />
                {{ deletingDatasetId ? '删除中…' : '再次确认删除' }}
              </button>
            </template>
          </template>
        </div>
      </header>
      <dl>
        <div><dt>Dataset ID</dt><dd class="mono">{{ selectedDataset.datasetId || selectedDataset.relativePath }}</dd></div>
        <div><dt>实际范围</dt><dd>{{ dateRange(selectedDataset) }}</dd></div>
        <div><dt>数据量</dt><dd>{{ selectedDataset.rowCount ? `${selectedDataset.rowCount.toLocaleString('zh-CN')} 根 K 线` : bytes(selectedDataset.sizeBytes) }}</dd></div>
        <div><dt>休市窗口</dt><dd>{{ count(selectedDataset.windowCount) }}</dd></div>
        <div><dt>Provider</dt><dd>{{ selectedDataset.provider || 'local' }}</dd></div>
        <div><dt>Checksum</dt><dd class="mono"><Hash :size="13" />{{ shortHash(selectedDataset.checksum) }}</dd></div>
      </dl>
      <details v-if="selectedDataset.sourceType === 'FROZEN_DATASET'" class="quality-details">
        <summary>查看完整数据质量报告</summary>
        <div class="quality-metrics">
          <span><small>输入 / 输出</small><strong>{{ selectedDataset.qualityReport.inputRows.toLocaleString('zh-CN') }} / {{ selectedDataset.qualityReport.outputRows.toLocaleString('zh-CN') }}</strong></span>
          <span><small>重复 K 线</small><strong>{{ selectedDataset.qualityReport.duplicateRows }}</strong></span>
          <span><small>冲突重复</small><strong>{{ selectedDataset.qualityReport.conflictingDuplicates }}</strong></span>
          <span><small>未闭合</small><strong>{{ selectedDataset.qualityReport.unclosedRows }}</strong></span>
          <span><small>缺失 K 线</small><strong>{{ selectedDataset.qualityReport.missingIntervals }}</strong></span>
          <span><small>缺失比例</small><strong>{{ (selectedDataset.qualityReport.missingRatio * 100).toFixed(4) }}%</strong></span>
        </div>
        <ul v-if="selectedDataset.qualityReport.warnings.length" class="compact-warning-list">
          <li v-for="warning in selectedDataset.qualityReport.warnings" :key="warning"><AlertTriangle :size="15" />{{ warning }}</li>
        </ul>
      </details>
    </section>
  </section>
</template>
