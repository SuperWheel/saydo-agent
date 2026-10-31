import { createApp } from 'vue'
import Vant from 'vant'
import 'vant/lib/index.css'
import './theme.css'
import './style.css'
import App from './App.vue'
import IconText from './components/IconText.vue'

createApp(App).use(Vant).component('IconText', IconText).mount('#app')
