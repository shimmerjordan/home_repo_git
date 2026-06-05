const store = require('../../utils/store.js')

// 平面可视化 (Canvas 2D, 无依赖)。把每个房间画成方块, 标注名称与物品数。
// 有 geometry (从 NAS 版导入的数据) 时按 x/z/w/d 摆放; 否则自动网格排布。
// 注: 完整 3D (Three.js) 见调研报告, 属后续阶段; 这里用 2D 平面图作为 MVP 可视化。

Page({
  data: { rooms: [], empty: false },
  onShow() { this.compute() },

  compute() {
    const locs = store.listLocations()
    const items = store.listItems()
    // 统计每个位置 (及其子孙) 的物品数。
    const childrenOf = {}
    locs.forEach((l) => { (childrenOf[l.parent_id] = childrenOf[l.parent_id] || []).push(l) })
    const countDirect = {}
    items.forEach((it) => { if (it.location_id != null) countDirect[it.location_id] = (countDirect[it.location_id] || 0) + 1 })
    const totalCount = (id) => {
      let n = countDirect[id] || 0
      ;(childrenOf[id] || []).forEach((c) => { n += totalCount(c.id) })
      return n
    }
    // 取"房间"层 (kind room), 没有则取所有非 home 顶层。
    let rooms = locs.filter((l) => l.kind === 'room')
    if (!rooms.length) rooms = locs.filter((l) => l.kind !== 'home')
    const data = rooms.map((r) => ({
      id: r.id, name: r.name, count: totalCount(r.id),
      geo: r.geometry && typeof r.geometry === 'object' ? r.geometry : null,
    }))
    this._rooms = data
    // setData 回调里再画, 确保 canvas 节点已按 empty 状态渲染完成 (避免拿不到节点)。
    this.setData({ rooms: data, empty: data.length === 0 }, () => this.draw())
  },

  draw() {
    const q = wx.createSelectorQuery()
    q.select('#plan').fields({ node: true, size: true }).exec((res) => {
      if (!res || !res[0] || !res[0].node) return
      const canvas = res[0].node
      const ctx = canvas.getContext('2d')
      // 优先 getWindowInfo (新), 回退 getSystemInfoSync (旧库)。
      let dpr = 2
      try { dpr = (wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()).pixelRatio || 2 } catch (e) {}
      const W = res[0].width, H = res[0].height
      canvas.width = W * dpr; canvas.height = H * dpr
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, W, H)
      const rooms = this._rooms || []
      if (!rooms.length) return

      const hasGeo = rooms.some((r) => r.geo && r.geo.w)
      if (hasGeo) this._drawGeo(ctx, W, H, rooms)
      else this._drawGrid(ctx, W, H, rooms)
    })
  },

  _drawRoom(ctx, x, y, w, h, room) {
    ctx.fillStyle = '#e0f2fe'
    ctx.strokeStyle = '#0ea5e9'
    ctx.lineWidth = 2
    ctx.fillRect(x, y, w, h)
    ctx.strokeRect(x, y, w, h)
    ctx.fillStyle = '#0f172a'
    ctx.font = '14px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(room.name, x + w / 2, y + h / 2 - 4, w - 8)
    ctx.fillStyle = '#0369a1'
    ctx.font = '12px sans-serif'
    ctx.fillText(`${room.count} 件`, x + w / 2, y + h / 2 + 16, w - 8)
  },

  _drawGrid(ctx, W, H, rooms) {
    const pad = 12
    const cols = Math.ceil(Math.sqrt(rooms.length))
    const rows = Math.ceil(rooms.length / cols)
    const cw = (W - pad * (cols + 1)) / cols
    const ch = Math.min((H - pad * (rows + 1)) / rows, 110)
    rooms.forEach((r, i) => {
      const c = i % cols, rr = Math.floor(i / cols)
      this._drawRoom(ctx, pad + c * (cw + pad), pad + rr * (ch + pad), cw, ch, r)
    })
  },

  _drawGeo(ctx, W, H, rooms) {
    // 用 geometry 的 x/z 为中心, w/d 为尺寸, 自适应缩放到画布。
    const gs = rooms.filter((r) => r.geo && r.geo.w)
    let minX = Infinity, minZ = Infinity, maxX = -Infinity, maxZ = -Infinity
    gs.forEach((r) => {
      const g = r.geo
      minX = Math.min(minX, g.x - g.w / 2); maxX = Math.max(maxX, g.x + g.w / 2)
      minZ = Math.min(minZ, g.z - g.d / 2); maxZ = Math.max(maxZ, g.z + g.d / 2)
    })
    const pad = 16
    const sx = (W - pad * 2) / Math.max(0.1, maxX - minX)
    const sz = (H - pad * 2) / Math.max(0.1, maxZ - minZ)
    const s = Math.min(sx, sz)
    gs.forEach((r) => {
      const g = r.geo
      const x = pad + (g.x - g.w / 2 - minX) * s
      const y = pad + (g.z - g.d / 2 - minZ) * s
      this._drawRoom(ctx, x, y, g.w * s, g.d * s, r)
    })
  },
})
