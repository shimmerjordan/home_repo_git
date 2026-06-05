const store = require('../../utils/store.js')
const backup = require('../../utils/backup.js')
const { fmtSize } = require('../../utils/util.js')

Page({
  data: {
    cfg: { url: '', username: '', password: '', remote_dir: 'voice-storage-backups' },
    backups: [],
    msg: '',
    busy: false,
  },
  onShow() {
    const s = store.getSettings()
    if (s.webdav) this.setData({ cfg: Object.assign(this.data.cfg, s.webdav) })
  },
  onField(e) {
    const f = e.currentTarget.dataset.field
    this.setData({ [`cfg.${f}`]: e.detail.value })
  },
  saveCfg() {
    store.setSettings({ webdav: this.data.cfg })
    this.setData({ msg: '✅ 已保存配置' })
  },
  async doBackup() {
    this.saveCfg()
    this.setData({ busy: true, msg: '备份中…' })
    try {
      const r = await backup.runBackup(this.data.cfg)
      this.setData({ msg: `✅ 已备份 ${r.name} (${fmtSize(r.size)})` })
      await this.refresh()
    } catch (e) {
      this.setData({ msg: '❌ ' + (e.message || e) })
    } finally {
      this.setData({ busy: false })
    }
  },
  async refresh() {
    try {
      const list = await backup.listBackups(this.data.cfg)
      this.setData({ backups: list.map((b) => ({ ...b, sizeText: fmtSize(b.size) })) })
    } catch (e) {
      this.setData({ msg: '❌ 列表失败: ' + (e.message || e) })
    }
  },
  restore(e) {
    const name = e.currentTarget.dataset.name
    wx.showModal({
      title: '恢复备份',
      content: `将用「${name}」覆盖本地全部数据，确定？`,
      confirmColor: '#b91c1c',
      success: async (r) => {
        if (!r.confirm) return
        this.setData({ busy: true, msg: '恢复中…' })
        try {
          await backup.restoreFromRemote(this.data.cfg, name, ['data', 'settings'], 'replace')
          this.setData({ msg: '✅ 已恢复，请到「物品」页查看' })
        } catch (err) {
          this.setData({ msg: '❌ ' + (err.message || err) })
        } finally {
          this.setData({ busy: false })
        }
      },
    })
  },
  del(e) {
    const name = e.currentTarget.dataset.name
    wx.showModal({
      title: '删除备份', content: name, confirmColor: '#b91c1c',
      success: async (r) => {
        if (!r.confirm) return
        try { await backup.deleteBackup(this.data.cfg, name); await this.refresh() }
        catch (err) { this.setData({ msg: '❌ ' + (err.message || err) }) }
      },
    })
  },
})
