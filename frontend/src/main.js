import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { installClientLog } from './composables/useClientLog'

installClientLog()
createApp(App).mount('#app')
