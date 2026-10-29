# -*- coding: utf-8 -*-
"""把 Markdown 中的 ```mermaid 代码块批量渲染为 PNG（本地离线）

实现方式：
    1) 从 Markdown 中抽取 ```mermaid 代码块；
    2) 为每个图生成一个临时 HTML（内嵌图源码 + 引用本地 `tools/vendor/mermaid.min.js`）；
    3) 调用本机 Chromium 内核浏览器（Edge / Chrome）无头截图（`--virtual-time-budget` 等 JS 渲染完成）；
    4) 用 Pillow 裁掉四周空白，输出紧凑 PNG。

用法：
    .venv\\Scripts\\python.exe 01.code\\tools\\mermaid2png.py <markdown文件> <输出目录> [--width 1800] [--height 1400] [--scale 2]

图名约定：
    在 mermaid 代码块**上一行**写 HTML 注释指定输出文件名（不含扩展名）：
        <!-- mermaid: arch_layers -->
        ```mermaid
        flowchart LR
        ...
        ```
    未指定时按顺序命名为 fig01、fig02 …
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

BLOCK_RE = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.DOTALL)
NAME_RE = re.compile(r"<!--\s*mermaid:\s*([A-Za-z0-9_\-]+)\s*-->")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>diagram</title>
<script src="__MERMAID_JS__"></script>
<style>
  html, body {{ margin: 0; padding: 0; background: #FFFFFF; }}
  #canvas {{ display: inline-block; padding: 24px; background: #FFFFFF; }}
  .mermaid {{ font-family: "SimHei", "Microsoft YaHei", sans-serif; }}
</style>
</head>
<body>
<div id="canvas">
<pre class="mermaid">
__DIAGRAM__
</pre>
</div>
<script>
  mermaid.initialize({{
    startOnLoad: false,
    securityLevel: 'loose',
    theme: 'base',
    fontFamily: '"SimHei", "Microsoft YaHei", sans-serif',
    themeVariables: {{
      fontFamily: '"SimHei", "Microsoft YaHei", sans-serif',
      fontSize: '15px',
      textColor: '#1F2937',
      primaryTextColor: '#1F2937',
      secondaryTextColor: '#1F2937',
      tertiaryTextColor: '#1F2937',
      lineColor: '#6B7280',
      primaryColor: '#E8EFFD',
      primaryBorderColor: '#1D4ED8',
      clusterBkg: '#F8FAFC',
      clusterBorder: '#ADB5BD',
      edgeLabelBackground: '#FFFFFF',
      actorBkg: '#E8EFFD',
      actorBorder: '#1D4ED8',
      actorTextColor: '#1F2937',
      signalColor: '#6B7280',
      signalTextColor: '#1F2937',
      noteBkgColor: '#FFF4E6',
      noteBorderColor: '#E8590C',
      noteTextColor: '#1F2937'
    }},
    flowchart: {{ useMaxWidth: false, htmlLabels: true, curve: 'basis' }},
    sequence: {{ useMaxWidth: false, mirrorActors: false }},
    er: {{ useMaxWidth: false }},
    gantt: {{ useMaxWidth: false }}
  }});
  mermaid.run({{ querySelector: '.mermaid' }});
</script>
</body>
</html>
"""


def find_browser() -> str:
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("未找到 Edge / Chrome，无法进行无头截图")


def extract_blocks(md_text: str) -> list[tuple[str, str]]:
    """返回 [(图名, 图源码), ...]，图名取自块前的 `<!-- mermaid: name -->` 注释。"""
    blocks: list[tuple[str, str]] = []
    for idx, match in enumerate(BLOCK_RE.finditer(md_text), start=1):
        diagram = match.group(1).strip("\n")
        # 图名注释必须紧邻代码块（往前最多 3 行内），避免误用上一张图的命名
        head_lines = md_text[: match.start()].splitlines()
        name = None
        for line in reversed(head_lines[-3:]):
            found = NAME_RE.search(line)
            if found:
                name = found.group(1)
                break
        if not name:
            name = f"fig{idx:02d}"
        blocks.append((name, diagram))
    return blocks


