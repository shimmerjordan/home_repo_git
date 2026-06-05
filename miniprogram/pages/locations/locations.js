const store = require('../../utils/store.js')

const KINDS = [
  { v: 'home', label: '家' },
  { v: 'room', label: '房间' },
  { v: 'box', label: '容器/箱子' },
  { v: 'shelf', label: '货架' },
  { v: 'drawer', label: '抽屉/层' },
]

Page({
  data: {
    locs: [],
    parentOptions: [],
    kinds: KINDS,
    newName: '',
    kindIndex: 1,
    parentIndex: 0,
    err: '',
  },
  onShow() { this.load() },
  load() {
    const all = store.listLocations()
    const locs = all.map((l) => ({ id: l.id, kind: l.kind, label: store.locationPath(l.id) }))
      .sort((a, b) => a.label.localeCompare(b.label))
    const parentOptions = [{ id: null, label: '(顶层)' }].concat(
      all.map((l) => ({ id: l.id, label: store.locationPath(l.id) }))
    )
    this.setData({ locs, parentOptions })
  },
  onName(e) { this.setData({ newName: e.detail.value }) },
  onKind(e) { this.setData({ kindIndex: Number(e.detail.value) }) },
  onParent(e) { this.setData({ parentIndex: Number(e.detail.value) }) },
  add() {
    const name = (this.data.newName || '').trim()
    if (!name) { this.setData({ err: '请输入名称' }); return }
    store.createLocation({
      name,
      kind: KINDS[this.data.kindIndex].v,
      parent_id: this.data.parentOptions[this.data.parentIndex].id,
    })
    this.setData({ newName: '', err: '' })
    this.load()
  },
  rename(e) {
    const id = e.currentTarget.dataset.id
    const cur = store.getLocation(id)
    wx.showModal({
      title: '重命名', editable: true, content: cur ? cur.name : '',
      success: (r) => {
        if (r.confirm && r.content && r.content.trim()) {
          store.updateLocation(id, { name: r.content.trim() })
          this.load()
        }
      },
    })
  },
  del(e) {
    const id = e.currentTarget.dataset.id
    wx.showModal({
      title: '删除位置', content: '若其下有物品或子位置将无法删除。', confirmColor: '#b91c1c',
      success: (r) => {
        if (!r.confirm) return
        try { store.deleteLocation(id); this.load() }
        catch (err) { wx.showToast({ title: err.message, icon: 'none' }) }
      },
    })
  },
})
