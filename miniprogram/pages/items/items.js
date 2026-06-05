const store = require('../../utils/store.js')

Page({
  data: { keyword: '', items: [] },
  onShow() { this.load() },
  load() {
    this.setData({ items: store.listItems({ q: this.data.keyword }) })
  },
  onInput(e) {
    this.setData({ keyword: e.detail.value })
    this.load()
  },
  goEdit(e) {
    const id = e.currentTarget.dataset.id
    wx.navigateTo({ url: `/pages/item-edit/item-edit?id=${id}` })
  },
  goAdd() {
    wx.navigateTo({ url: '/pages/item-edit/item-edit' })
  },
})
