// 语音/文本 -> 结构化意图 -> 本地 store 操作。
// 移植自 NAS 版 backend/app/llm/intent.py (精简但忠实)。
// parseIntent 接受可注入的 chatFn, 便于在 node 里脱离微信测试解析+执行逻辑。

const store = require('./store.js')
const llm = require('./llm.js')

const SYSTEM_PROMPT = `你是家庭仓储管家的语义解析器, 同时给出温暖口语化的中文回答。
1. 阅读用户语句和库存摘要 (每行: [id] 名称 ×数量 @位置 #分类)
2. 选择意图: find / take_out / put_in / consume / create_item / list / unknown
   - take_out: 借出待归位 (拿/借走)
   - consume: 用完不归位 (吃了/喝了/用完/扔了/送了)
   - put_in: 归位/存入
   - create_item: 新增物品 (需 item_name, 可选 location_id)
3. 在候选里按名称/别名/分类匹配最可能的物品, 命中填 item_id
4. confidence 0~1: 名称明确一致 0.9+; 别名/语义推断 0.6~0.85; 模糊 <0.5; 没找到 <0.3
5. speech: 一句温暖中文回答 (<=60字)
只输出一个 JSON 对象, 不要多余文字:
{"intent":"...","confidence":0.0,"speech":"...","item_id":null,"item_name":null,"location_id":null,"quantity":1,"candidates":[],"reasoning":"..."}`

// 紧凑库存摘要 (关键词预筛, 控制 token)。
function buildSummary(text, limit) {
  limit = limit || 40
  const kw = String(text || '').toLowerCase()
  let items = store.listItems()
  // 简单相关性: 命中关键词的优先, 再补足到 limit。
  const score = (it) => {
    const hay = [it.name, it.aliases, it.category, it.tags].join(' ').toLowerCase()
    let s = 0
    kw.split(/[\s,，。]+/).filter(Boolean).forEach((t) => { if (hay.includes(t)) s++ })
    return s
  }
  items = items
    .map((it) => ({ it, s: score(it) }))
    .sort((a, b) => b.s - a.s || a.it.id - b.it.id)
    .slice(0, limit)
    .map((x) => x.it)
  const lines = items.map(
    (it) => `[${it.id}] ${it.name}${it.aliases ? '(' + it.aliases + ')' : ''} ×${it.quantity}` +
      `${it.location_path ? ' @' + it.location_path : ''}${it.category ? ' #' + it.category : ''}`
  )
  return lines.join('\n') || '(库存为空)'
}

// chatFn 可选注入 (默认用 llm.chat); 返回 {parsed, summary}
async function parseIntent(text, llmCfg, chatFn) {
  const summary = buildSummary(text)
  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: `用户语句: ${text}\n\n当前库存摘要:\n${summary}\n\n请解析意图, 只输出 JSON。` },
  ]
  const fn = chatFn || llm.chat
  const content = await fn(llmCfg, messages)
  const parsed = llm.parseJSON(content)
  parsed.intent = parsed.intent || 'unknown'
  parsed.confidence = Math.max(0, Math.min(1, Number(parsed.confidence) || 0))
  parsed.speech = parsed.speech || ''
  parsed.quantity = Number(parsed.quantity) || 1
  parsed.candidates = parsed.candidates || []
  return { parsed, summary }
}

