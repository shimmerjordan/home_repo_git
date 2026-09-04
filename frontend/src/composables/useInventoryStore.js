// 物品 / 位置的共享数据源。以前物品页、位置页、语音页、3D 页各自 listItems(1000) +
// listLocations(), 开机四路并发拉同一份数据。这里: 模块级单例 + in-flight 去重
// (同时来的几个 load 共享一个 Promise) + 显式 invalidate() 标脏。
//
// 服务端 /api/items 默认过滤 quantity=0; 物品页要看全部所以传 include_depleted。
// 这里统一拉全量, 需要"库存>0"视图的用 activeItems, 与服务端默认过滤等价。
//
// 返回的 items / locations 是所有消费者共享的同一个 ref —— 只能整体替换
// (items.value = 新数组), 禁止原地 sort/splice/修改元素属性, 否则会互相污染
// (BuildingPanel 需要可原地编辑的副本时自己 .slice() 一份, 不改这里)。
import { ref, computed } from 'vue'
import { api } from '../api'

const items = ref([])
const locations = ref([])
let itemsFresh = false
let locationsFresh = false
let itemsInflight = null
let locationsInflight = null
// 每次 invalidate() (或 force 请求) 递增。发起 fetch 时记下当时的 generation,
// resolve 时若 generation 已经过期 (期间又被 invalidate/force 了) 则结果作废:
// 不写 items.value / locations.value, 不标 fresh —— 否则"失效前发出的旧请求"
// 会在失效后把 fresh 重新钉回 true, 直到下次 invalidate 才更正。
let generation = 0

const activeItems = computed(() => items.value.filter((i) => (i.quantity || 0) > 0))

function loadItems(force = false) {
  if (itemsFresh && !force) return Promise.resolve(items.value)
  // force 时哪怕已经有一个在途请求, 也不能复用它 —— 那个请求可能是失效前发出的,
  // 拿到的是变更前的旧数据。递增 generation 作废它, 另起一个。
  if (force) { generation += 1; itemsInflight = null }
  if (!itemsInflight) {
    const gen = generation
    const p = api.listItems({ limit: 1000, include_depleted: true })
      .then((rows) => {
        if (gen === generation) { items.value = rows; itemsFresh = true }
        return rows
      })
      .finally(() => { if (itemsInflight === p) itemsInflight = null })
    itemsInflight = p
  }
  return itemsInflight
}

function loadLocations(force = false) {
  if (locationsFresh && !force) return Promise.resolve(locations.value)
  if (force) { generation += 1; locationsInflight = null }
  if (!locationsInflight) {
    const gen = generation
    const p = api.listLocations()
      .then((rows) => {
        if (gen === generation) { locations.value = rows; locationsFresh = true }
        return rows
      })
      .finally(() => { if (locationsInflight === p) locationsInflight = null })
    locationsInflight = p
  }
  return locationsInflight
}

function loadAll(force = false) {
  return Promise.all([loadLocations(force), loadItems(force)])
}

function invalidate() {
  generation += 1
  itemsFresh = false
  locationsFresh = false
  itemsInflight = null
  locationsInflight = null
}

export function useInventoryStore() {
  return { items, locations, activeItems, loadItems, loadLocations, loadAll, invalidate }
}
