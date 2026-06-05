<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'

const meta = ref(null)          // { webdav, components, restore_targets, capabilities }
const cfg = ref(null)           // editable copy of webdav config
const passwordInput = ref('')   // empty = leave unchanged
const passphraseInput = ref('') // empty = leave unchanged
const saving = ref(false)
const errorMsg = ref('')
const testResult = ref('')
const busy = ref('')            // current long-op label
const backups = ref([])
const listError = ref('')

// restore dialog state
const restoreTarget = ref(null) // backup name being restored, or '__upload__'
const restoreTargets = ref(['database'])
const restorePass = ref('')
const restoreFile = ref(null)
const restoreResult = ref('')

const scheduleOpts = [
  { v: 'manual', label: '仅手动' },
  { v: 'hourly', label: '每小时' },
  { v: 'daily', label: '每天' },
  { v: 'weekly', label: '每周一' },
]

async function load() {
  errorMsg.value = ''
  try {
    const m = await api.getBackupSettings()
    meta.value = m
    cfg.value = { ...m.webdav }
    if (!Array.isArray(cfg.value.components)) cfg.value.components = []
    passwordInput.value = ''
    passphraseInput.value = ''
  } catch (e) { errorMsg.value = String(e.message || e) }
}

onMounted(load)

function toggleComponent(key) {
  const arr = cfg.value.components
  const i = arr.indexOf(key)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(key)
}

async function save() {
  saving.value = true
  errorMsg.value = ''
  try {
    const patch = {
      enabled: !!cfg.value.enabled,
      url: cfg.value.url || '',
      username: cfg.value.username || '',
      remote_dir: cfg.value.remote_dir || '',
      components: cfg.value.components.slice(),
      encrypt: !!cfg.value.encrypt,
      schedule: cfg.value.schedule || 'manual',
      hour: parseInt(cfg.value.hour, 10) || 0,
      keep_daily: parseInt(cfg.value.keep_daily, 10) || 0,
      keep_weekly: parseInt(cfg.value.keep_weekly, 10) || 0,
      keep_monthly: parseInt(cfg.value.keep_monthly, 10) || 0,
    }
    if (passwordInput.value) patch.password = passwordInput.value
    if (passphraseInput.value) patch.passphrase = passphraseInput.value
    await api.updateBackupSettings(patch)
    await load()
  } catch (e) { errorMsg.value = String(e.message || e) }
  finally { saving.value = false }
}

async function testConn() {
  testResult.value = '测试中…'
  try {
    // Save first so the backend tests with the latest config.
    await save()
    const r = await api.testBackup()
    testResult.value = '✅ ' + (r.message || '连接成功')
  } catch (e) { testResult.value = '❌ ' + (e.message || e) }
}

async function runNow() {
  busy.value = '正在备份…'
  errorMsg.value = ''
  try {
    await save()
    const r = await api.runBackup()
    busy.value = `✅ 已备份 ${r.name} (${fmtSize(r.size)})` +
      (r.deleted?.length ? `，清理 ${r.deleted.length} 个过期备份` : '')
    await refreshList()
  } catch (e) { busy.value = ''; errorMsg.value = String(e.message || e) }
}

async function refreshList() {
  listError.value = ''
  try { backups.value = await api.listBackups() }
  catch (e) { listError.value = String(e.message || e); backups.value = [] }
}

async function del(name) {
  if (!confirm(`确定删除备份 ${name} ？此操作不可恢复。`)) return
  try { await api.deleteBackup(name); await refreshList() }
  catch (e) { listError.value = String(e.message || e) }
}

function openRestore(name) {
  restoreTarget.value = name
  restoreTargets.value = ['database']
  restorePass.value = ''
  restoreFile.value = null
  restoreResult.value = ''
}
function openRestoreUpload() { openRestore('__upload__') }
function closeRestore() { restoreTarget.value = null }

function onRestoreFile(e) { restoreFile.value = e.target.files?.[0] || null }

