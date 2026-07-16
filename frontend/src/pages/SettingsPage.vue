<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertTriangle, LockKeyhole, RotateCcw, Save, ShieldCheck } from '@lucide/vue'
import StatusBadge from '../components/StatusBadge.vue'
import { loadV2ActiveConfig, type V2ActiveConfig } from '../api'
import type { StrategyConfigData, StrategySettings } from '../mock'

const props = defineProps<{
  accountId: string
  config: StrategyConfigData
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  save: [draft: StrategySettings]
}>()

const draft = ref<StrategySettings>({ ...props.config.draft })
const activeConfig = ref<V2ActiveConfig | null>(null)
const activeLoading = ref(false)
const activeError = ref('')

const sectionLabels: Record<string, string> = {
  features: '功能开关',
  risk: '风险预算',
  regime: 'Regime 市场状态',
  grid: '自适应网格',
  inventory: '库存管理',
  cooldown: '冷却与恢复',
  costs: '成本模型',
  timing: '时间窗口',
  selection: '选币与流动性',
}

const policySections = computed(() => Object.entries(activeConfig.value?.sections || {})
  .filter(([key]) => sectionLabels[key])
  .map(([key, value]) => ({
    key,
    label: sectionLabels[key],
    rows: flattenConfig(value),
  })))

watch(
  () => props.config.draft,
  (value) => {
    draft.value = { ...value }
  },
  { deep: true },
)
watch(() => props.accountId, () => void loadActiveConfig())
onMounted(() => void loadActiveConfig())

async function loadActiveConfig() {
  activeLoading.value = true
  activeError.value = ''
  try {
    activeConfig.value = await loadV2ActiveConfig(props.accountId)
  } catch (reason) {
    activeConfig.value = null
    activeError.value = reason instanceof Error ? reason.message : '无法读取 v2 激活配置'
  } finally {
    activeLoading.value = false
  }
}

function flattenConfig(
  value: Record<string, unknown>,
  prefix = '',
): Array<{ key: string; value: string }> {
  const rows: Array<{ key: string; value: string }> = []
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      rows.push(...flattenConfig(item as Record<string, unknown>, path))
    } else {
      rows.push({
        key: path,
        value: Array.isArray(item) ? item.join(', ') : String(item ?? '—'),
      })
    }
  }
  return rows
}

function reset() {
  draft.value = { ...props.config.current }
}
</script>

