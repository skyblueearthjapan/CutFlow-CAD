/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}

// SVGスプライトを raw 文字列として import するため (App.vue で v-html 注入)
declare module '*.svg?raw' {
  const content: string;
  export default content;
}
