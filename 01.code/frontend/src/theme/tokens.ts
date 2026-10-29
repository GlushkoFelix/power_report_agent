/**
 * 语义色 / 用途色板常量与 Plotly 主题注入（UIUX 4.2 / 7.2）。
 *
 * 为什么 JS 里还要存一份十六进制色值？——**Plotly 不认 CSS 变量**（figure 里的颜色必须是字面量）。
 * 因此这里的两份色板与 `styles/index.css` 的变量**一一对应**，改动时必须同步；
 * 分为 `light` / `dark` 两套，切换主题时重新 `Plotly.react` 注入。
 *
 * 后端 figure 只写语义色与文案、**不写主题色**（`paper_bgcolor` / `font.color` / 网格色一律不写），
 * 亮暗主题与"导出固定亮色底"完全由前端在这里接管（UIUX 7.2 的落地约定）。
 */

export type ThemeMode = 'light' | 'dark';

/** 语义色板（时段拆分专用：峰/平/谷/未计量/异常/缺采） */
export const SEMANTIC_COLORS: Record<ThemeMode, Record<string, string>> = {
  light: {
    peak: '#E8590C',
    flat: '#1D4ED8',
    valley: '#0CA678',
    unmetered: '#7048E8',
    anomaly: '#E03131',
    missing: '#ADB5BD',
  },
  dark: {
    peak: '#FF7A33',
    flat: '#5B82EE',
    valley: '#2FD4A0',
    unmetered: '#8F6BF0',
    anomaly: '#FF5A5A',
    missing: '#8B94A3',
  },
};

/** 用途色板（按用途拆分专用，与语义色**互不共用**——PRD 红线 R3 的视觉层要求） */
export const PURPOSE_COLORS: Record<ThemeMode, Record<string, string>> = {
  light: {
    kitchen: '#0B7285',
    laundry: '#15AABF',
    hvac: '#4C6EF5',
    unmetered: '#7048E8',
  },
  dark: {
    kitchen: '#12909F',
    laundry: '#2FC0D4',
    hvac: '#6F8BFF',
    unmetered: '#8F6BF0',
  },
};

/** Plotly 主题（纸面/绘图区/字体/网格） */
export const PLOTLY_THEME: Record<ThemeMode, Record<string, string>> = {
  light: {
    paper_bgcolor: '#FFFFFF',
    plot_bgcolor: '#FFFFFF',
    fontColor: '#1F2937',
    gridcolor: '#EEF1F5',
    zerolinecolor: '#D9DEE7',
  },
  dark: {
    paper_bgcolor: '#1C2230',
    plot_bgcolor: '#1C2230',
    fontColor: '#E5E9F0',
    gridcolor: '#2A3242',
    zerolinecolor: '#313A4D',
  },
};

/** 全站图例说明用的语义色标签（右栏图例固定展示，UIUX 5.1 区域 7） */
export const LEGEND_ITEMS: { key: string; label: string; cssVar: string }[] = [
  { key: 'peak', label: '峰', cssVar: '--c-peak' },
  { key: 'flat', label: '平', cssVar: '--c-flat' },
  { key: 'valley', label: '谷', cssVar: '--c-valley' },
  { key: 'unmetered', label: '未计量', cssVar: '--c-unmetered' },
  { key: 'anomaly', label: '异常', cssVar: '--c-anomaly' },
  { key: 'missing', label: '缺采', cssVar: '--c-missing' },
];

/**
 * 把前端主题注入到后端返回的 figure 上（**浅拷贝，不改原对象**）。
 *
 * 注入内容：纸面/绘图区底色、字体色与字号、网格与零线色、图例与边距、hover 样式；
 * 后端已写的语义色（trace 颜色）与文案一律保留。
 */
export function themedFigure(
  figure: Record<string, unknown>,
  mode: ThemeMode,
): Record<string, unknown> {
  const t = PLOTLY_THEME[mode];
  const layout = (figure.layout ?? {}) as Record<string, unknown>;
  const axisPatch = {
    gridcolor: t.gridcolor,
    zerolinecolor: t.zerolinecolor,
    linecolor: t.zerolinecolor,
    tickfont: { color: t.fontColor, size: 11 },
    title: { font: { color: t.fontColor, size: 11 } },
  };
  const patchAxis = (value: unknown) => {
    if (Array.isArray(value)) return value.map((v) => ({ ...(v as object), ...axisPatch }));
    return { ...((value as object) ?? {}), ...axisPatch };
  };

  return {
    ...figure,
    layout: {
      ...layout,
      paper_bgcolor: t.paper_bgcolor,
      plot_bgcolor: t.plot_bgcolor,
      font: { color: t.fontColor, size: 11 },
      margin: { l: 48, r: 16, t: 28, b: 40 },
      hoverlabel: {
        bgcolor: t.paper_bgcolor,
        bordercolor: t.zerolinecolor,
        font: { color: t.fontColor, size: 11 },
      },
      legend: { orientation: 'h', y: -0.18, font: { color: t.fontColor, size: 11 } },
      xaxis: patchAxis(layout.xaxis),
      yaxis: patchAxis(layout.yaxis),
    },
  };
}
