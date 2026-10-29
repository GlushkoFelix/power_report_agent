/**
 * `plotly.js-dist-min` 的最小类型声明。
 *
 * 决策（UIUX 4.6 / 7.2 的落地选择）：不用 `react-plotly.js`（年久失修、类型停留在 React 17），
 * 而是直接调用 `Plotly.react` 自封装容器组件。这里只声明我们实际用到的方法，
 * 避免引入完整 `@types/plotly.js`（体积大且与 dist-min 的默认导出形状不一致）。
 */
declare module 'plotly.js-dist-min' {
  export interface PlotlyHTMLElement extends HTMLElement {
    on: (event: string, handler: (data: unknown) => void) => void;
    removeAllListeners?: (event?: string) => void;
  }

  export interface ToImageOptions {
    format?: 'png' | 'jpeg' | 'webp' | 'svg';
    width?: number;
    height?: number;
    scale?: number;
  }

  export interface PlotlyStatic {
    react: (
      root: PlotlyHTMLElement | HTMLElement,
      data: unknown[],
      layout?: Record<string, unknown>,
      config?: Record<string, unknown>,
    ) => Promise<PlotlyHTMLElement>;
    relayout: (
      root: PlotlyHTMLElement | HTMLElement,
      layout: Record<string, unknown>,
    ) => Promise<PlotlyHTMLElement>;
    toImage: (root: PlotlyHTMLElement | HTMLElement, options?: ToImageOptions) => Promise<string>;
    purge: (root: PlotlyHTMLElement | HTMLElement) => void;
    Plots?: { resize: (root: PlotlyHTMLElement | HTMLElement) => void };
  }

  const Plotly: PlotlyStatic;
  export default Plotly;
}
