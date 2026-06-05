const store = require('../../utils/store.js')

Page({
  data: {
    id: null,
    form: { name: '', aliases: '', category: '', tags: '', quantity: 1, price: 0, note: '', location_id: null },
    locOptions: [],
    locIndex: 0,
    err: '',
  },
  onLoad(query) {
    const locs = store.listLocations()
    const locOptions = [{ id: null, label: '(未指定)' }].concat(
      locs.map((l) => ({ id: l.id, label: store.locationPath(l.id) }))
    )
    let form = this.data.form
    let id = null
    let locIndex = 0
    if (query && query.id) {
      id = Number(query.id)
      const it = store.getItem(id)
      if (it) {
        form = {
          name: it.name, aliases: it.aliases, category: it.category, tags: it.tags,
          quantity: it.quantity, price: it.price, note: it.note, location_id: it.location_id,
        }
        const idx = locOptions.findIndex((o) => o.id === it.location_id)
        if (idx >= 0) locIndex = idx
      }
      wx.setNavigationBarTitle({ title: '编辑物品' })
    } else {
      wx.setNavigationBarTitle({ title: '新增物品' })
    }
    this.setData({ id, form, locOptions, locIndex })
  },
  onField(e) {
    const f = e.currentTarget.dataset.field
    this.setData({ [`form.${f}`]: e.detail.value })
  },
  onLoc(e) {
    const idx = Number(e.detail.value)
    this.setData({ locIndex: idx, 'form.location_id': this.data.locOptions[idx].id })
  },
  save() {
    const f = this.data.form
    if (!f.name || !f.name.trim()) { this.setData({ err: '名称不能为空' }); return }
    const payload = {
      ...f,
      quantity: Number(f.quantity) || 0,
      price: Number(f.price) || 0,
    }
    if (this.data.id) store.updateItem(this.data.id, payload)
    else store.createItem(payload)
    wx.navigateBack()
  },
  del() {
    if (!this.data.id) return
    wx.showModal({
      title: '删除物品', content: '同时删除其流水记录，不可恢复。', confirmColor: '#b91c1c',
      success: (r) => {
        if (r.confirm) { store.deleteItem(this.data.id); wx.navigateBack() }
      },
    })
  },
  tx(e) {
    if (!this.data.id) { this.setData({ err: '请先保存物品再记流水' }); return }
    const action = e.currentTarget.dataset.action
    store.recordTransaction(this.data.id, { action, quantity: 1 })
    const it = store.getItem(this.data.id)
    this.setData({ 'form.quantity': it.quantity })
    wx.showToast({ title: action === 'take_out' ? '已取出 1' : '已存入 1', icon: 'none' })
  },
})
