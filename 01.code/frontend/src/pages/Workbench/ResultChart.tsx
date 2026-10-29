/**
 * Plotly 容器（UIUX 7.2 落地约定）。
 *
 * 关键分工：**后端 figure 只写语义色与文案，主题色全部由前端注入**——
 * `themedFigure()` 每次渲染按当前主题补上纸面/绘图区/字体/网格色，
 * 这样"图表随主题切换、导出固定亮色底"只需一套前端代码。
 *
 * 交互：hover tooltip 默认开、**框选缩放默认关**（避免误操作，UIUX 7.2）；
 * 点击数据点回调 `onPointClick(index)` 供表格联动定位。
 */

import { useEffect, useRef, useState } from 'react';
import Plotly, { type PlotlyHTMLElement } from 'plotly.js-dist-min';

import { themedFigure, type ThemeMode } from '@/theme/tokens';

interface Props {
  figure: Record<string, unknown>;
  mode: ThemeMode;
  height?: number;
  onPointClick?: (index: number) => void;
  /** 把 graph div 暴露给父组件（导出图片时需要在同一节点上临时改主题） */
  onReady?: (node: PlotlyHTMLElement) => void;
}

export default function ResultChart({ figure, mode, height = 300, onPointClick, onReady }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  // 用 state 持有 Plotly 节点：点击回调的 effect 需要它作依赖（ref 变更不触发重跑）
  const [node, setNode] = useState<PlotlyHTMLElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !figure) return;

    const themed = themedFigure(figure, mode);
    const data = (themed.data as unknown[]) ?? [];
    const layout = (themed.layout as Record<string, unknown>) ?? {};
    let alive = true;

    void Plotly.react(
      container as unknown as PlotlyHTMLElement,
      data,
      { ...layout, height, autosize: true },
      {
        displayModeBar: false,
        responsive: true,
        // 框选缩放默认关闭（UIUX 7.2）
        scrollZoom: false,
        doubleClick: 'reset',
      },
    ).then((created) => {
      if (!alive) return;
      setNode(created);
      onReady?.(created);
    });

    return () => {
      alive = false;
    };
  }, [figure, mode, height, onReady]);

  useEffect(() => {
    if (!node || !onPointClick) return;
    node.on('plotly_click', (data: unknown) => {
      const points = (data as { points?: { pointNumber?: number; pointIndex?: number }[] }).points;
      const index = points?.[0]?.pointNumber ?? points?.[0]?.pointIndex;
      if (typeof index === 'number') onPointClick(index);
    });
  }, [node, onPointClick]);

  // 卸载时释放 Plotly 实例，避免内存泄漏
  useEffect(
    () => () => {
      if (containerRef.current) {
        try {
          Plotly.purge(containerRef.current as unknown as PlotlyHTMLElement);
        } catch {
          /* 已卸载的节点忽略 */
        }
      }
    },
    [],
  );

  return <div ref={containerRef} style={{ width: '100%', height }} />;
}

/** 导出当前图为 PNG（2 倍图、**固定亮色底**，UIUX 7.2 / S7） */
export async function exportFigurePng(node: PlotlyHTMLElement, fileName: string): Promise<void> {
  // 导出要固定亮色底，但**导出后必须把当前主题改回去**：
  // 旧实现 relayout 成亮色底后不再还原 —— 暗色模式下点一次导出，图就永久变成白底。
  // `PlotlyHTMLElement` 类型未声明 `layout`（运行时确实存在），按需宽松取值
  const rawNode = node as unknown as { layout?: Record<string, unknown> };
  const layout = rawNode.layout ?? {};
  const font = (layout.font ?? {}) as Record<string, unknown>;
  const previous = {
    paper_bgcolor: layout.paper_bgcolor,
    plot_bgcolor: layout.plot_bgcolor,
    'font.color': font.color,
  };
  let url = '';
  try {
    await Plotly.relayout(node, {
      paper_bgcolor: '#FFFFFF',
      plot_bgcolor: '#FFFFFF',
      'font.color': '#1F2937',
    });
    url = await Plotly.toImage(node, { format: 'png', width: 1200, height: 600, scale: 2 });
    const link = document.createElement('a');
    link.href = url;
    link.download = `${fileName}.png`;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove(); // 旧实现不挂载也不回收；这里显式挂载再移除，避免残留可点击的下载锚点
  } finally {
    try {
      await Plotly.relayout(node, previous);
    } catch {
      /* 图已卸载（切会话）时忽略 */
    }
    if (url.startsWith('blob:')) URL.revokeObjectURL(url);
  }
}