def crop_whitespace(png_path: Path, padding: int = 12) -> None:
    """裁掉四周纯白边距。"""
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        diff = ImageChops.difference(im, bg).convert("L")
        mask = diff.point(lambda p: 255 if p > 6 else 0)
        bbox = mask.getbbox()
        if not bbox:
            return
        left = max(bbox[0] - padding, 0)
        top = max(bbox[1] - padding, 0)
        right = min(bbox[2] + padding, im.width)
        bottom = min(bbox[3] + padding, im.height)
        im.crop((left, top, right, bottom)).save(png_path)


def render_one(
    browser: str,
    name: str,
    diagram: str,
    out_dir: Path,
    mermaid_js: str,
    width: int,
    height: int,
    scale: int,
    tmp_root: Path,
) -> Path:
    png_path = (out_dir / f"{name}.png").resolve()
    if png_path.exists():
        png_path.unlink()
    # **不要用 `TemporaryDirectory`**：它在 `with` 退出时立即删目录，而 Edge 的 `--screenshot`
    # 是**异步落盘**——父进程可能先返回、子进程才去加载 `file://` 页面，此时页面已被删掉，
    # 截出来的是 **ERR_FILE_NOT_FOUND 错误页**（实测 11 张全变错误页且体积同为 96KB）。
    # 早期表现更迷惑：清理因 Crashpad 占用抛 `WinError 145` 而失败，文件侥幸存活，图反而是对的。
    # 因此改为「本张图独立 mkdtemp + 轮询等 PNG 落盘 + 全流程结束再统一清理」。
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"mermaid_{name}_", dir=tmp_root))
    html_path = tmp_dir / "diagram.html"
    html_path.write_text(
        HTML_TEMPLATE.replace("__MERMAID_JS__", mermaid_js).replace("__DIAGRAM__", diagram),
        encoding="utf-8",
    )
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={tmp_dir / 'profile'}",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=4000",
        f"--screenshot={png_path}",
        html_path.as_uri(),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    # 浏览器 stderr 可能混用 GBK/UTF-8：自己宽松解码，避免子进程管道解码器抛错刷屏
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    # 等落盘（最多 10s）：`subprocess.run` 返回 ≠ PNG 已写完
    for _ in range(40):
        if png_path.exists() and png_path.stat().st_size > 0:
            break
        time.sleep(0.25)
    if not png_path.exists():
        raise RuntimeError(
            f"截图失败（退出码 {result.returncode}）：{stderr_text.strip()[:300]}"
        )
    crop_whitespace(png_path)
    return png_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown 中的 mermaid 块 → PNG（本地离线）")
    parser.add_argument("markdown", help="包含 ```mermaid 代码块的 Markdown 文件")
    parser.add_argument("out_dir", help="PNG 输出目录")
    parser.add_argument("--width", type=int, default=1900, help="渲染视口宽度，默认 1900")
    parser.add_argument("--height", type=int, default=1500, help="渲染视口高度，默认 1500")
    parser.add_argument("--scale", type=int, default=2, help="设备像素比，默认 2（2 倍图）")
    args = parser.parse_args()

    md_path = Path(args.markdown).resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tools_dir = Path(__file__).resolve().parent
    mermaid_js = (tools_dir / "vendor" / "mermaid.min.js").resolve()
    if not mermaid_js.exists():
        print(f"缺少本地 mermaid：{mermaid_js}")
        return 1

    blocks = extract_blocks(md_path.read_text(encoding="utf-8"))
    if not blocks:
        print(f"未在 {md_path.name} 中找到 mermaid 代码块")
        return 1

    browser = find_browser()
    print(f"浏览器：{browser}")
    print(f"mermaid：{mermaid_js.name}（本地离线）")
    # 临时根目录在**整轮渲染期间保持存在**（原因见 `render_one` 的注释），结束再统一清理；
    # 清理失败（Crashpad 占用等）不再影响结果判断
    tmp_root = Path(tempfile.mkdtemp(prefix="mermaid_run_"))
    failed = 0
    try:
        for name, diagram in blocks:
            try:
                png = render_one(
                    browser, name, diagram, out_dir, mermaid_js.as_uri(),
                    args.width, args.height, args.scale, tmp_root,
                )
                size_kb = png.stat().st_size // 1024
                print(f"  [ok] {name}.png（{size_kb} KB）")
            except Exception as exc:  # noqa: BLE001 - 单图失败不影响其余
                failed += 1
                print(f"  [fail] {name}: {exc}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"完成：成功 {len(blocks) - failed} / {len(blocks)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
