<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, patchJSON } from '../api'
const rows = ref([])
const err = ref('')
async function load() {
  rows.value = await getJSON('/api/rules')
}
async function toggle(r) {
  await patchJSON(`/api/rules/${r.id}`, { enabled: !r.enabled })
  await load()
}
function refLabel(r) {
  if (!r.reference) return ''
  const t = r.reference.target_dataset_name || `#${r.reference.target_dataset_id}`
  return `${t}.${r.reference.target_field}`
}
onMounted(async () => {
  try { await load() }
  catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <header>
      <h1>规则</h1>
      <p class="muted">字段级必填 / 类型检查；error 进隔离，warn 仍可放行。跨数据集参照规则读目标夹具键集，不读精选区。</p>
    </header>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <table>
        <thead><tr><th>ID</th><th>数据集</th><th>名称</th><th>字段</th><th>类型</th><th>严重度</th><th>参照来源</th><th>启用</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.dataset_id }}</td>
            <td>{{ r.name }}</td>
            <td><code>{{ r.field }}</code></td>
            <td>{{ r.rule_type }}</td>
            <td><span class="tag" :class="r.severity === 'error' ? 'danger' : ''">{{ r.severity }}</span></td>
            <td>
              <template v-if="r.reference">
                <div><code>{{ refLabel(r) }}</code></div>
                <div class="muted">{{ r.reference.fixture_path || '—' }}</div>
                <div>
                  <span class="tag" :class="r.reference.ready ? 'ok' : 'danger'">
                    {{ r.reference.ready ? `目标键 ${r.reference.target_key_count}` : r.reference.status }}
                  </span>
                  <span class="muted" v-if="r.reference.ready"> / 目标行 {{ r.reference.target_rows }}</span>
                </div>
                <div class="muted" v-if="!r.reference.ready">{{ r.reference.error }}</div>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ r.enabled ? '是' : '否' }}</td>
            <td><button @click="toggle(r)">{{ r.enabled ? '停用' : '启用' }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
