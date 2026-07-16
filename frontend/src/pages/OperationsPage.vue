<script setup lang="ts">
import {
  CheckCircle2,
  CircleStop,
  Play,
  RefreshCw,
  ServerCog,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from '@lucide/vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { ConsoleSummary, TraderProcessState, VerificationRow } from '../mock'

defineProps<{
  summary: ConsoleSummary
  process: TraderProcessState
  verificationRows: VerificationRow[]
}>()

const emit = defineEmits<{
  action: [action: string]
}>()

function verificationTone(status: string) {
  if (['passed', 'ok', 'success'].includes(status.toLowerCase())) return 'good'
  if (['failed', 'error'].includes(status.toLowerCase())) return 'danger'
  return 'warning'
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Operations</p>
        <h2>连接、进程与安全清扫</h2>
        <p>这里只放运维动作；策略决策和参数配置不与系统维护混在一起。</p>
      </div>
      <StatusBadge
        :tone="process.state === 'running' ? 'good' : 'warning'"
        :label="`交易进程 ${process.state}`"
      />
    </section>

    <div class="content-grid">
      <section class="panel" aria-labelledby="process-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">服务状态</p>
            <h2 id="process-title">交易进程</h2>
          </div>
          <ServerCog :size="22" />
        </div>
        <dl class="metadata-list">
          <div><dt>服务</dt><dd>{{ process.service }}</dd></div>
          <div><dt>运行方式</dt><dd>{{ process.mode }}</dd></div>
          <div><dt>当前状态</dt><dd>{{ process.state }}</dd></div>
          <div><dt>最后心跳</dt><dd>{{ summary.heartbeat }}</dd></div>
          <div><dt>说明</dt><dd>{{ process.detail || '—' }}</dd></div>
        </dl>
        <div class="button-row">
          <button class="button button--secondary" type="button" @click="emit('action', 'trader-restart')">
            <Play :size="17" />请求重启
          </button>
          <button class="button button--danger-outline" type="button" @click="emit('action', 'trader-stop')">
            <CircleStop :size="17" />停止循环
          </button>
        </div>
      </section>

      <section class="panel" aria-labelledby="environment-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">连接环境</p>
            <h2 id="environment-title">{{ summary.mode }}</h2>
          </div>
          <ShieldCheck :size="22" />
        </div>
        <dl class="metadata-list">
          <div><dt>账户</dt><dd>{{ summary.accountLabel }}</dd></div>
          <div><dt>账户接口</dt><dd>{{ summary.accountSummary.status }}</dd></div>
          <div><dt>数据库</dt><dd>{{ summary.accountId }}</dd></div>
          <div><dt>最新消息</dt><dd>{{ summary.latestSystemMessage || '—' }}</dd></div>
        </dl>
        <button class="button button--secondary button--full" type="button" @click="emit('action', 'verify')">
          <RefreshCw :size="17" />运行只读环境验证
        </button>
      </section>
    </div>

    <section class="panel" aria-labelledby="verification-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">验收清单</p>
          <h2 id="verification-title">最近一次检查</h2>
        </div>
      </div>
      <div class="verification-grid">
        <article v-for="row in verificationRows" :key="row.module" class="verification-card">
          <CheckCircle2 v-if="verificationTone(row.statusCode) === 'good'" :size="21" />
          <TriangleAlert v-else :size="21" />
          <div>
            <strong>{{ row.name }}</strong>
            <span>{{ row.detail }}</span>
            <small>{{ row.lastChecked }}</small>
          </div>
          <StatusBadge :tone="verificationTone(row.statusCode)" :label="row.status" />
        </article>
        <div v-if="!verificationRows.length" class="empty-inline">暂无验证记录</div>
      </div>
    </section>

    <section class="panel danger-zone" aria-labelledby="maintenance-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">维护操作</p>
          <h2 id="maintenance-title">安全清扫</h2>
          <p>撤销残留订单、关闭仓位并核对交易所状态。操作需要明确输入确认词。</p>
        </div>
      </div>
      <button class="action-tile action-tile--danger" type="button" @click="emit('action', 'safety-sweep')">
        <Trash2 :size="22" />
        <span><strong>执行安全清扫</strong><small>优先用于测试结束或状态不一致后的收尾</small></span>
      </button>
    </section>
  </div>
</template>