async function doRestore() {
  if (!restoreTargets.value.length) { restoreResult.value = '❌ 请至少选择一项恢复内容'; return }
  restoreResult.value = '恢复中…'
  try {
    let r
    if (restoreTarget.value === '__upload__') {
      if (!restoreFile.value) { restoreResult.value = '❌ 请选择备份文件'; return }
      r = await api.restoreBackupUpload(restoreFile.value, restoreTargets.value, restorePass.value)
    } else {
      r = await api.restoreBackup(restoreTarget.value, restoreTargets.value, restorePass.value)
    }
    restoreResult.value = `✅ 已恢复: ${r.restored.join(', ')}。恢复前快照已存于服务器 ${r.snapshot}`
  } catch (e) { restoreResult.value = '❌ ' + (e.message || e) }
}

function fmtSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']; let i = 0; let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`
}

const encUnavailable = computed(() => meta.value && !meta.value.capabilities?.encryption)
const davUnavailable = computed(() => meta.value && !meta.value.capabilities?.webdav)
</script>

<template>
  <div v-if="cfg" class="grid gap-4 md:grid-cols-2">
    <!-- 左列: WebDAV + 内容 + 加密 -->
    <div class="card p-4 space-y-3">
      <div class="font-semibold">WebDAV 连接</div>
      <div v-if="davUnavailable" class="text-xs text-red-600">
        后端缺少 webdav4 库，备份不可用。请在 backend 安装依赖后重启。
      </div>
      <div>
        <label class="label">服务器地址</label>
        <input v-model="cfg.url" class="input" placeholder="https://dav.jianguoyun.com/dav/" />
        <div class="text-xs text-slate-500 mt-1">坚果云 / Nextcloud / 群晖 / InfiniCLOUD 等任意 WebDAV。</div>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="label">账号</label>
          <input v-model="cfg.username" class="input" autocomplete="off" />
        </div>
        <div>
          <label class="label">密码 {{ cfg.password_set ? '(已设置, 留空保持)' : '' }}</label>
          <input v-model="passwordInput" type="password" class="input"
            :placeholder="cfg.password_set ? '••••' : '应用授权密码'" />
        </div>
      </div>
      <div>
        <label class="label">远程子目录</label>
        <input v-model="cfg.remote_dir" class="input" placeholder="voice-storage-backups" />
      </div>
      <div class="flex gap-2 items-center">
        <button class="btn btn-secondary" @click="testConn">保存并测试连接</button>
        <label class="flex items-center gap-1.5 text-sm ml-auto">
          <input type="checkbox" v-model="cfg.enabled" /> 启用调度
        </label>
      </div>
      <div class="text-sm">{{ testResult }}</div>

      <div class="border-t pt-3">
        <div class="font-medium text-sm mb-1">备份内容 (选择性备份)</div>
        <div class="flex flex-col gap-1.5">
          <label v-for="c in meta.components" :key="c.key" class="flex items-center gap-2 text-sm">
            <input type="checkbox" :checked="cfg.components.includes(c.key)" @change="toggleComponent(c.key)" />
            {{ c.label }}
          </label>
        </div>
      </div>

      <div class="border-t pt-3 space-y-2">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="cfg.encrypt" /> AES-256 口令加密
        </label>
        <div v-if="encUnavailable" class="text-xs text-red-600">
          后端缺少 cryptography 库，加密不可用 (备份会失败)。请安装依赖或关闭加密。
        </div>
        <div>
          <label class="label">加密口令 {{ cfg.passphrase_set ? '(已设置, 留空保持)' : '' }}</label>
          <input v-model="passphraseInput" type="password" class="input"
            :placeholder="cfg.passphrase_set ? '••••' : '用于加解密备份包'" :disabled="!cfg.encrypt" />
          <div class="text-xs text-slate-500 mt-1">⚠️ 口令丢失将无法恢复加密备份，请妥善保管。</div>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="save">保存设置</button>
      <div v-if="errorMsg" class="text-xs text-red-600">{{ errorMsg }}</div>
    </div>

    <!-- 右列: 调度 + 保留 + 立即备份 + 历史 -->
    <div class="card p-4 space-y-3">
      <div class="font-semibold">自动备份 & 保留策略</div>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="label">调度</label>
          <select v-model="cfg.schedule" class="input">
            <option v-for="o in scheduleOpts" :key="o.v" :value="o.v">{{ o.label }}</option>
          </select>
        </div>
        <div>
          <label class="label">触发小时 (每天/每周)</label>
          <input v-model.number="cfg.hour" type="number" min="0" max="23" class="input" />
        </div>
      </div>
      <div class="text-xs text-slate-500">需勾选「启用调度」且非「仅手动」才会自动备份。</div>
      <div class="border-t pt-3">
        <div class="font-medium text-sm mb-1">分层保留 (GFS, 自动清理旧备份)</div>
        <div class="grid grid-cols-3 gap-2">
          <div>
            <label class="label">保留日备</label>
            <input v-model.number="cfg.keep_daily" type="number" min="0" class="input" />
          </div>
          <div>
            <label class="label">保留周备</label>
            <input v-model.number="cfg.keep_weekly" type="number" min="0" class="input" />
          </div>
          <div>
            <label class="label">保留月备</label>
            <input v-model.number="cfg.keep_monthly" type="number" min="0" class="input" />
          </div>
        </div>
        <div class="text-xs text-slate-500 mt-1">每个日/周/月各保留最近 N 个，其余自动删除。</div>
      </div>

      <div class="border-t pt-3 flex flex-wrap gap-2 items-center">
        <button class="btn btn-primary" @click="runNow">立即备份</button>
        <button class="btn btn-secondary" @click="refreshList">刷新列表</button>
        <button class="btn btn-secondary" @click="openRestoreUpload">从文件恢复</button>
        <span class="text-sm">{{ busy }}</span>
      </div>

      <div class="border-t pt-3">
        <div class="font-medium text-sm mb-1">备份历史 (WebDAV)</div>
        <div v-if="listError" class="text-xs text-red-600">{{ listError }}</div>
        <div v-else-if="!backups.length" class="text-xs text-slate-500">暂无备份，点「刷新列表」或「立即备份」。</div>
        <table v-else class="w-full text-xs">
          <thead class="text-slate-500 text-left">
            <tr><th class="py-1">文件</th><th>大小</th><th>时间</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="b in backups" :key="b.name" class="border-t">
              <td class="py-1 break-all">{{ b.name }}{{ b.name.includes('.enc.') ? ' 🔒' : '' }}</td>
              <td>{{ fmtSize(b.size) }}</td>
              <td class="text-slate-500">{{ b.modified }}</td>
              <td class="text-right whitespace-nowrap">
                <a class="text-sky-600 hover:underline" :href="api.downloadBackupUrl(b.name)">下载</a>
                <button class="text-emerald-600 hover:underline ml-2" @click="openRestore(b.name)">恢复</button>
                <button class="text-red-600 hover:underline ml-2" @click="del(b.name)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 恢复对话框 -->
    <div v-if="restoreTarget" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4"
      @click.self="closeRestore">
      <div class="card p-4 space-y-3 max-w-md w-full bg-white">
        <div class="font-semibold">
          恢复 {{ restoreTarget === '__upload__' ? '(从上传文件)' : restoreTarget }}
        </div>
        <div class="text-xs text-red-600">
          ⚠️ 恢复会覆盖当前对应数据。系统会在恢复前自动在服务器端快照当前数据以便回滚。
        </div>
        <div v-if="restoreTarget === '__upload__'">
          <label class="label">备份文件 (.zip / .enc.zip)</label>
          <input type="file" accept=".zip" class="input" @change="onRestoreFile" />
        </div>
        <div>
          <label class="label">恢复内容</label>
          <div class="flex flex-col gap-1.5 mt-1">
            <label v-for="t in meta.restore_targets" :key="t" class="flex items-center gap-2 text-sm">
              <input type="checkbox" :value="t" v-model="restoreTargets" />
              {{ t === 'settings' ? 'app 设置' : t === 'database' ? '数据库 (物品/位置/流水/审计)' : '系统日志' }}
            </label>
          </div>
        </div>
        <div>
          <label class="label">解密口令 (加密备份必填)</label>
          <input v-model="restorePass" type="password" class="input" placeholder="若备份已加密" />
        </div>
        <div class="text-sm">{{ restoreResult }}</div>
        <div class="flex gap-2 justify-end">
          <button class="btn btn-secondary" @click="closeRestore">关闭</button>
          <button class="btn btn-primary" @click="doRestore">确认恢复</button>
        </div>
      </div>
    </div>
  </div>
</template>