// 执行意图, 直接读写本地 store。confidenceThreshold 以下的写操作返回待确认。
// 返回 { intent, speech, executed, needs_confirmation, pending, candidates }
function executeIntent(text, parsed, confidenceThreshold) {
  const threshold = confidenceThreshold != null ? confidenceThreshold : 0.5
  const intent = parsed.intent || 'unknown'
  const conf = Number(parsed.confidence) || 0
  const qty = Number(parsed.quantity) || 1

  // 候选: item_id 优先, 再加 candidates, 再用关键词兜底搜索。
  let ids = []
  if (parsed.item_id != null) ids.push(Number(parsed.item_id))
  ;(parsed.candidates || []).forEach((c) => { if (ids.indexOf(Number(c)) < 0) ids.push(Number(c)) })
  let candItems = ids.map((id) => store.getItem(id)).filter(Boolean)
  if (!candItems.length) candItems = store.listItems({ q: text }).slice(0, 5)

  const base = {
    intent, confidence: conf, speech: parsed.speech || '', executed: false,
    needs_confirmation: false, pending: null,
    candidates: candItems.map((it) => ({ item_id: it.id, item_name: it.name, location_path: it.location_path })),
  }

  if (intent === 'find' || intent === 'list') {
    if (intent === 'find' && candItems.length && !base.speech) {
      const top = candItems[0]
      base.speech = `${conf >= 0.85 ? '找到啦, ' : '你可能想找的是'}${top.name}在${top.location_path || '未登记位置'}, 库存${top.quantity}个`
    } else if (intent === 'find' && !candItems.length) {
      base.speech = base.speech || '暂未找到这种东西, 可能还没登记进来'
    }
    return base
  }

  if (intent === 'create_item') {
    const name = (parsed.item_name || '').trim()
    if (!name) { base.intent = 'unknown'; base.speech = base.speech || '请告诉我物品名称'; return base }
    const ln = Number(parsed.location_id)
    const loc = parsed.location_id != null && Number.isFinite(ln) ? ln : null
    const it = store.createItem({ name, location_id: loc, quantity: qty })
    base.executed = true
    base.candidates = [{ item_id: it.id, item_name: it.name, location_path: it.location_path }]
    base.speech = base.speech || `记下啦, ${name} 放在 ${it.location_path || '未指定位置'} 了`
    return base
  }

  if (intent === 'take_out' || intent === 'put_in' || intent === 'consume') {
    const target = candItems[0]
    if (!target) { base.intent = 'unknown'; base.speech = base.speech || '没找到这个物品, 要不要先创建一个'; return base }
    if (conf < threshold) {
      base.needs_confirmation = true
      base.pending = { intent, item_id: target.id, location_id: parsed.location_id != null ? Number(parsed.location_id) : null, quantity: qty }
      const verb = { take_out: '取出', put_in: '存放', consume: '消耗' }[intent]
      base.speech = base.speech || `我不太确定, 你是想${verb}${target.name}吗`
      return base
    }
    return confirmAction({ intent, item_id: target.id, location_id: parsed.location_id, quantity: qty }, base)
  }

  base.speech = base.speech || '没听懂呢, 能换个说法吗'
  return base
}

// 执行一个已确认的写操作 (供低置信度确认后调用)。
function confirmAction(pending, base) {
  base = base || { executed: false, candidates: [] }
  const it = store.getItem(pending.item_id)
  if (!it) { base.intent = 'unknown'; base.speech = '物品不存在了'; return base }
  const qty = Number(pending.quantity) || 1
  const action = pending.intent
  // location_id 来自 LLM, 可能是非数字 -> 仅在能解析为有效数字时采用, 避免写入 NaN。
  const locNum = Number(pending.location_id)
  const loc = pending.location_id != null && Number.isFinite(locNum) ? locNum : null
  // put_in 指定了新位置时, 同步物品位置 (与后端一致)。
  if (action === 'put_in' && loc != null) {
    store.updateItem(it.id, { location_id: loc })
  }
  const data = { action, quantity: qty }
  if (loc != null) data.location_id = loc
  store.recordTransaction(it.id, data)
  const after = store.getItem(it.id)
  base.executed = true
  base.intent = action
  if (action === 'take_out') base.speech = `已取出${it.name} ${qty}个, 用完记得归位哦, 当前余量${after.quantity}`
  else if (action === 'consume') base.speech = `已记录用完${it.name} ${qty}个, 剩${after.quantity}个`
  else base.speech = `好的, 已存入${it.name} ${qty}个到${after.location_path || '原位置'}, 现在共${after.quantity}件`
  base.candidates = [{ item_id: after.id, item_name: after.name, location_path: after.location_path }]
  return base
}

module.exports = { SYSTEM_PROMPT, buildSummary, parseIntent, executeIntent, confirmAction }
