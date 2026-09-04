// 物品 / 位置的共享数据源。以前物品页、位置页、语音页、3D 页各自 listItems(1000) +
// listLocations(), 开机四路并发拉同一份数据。这里: 模块级单例 + in-flight 去重
// (同时来的几个 load 共享一个 Promise) + 显式 invalidate() 标脏。
//
// 服务端 /api/items 默认过滤 quantity=0; 物品页要看全部所以传 include_depleted。
// 这里统一拉全量, 需要"库存>0"视图的用 activeItems, 与服务端默认过滤等价。
import { ref, computed } from 'vue'
import { api } from '../api'

const items = ref([])
const locations = ref([])
let itemsFresh = false
let locationsFresh = false
let itemsInflight = null
let locationsInflight = null

const activeItems = computed(() => items.value.filter((i) => (i.quantity || 0) > 0))

function loadItems(force = false) {
  if (itemsFresh && !force) return Promise.resolve(items.value)
  if (!itemsInflight) {
    itemsInflight = api.listItems({ limit: 1000, include_depleted: true })
      .then((rows) => { items.value = rows; itemsFresh = true; return rows })
      .finally(() => { itemsInflight = null })
  }
  return itemsInflight
}

function loadLocations(force = false) {
  if (locationsFresh && !force) return Promise.resolve(locations.value)
  if (!locationsInflight) {
    locationsInflight = api.listLocations()
      .then((rows) => { locations.value = rows; locationsFresh = true; return rows })
      .finally(() => { locationsInflight = null })
  }
  return locationsInflight
}

function loadAll(force = false) {
  return Promise.all([loadLocations(force), loadItems(force)])
}

function invalidate() {
  itemsFresh = false
  locationsFresh = false
}

export function useInventoryStore() {
  return { items, locations, activeItems, loadItems, loadLocations, loadAll, invalidate }
}