<template>
  <div class="page-stack settings-page">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Settings</p>
        <h2>策略参数与版本差异</h2>
        <p>先修改草稿，再由交易进程决定何时安全生效；提高风险的修改不会热加载。</p>
      </div>
      <StatusBadge
        :tone="config.diff.length ? 'warning' : 'good'"
        :label="config.diff.length ? `${config.diff.length} 项待应用` : '已与当前配置一致'"
      />
    </section>

    <div v-if="error" class="inline-alert inline-alert--danger" role="alert">
      <AlertTriangle :size="20" />
      {{ error }}
    </div>

    <div class="mobile-settings-warning">
      <ShieldCheck :size="22" />
      <div>
        <strong>手机端仅提供查看</strong>
        <p>为避免误触，高风险参数请在桌面端修改和保存。</p>
      </div>
    </div>

    <section class="panel" aria-labelledby="core-settings-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">常用参数</p>
          <h2 id="core-settings-title">资金和网格边界</h2>
          <p>这些参数最直接影响风险，默认全部展示。</p>
        </div>
        <span class="muted">草稿更新 {{ config.draftUpdatedAt || '—' }}</span>
      </div>
      <div class="settings-grid">
        <label class="field">
          <span>杠杆</span>
          <input v-model.number="draft.leverage" type="number" min="1" max="2" step="0.1">
          <small>建议 1 倍；提高时仅下一个窗口生效。</small>
        </label>
        <label class="field">
          <span>单标的资金（USDT）</span>
          <input v-model.number="draft.capitalPerSymbol" type="number" min="1" step="1">
        </label>
        <label class="field">
          <span>最大并发会话</span>
          <input v-model.number="draft.maxConcurrent" type="number" min="1" max="10" step="1">
        </label>
        <label class="field">
          <span>总资金上限（USDT）</span>
          <input v-model.number="draft.totalCapitalLimit" type="number" min="1" step="1">
        </label>
        <label class="field">
          <span>最大网格数量</span>
          <input v-model.number="draft.maxGridNum" type="number" min="2" max="100" step="1">
        </label>
        <label class="field">
          <span>单会话止盈（USDT）</span>
          <input v-model.number="draft.takeProfitUsdt" type="number" min="0" step="0.1">
        </label>
      </div>
    </section>

    <details class="panel disclosure" open>
      <summary>
        <span><strong>市场观察与波动</strong><small>决定何时计算区间与允许入场</small></span>
      </summary>
      <div class="settings-grid disclosure__content">
        <label class="field">
          <span>波动率方法</span>
          <select v-model="draft.volatilityMethod">
            <option v-for="option in config.volatilityOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <label class="field">
          <span>观察时长（小时）</span>
          <input v-model.number="draft.observeHours" type="number" min="0.1" step="0.1">
        </label>
        <label class="field">
          <span>观察 K 线周期</span>
          <input v-model="draft.observeKlineInterval" type="text">
        </label>
        <label class="field">
          <span>扫描候选数量</span>
          <input v-model.number="draft.scanCandidateCount" type="number" min="1" max="100" step="1">
        </label>
      </div>
    </details>

    <details class="panel disclosure">
      <summary>
        <span><strong>高级网格与成本参数</strong><small>不常调整，错误设置可能让回测失真</small></span>
      </summary>
      <div class="settings-grid disclosure__content">
        <label class="field">
          <span>最小格距</span>
          <input v-model.number="draft.minStepPct" type="number" min="0" max="0.1" step="0.0001">
        </label>
        <label class="field">
          <span>最小可交易区间</span>
          <input v-model.number="draft.minTradableRangePct" type="number" min="0" max="0.5" step="0.0001">
        </label>
        <label class="field">
          <span>止损缓冲</span>
          <input v-model.number="draft.stopBufferPct" type="number" min="0" max="0.2" step="0.0001">
        </label>
        <label class="field">
          <span>安全系数</span>
          <input v-model.number="draft.safetyMultiplier" type="number" min="1" max="20" step="0.1">
        </label>
        <label class="field">
          <span>最大 Maker 费率</span>
          <input v-model.number="draft.maxMakerFeeRate" type="number" min="0" max="0.01" step="0.00001">
        </label>
      </div>
    </details>

    <section class="panel" aria-labelledby="active-policy-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">V2 Active Policy</p>
          <h2 id="active-policy-title">当前激活的完整策略政策</h2>
          <p>Regime、风险、库存和冷却参数完整可见；控制台不会直接热修改这些高风险政策。</p>
        </div>
        <StatusBadge
          :tone="activeError ? 'danger' : activeLoading ? 'warning' : 'good'"
          :label="activeError ? '读取失败' : activeLoading ? '正在读取' : activeConfig?.version || '未加载'"
        />
      </div>
      <div v-if="activeError" class="inline-alert inline-alert--danger">
        <AlertTriangle :size="19" />{{ activeError }}
      </div>
      <div v-else class="policy-lock-note">
        <LockKeyhole :size="20" />
        <span>
          <strong>高风险政策由版本化配置管理</strong>
          修改应先回测、形成验证报告，再在新窗口激活；运行中的风险提高会被后端拒绝或延期。
        </span>
      </div>
    </section>

    <div class="policy-section-grid">
      <details
        v-for="(section, index) in policySections"
        :key="section.key"
        class="panel disclosure policy-section"
        :open="index < 2"
      >
        <summary>
          <span><strong>{{ section.label }}</strong><small>{{ section.rows.length }} 项激活参数</small></span>
        </summary>
        <dl class="policy-rows disclosure__content">
          <div v-for="row in section.rows" :key="row.key">
            <dt>{{ row.key }}</dt>
            <dd>{{ row.value }}</dd>
          </div>
        </dl>
      </details>
    </div>

    <section v-if="config.diff.length" class="panel" aria-labelledby="diff-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">版本差异</p>
          <h2 id="diff-title">草稿将修改什么</h2>
        </div>
      </div>
      <div class="diff-list">
        <div v-for="item in config.diff" :key="item.key">
          <span>{{ item.label }}</span>
          <del>{{ item.current }}</del>
          <strong>→ {{ item.draft }}</strong>
        </div>
      </div>
    </section>

    <section class="settings-savebar">
      <div>
        <ShieldCheck :size="22" />
        <span><strong>Risk Manager 拥有最终决定权</strong><small>保存草稿不代表立即提高实盘风险。</small></span>
      </div>
      <div>
        <button class="button button--ghost" type="button" :disabled="busy" @click="reset">
          <RotateCcw :size="17" />恢复当前值
        </button>
        <button class="button button--primary" type="button" :disabled="busy" @click="emit('save', { ...draft })">
          <Save :size="17" />{{ busy ? '保存中…' : '保存草稿' }}
        </button>
      </div>
    </section>
  </div>
</template>
