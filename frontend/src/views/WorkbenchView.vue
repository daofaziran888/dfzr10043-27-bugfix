<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="supportsManifest"
              class="link"
              type="button"
              :data-testid="`manifest-${row.id}`"
              @click="openManifest(row)"
            >
              查看文件明细
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showManifest" class="modal-backdrop" data-testid="manifest-modal">
      <div class="modal manifest-modal">
        <h3>文件明细 · {{ manifestTitle }}</h3>
        <p class="status-legend">
          <span>校验摘要：{{ manifest?.checksum || '—' }}</span>
          <span v-if="manifest?.file_count !== undefined">有效文件：{{ manifest.file_count }}</span>
          <span v-if="manifest?.excluded?.length">排除文件：{{ manifest.excluded.length }}</span>
          <span v-if="manifest?.file_count === 0" class="error-text">没有可交接的有效文件</span>
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>归集编号</th>
              <th>文件名称</th>
              <th>校验结论</th>
              <th>校验值</th>
              <th>交付回执</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="file in manifestFiles" :key="String(file.file_id)" :data-testid="`manifest-file-${file.file_id}`">
              <td>{{ file['归集编号'] ?? '—' }}</td>
              <td>{{ file['文件名称'] ?? '—' }}</td>
              <td>{{ file['校验结论'] ?? '—' }}</td>
              <td>{{ file['校验值'] ?? '—' }}</td>
              <td>{{ file['交付回执'] || '—' }}</td>
            </tr>
            <tr v-for="file in manifestExcluded" :key="`excluded-${String(file.file_id)}`" data-testid="manifest-excluded">
              <td>{{ file['归集编号'] ?? '—' }}</td>
              <td>{{ file['文件名称'] ?? '—' }}</td>
              <td class="error-text">{{ file['校验结论'] ?? '不符' }}</td>
              <td>{{ file['校验值'] ?? '—' }}</td>
              <td>—</td>
            </tr>
            <tr v-if="!manifestFiles.length && !manifestExcluded.length">
              <td colspan="5" class="empty-state">暂无文件明细</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-manifest" @click="showManifest = false">
            关闭
          </button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadManifest,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { FileManifest } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showManifest = ref(false)
const manifest = ref<FileManifest | null>(null)
const manifestTitle = ref('')
const supportsManifest = ['capture_batch', 'handoff_pack'].includes(meta.key)
const manifestFiles = computed(() => manifest.value?.files ?? [])
const manifestExcluded = computed(() => manifest.value?.excluded ?? [])
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function openManifest(row: EntryRow) {
  errorMessage.value = ''
  try {
    const payload = await loadManifest(meta.key, Number(row.id))
    manifest.value = payload
    manifestTitle.value = String(
      row['批次编号'] ?? row['交接编号'] ?? row.id,
    )
    showManifest.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文件明细读取失败'
  }
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
