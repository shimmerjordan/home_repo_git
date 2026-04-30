<script setup>
import { ref, watch } from 'vue'

const props = defineProps({ item: Object, locations: Array })
const emit = defineEmits(['cancel', 'save'])

const form = ref({
  name: '', aliases: '', category: '', tags: '',
  quantity: 1, price: 0, note: '', location_id: null,
  pos_x: null, pos_z: null,
})

watch(() => props.item, (v) => {
  if (v) form.value = { ...v }
}, { immediate: true })

function submit() {
  if (!form.value.name?.trim()) return
  const payload = { ...form.value, location_id: form.value.location_id || null }
  // Empty strings → null so backend stores them as NULL.
  payload.pos_x = (form.value.pos_x === '' || form.value.pos_x == null) ? null : +form.value.pos_x
  payload.pos_z = (form.value.pos_z === '' || form.value.pos_z == null) ? null : +form.value.pos_z
  emit('save', payload)
}

function clearPos() {
  form.value.pos_x = null
  form.value.pos_z = null
}
</script>

<template>
  <div class="fixed inset-0 bg-black/40 flex items-center justify-center z-20 p-4" @click.self="$emit('cancel')">
    <div class="card p-4 w-full max-w-lg space-y-3">
      <div class="font-semibold">{{ item ? '编辑物品' : '新增物品' }}</div>
      <div class="grid grid-cols-2 gap-3">
        <div class="col-span-2">
          <label class="label">名称 *</label>
          <input v-model="form.name" class="input" />
        </div>
        <div class="col-span-2">
          <label class="label">别名 (用 / 或 , 分隔)</label>
          <input v-model="form.aliases" class="input" placeholder="充电宝,移动电源" />
        </div>
        <div>
          <label class="label">分类</label>
          <input v-model="form.category" class="input" />
        </div>
        <div>
          <label class="label">标签</label>
          <input v-model="form.tags" class="input" />
        </div>
        <div>
          <label class="label">数量</label>
          <input v-model.number="form.quantity" type="number" class="input" />
        </div>
        <div>
          <label class="label">单价</label>
          <input v-model.number="form.price" type="number" step="0.01" class="input" />
        </div>
        <div class="col-span-2">
          <label class="label">位置</label>
          <select v-model="form.location_id" class="input">
            <option :value="null">—</option>
            <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.full_path }}</option>
          </select>
        </div>
        <div class="col-span-2">
          <label class="label">在父容器内位置 (米, 相对中心,留空=自动网格)</label>
          <div class="flex gap-2 items-center">
            <input v-model.number="form.pos_x" type="number" step="0.05" placeholder="x"
                   class="input flex-1" />
            <input v-model.number="form.pos_z" type="number" step="0.05" placeholder="z"
                   class="input flex-1" />
            <button class="btn btn-secondary text-xs" type="button" @click="clearPos">清空</button>
          </div>
        </div>
        <div class="col-span-2">
          <label class="label">备注</label>
          <textarea v-model="form.note" class="input" rows="2"></textarea>
        </div>
      </div>
      <div class="flex justify-end gap-2">
        <button class="btn btn-secondary" @click="$emit('cancel')">取消</button>
        <button class="btn btn-primary" @click="submit">保存</button>
      </div>
    </div>
  </div>
</template>
