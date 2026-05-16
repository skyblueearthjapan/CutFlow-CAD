import { createApp } from 'vue';
import App from './App.vue';

// Design tokens → base reset → component styles の順で読み込む。
// この順序を変えると CSS 変数の解決順が壊れる。
import './assets/styles/tokens.css';
import './assets/styles/base.css';
import './assets/styles/components.css';

createApp(App).mount('#app');
