// 全局 App。小程序无常驻后端: 所有数据在本地, 业务逻辑在 utils/store.js。
const store = require('./utils/store.js')

App({
  globalData: {
    version: '0.1.0-mvp',
  },
  onLaunch() {
    // 首次启动: 若本地无数据, 建一个默认「我家」, 与 NAS 版的 _migrate_to_home 概念一致。
    store.ensureSeed()
  },
})
