/**
 * Tailwind 配置（UIUX 4.6：布局与视觉用 Tailwind，交互组件用 AntD）。
 *
 * 关键点：**关闭 preflight**（`corePlugins.preflight: false`）——AntD 自带 reset，
 * 两者叠加会互相覆盖（按钮默认样式、标题 margin 等），关掉 preflight 后
 * Tailwind 只提供 utility，不与 AntD 争基础样式（UIUX 4.6「StyleProvider layer」方案的配套）。
 *
 * 颜色统一走 CSS 变量（见 src/styles/index.css），因此暗色主题只需替换变量，
 * Tailwind 侧不用写 `dark:` 前缀。
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: 'var(--c-primary)',
          light: 'var(--c-primary-light)',
          hover: 'var(--c-primary-hover)',
        },
        text: {
          main: 'var(--c-text-main)',
          sub: 'var(--c-text-sub)',
          disabled: 'var(--c-text-disabled)',
        },
        surface: {
          page: 'var(--c-bg-page)',
          card: 'var(--c-bg-card)',
          weak: 'var(--c-bg-weak)',
          weak2: 'var(--c-bg-weak2)',
        },
        line: {
          DEFAULT: 'var(--c-line)',
          soft: 'var(--c-line-soft)',
        },
        // 语义色（UIUX 4.2；全站统一，严禁混用）
        peak: 'var(--c-peak)',
        flat: 'var(--c-flat)',
        valley: 'var(--c-valley)',
        unmetered: 'var(--c-unmetered)',
        anomaly: 'var(--c-anomaly)',
        missing: 'var(--c-missing)',
        // 用途色板（与时段语义色互不共用，PRD R3）
        use1: 'var(--c-use-kitchen)',
        use2: 'var(--c-use-laundry)',
        use3: 'var(--c-use-hvac)',
      },
      borderRadius: {
        card: '10px',
        btn: '8px',
        input: '6px',
        chip: '13px',
        tag: '4px',
      },
      fontSize: {
        h1: ['20px', '28px'],
        h2: ['16px', '24px'],
        h3: ['14px', '22px'],
        body: ['12px', '20px'],
        table: ['11px', '18px'],
        metric: ['24px', '32px'],
      },
      fontFamily: {
        sans: ['"Microsoft YaHei"', '"PingFang SC"', 'system-ui', 'sans-serif'],
        mono: ['Consolas', '"Courier New"', 'monospace'],
      },
    },
  },
  plugins: [],
};
