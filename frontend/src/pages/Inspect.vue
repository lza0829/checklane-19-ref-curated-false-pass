<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'
const datasets = ref([])
const datasetId = ref(1)
const busy = ref(false)
const err = ref('')
const last = ref(null)
const failed = ref(null)
onMounted(async () => {
  datasets.value = await getJSON('/api/datasets')
  if (datasets.value.length) datasetId.value = datasets.value[0].id
})
async function run() {
  busy.value = true
  err.value = ''
  failed.value = null
  try {
    last.value = await postJSON('/api/inspect', { dataset_id: Number(datasetId.value) })
  } catch (e) {
    // 409：目标夹具未就绪，整批参照检查失败；后端回传失败批次，离线展示
    last.value = null
    failed.value = parseFailure(e)
    err.value = failed.value ? '' : String(e.message || e)
  } finally {
    busy.value = false
  }
}
function parseFailure(e) {
  try {
    const text = String(e.message || e)
    const body = JSON.parse(text)
    const detail = body && body.detail ? body.detail : body
    if (detail && detail.report) return detail
  } catch (_) {}
  return null
}
</script>
<template>
  <div class="page">
    <header>
      <h1>入检台</h1>
      <p class="muted">装载夹具 → 执行规则（含跨数据集参照）→ 隔离失败行 → 放行通过行到精选区</p>
    </header>
    <div class="panel row">
      <label>数据集
        <select v-model="datasetId">
          <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.id }} · {{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run">{{ busy ? '入检中…' : '开始入检' }}</button>
    </div>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <!-- 目标夹具未就绪：整批失败，不签发 -->
    <div class="panel" v-if="failed">
      <h3>
        批次 #{{ failed.batch_id }}
        <span class="tag danger">failed · 整批中止</span>
      </h3>
      <p class="tag danger">{{ failed.report.error }}</p>
      <div class="tiles">
        <div class="tile"><div class="hero-num">{{ failed.report.counts.scanned }}</div><div class="muted">扫描</div></div>
        <div class="tile"><div class="hero-num">{{ failed.report.counts.passed }}</div><div class="muted">通过</div></div>
        <div class="tile"><div class="hero-num">{{ failed.report.counts.quarantined }}</div><div class="muted">隔离</div></div>
        <div class="tile"><div class="hero-num">{{ failed.report.counts.released }}</div><div class="muted">签发</div></div>
      </div>
      <h4>对账</h4>
      <table>
        <thead><tr><th>校验</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in failed.report.checks" :key="c.name">
            <td>{{ c.name }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '通过' : '失败' }}</span></td>
            <td class="muted">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
      <h4>参照规则</h4>
      <table>
        <thead><tr><th>规则</th><th>源字段</th><th>目标</th><th>夹具</th><th>状态</th><th>失败键</th></tr></thead>
        <tbody>
          <tr v-for="r in failed.report.reference.rules" :key="r.rule_id">
            <td>{{ r.rule_name }}</td>
            <td><code>{{ r.source_field }}</code></td>
            <td>{{ r.target_dataset_name }}.<code>{{ r.target_field }}</code></td>
            <td class="muted">{{ r.fixture_path }}</td>
            <td><span class="tag danger">{{ r.status }}</span></td>
            <td class="muted">{{ r.error || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="last">
      <h3>批次 #{{ last.id }} <span class="tag" :class="last.status === 'released' ? 'ok' : ''">{{ last.status }}</span></h3>
      <div class="tiles">
        <div class="tile"><div class="hero-num">{{ last.scanned_count }}</div><div class="muted">扫描</div></div>
        <div class="tile"><div class="hero-num">{{ last.passed_count }}</div><div class="muted">通过</div></div>
        <div class="tile"><div class="hero-num">{{ last.quarantined_count }}</div><div class="muted">隔离</div></div>
        <div class="tile"><div class="hero-num">{{ last.released_count }}</div><div class="muted">签发</div></div>
        <div class="tile" v-if="last.report.reference && last.report.reference.enabled">
          <div class="hero-num">{{ last.report.reference.failed_row_count }}</div>
          <div class="muted">参照失败行</div>
        </div>
      </div>
      <h4>对账</h4>
      <table>
        <thead><tr><th>校验</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in last.report.checks" :key="c.name">
            <td>{{ c.name }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '通过' : '失败' }}</span></td>
            <td class="muted">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
      <template v-if="last.report.reference && last.report.reference.enabled">
        <h4>跨数据集参照</h4>
        <table>
          <thead><tr><th>规则</th><th>源字段</th><th>目标（夹具键集）</th><th>夹具</th><th>源行/目标行</th><th>目标键</th><th>缺失键</th><th>失败行</th></tr></thead>
          <tbody>
            <tr v-for="r in last.report.reference.rules" :key="r.rule_id">
              <td>{{ r.rule_name }}</td>
              <td><code>{{ r.source_field }}</code></td>
              <td>{{ r.target_dataset_name }}.<code>{{ r.target_field }}</code></td>
              <td class="muted">{{ r.fixture_path }}</td>
              <td class="muted">{{ r.source_rows }} / {{ r.target_rows }}</td>
              <td>{{ r.target_key_count }}</td>
              <td>
                <span class="tag" :class="r.missing_key_count ? 'danger' : 'ok'">{{ r.missing_key_count }}</span>
                <span class="muted" v-if="r.missing_key_count"> · {{ r.missing_keys.join('、') }}</span>
              </td>
              <td>{{ r.failed_row_count }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">参照来源：目标数据集登记的夹具键集（只读夹具，不读精选区）。目标缺失/为空/解析失败时整批中止，不签发。</p>
      </template>
      <h4>行清单</h4>
      <table>
        <thead><tr><th>#</th><th>键</th><th>处置</th><th>命中</th></tr></thead>
        <tbody>
          <tr v-for="r in last.rows" :key="r.id">
            <td>{{ r.row_no }}</td>
            <td>{{ r.entity_key }}</td>
            <td><span class="tag" :class="r.disposition === 'pass' ? 'ok' : 'danger'">{{ r.disposition }}</span></td>
            <td class="muted">
              <span v-for="h in r.rule_hits" :key="h.rule_id + h.field">
                {{ h.name }}<template v-if="h.rule_type === 'ref_exists'">（{{ h.reference.target_dataset_name }}.{{ h.reference.target_field }} 缺 {{ h.reference.key }}）</template>；
              </span>
              <span v-if="!r.rule_hits.length">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
