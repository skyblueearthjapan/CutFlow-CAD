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

/** Vite env vars consumed by the frontend. */
interface ImportMetaEnv {
  /** Backend base URL (no trailing slash). Default: http://localhost:8080 */
  readonly VITE_API_BASE?: string;
  /** When 'true', force the in-browser mock data path even if API is up. */
  readonly VITE_USE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
