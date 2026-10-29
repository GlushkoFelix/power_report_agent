# -*- coding: utf-8 -*-
"""SVG → PNG 批量导出工具（线框图渲染）

实现方式：调用本机已安装的 Chromium 内核浏览器（Edge / Chrome）**无头截图**，
不引入任何第三方渲染库（reportlab 的 renderPM 后端需要 Windows 上缺失的 cairo 动态库）。

用法：
    .venv\\Scripts\\python.exe 01.code\\tools\\svg2png.py <svg目录或文件> [--scale 2]

说明：
    - 源 SVG 是唯一事实源，PNG 为派生产物，可随时重新生成
    - 输出 PNG 与源 SVG 同目录同名
    - 路径含中文也能正确处理（通过 Path.as_uri() 做百分号编码）
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

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

SIZE_RE = re.compile(r'width="(\d+)"\s+height="(\d+)"')


def find_browser() -> str:
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("未找到 Edge / Chrome，无法进行无头截图")


def read_size(svg_text: str, default: tuple[int, int] = (1440, 900)) -> tuple[int, int]:
    match = SIZE_RE.search(svg_text)
    if not match:
        return default
    return int(match.group(1)), int(match.group(2))


def convert(browser: str, svg_path: Path, scale: int) -> Path:
    svg_path = svg_path.resolve()  # as_uri() 只接受绝对路径
    svg_text = svg_path.read_text(encoding="utf-8")
    width, height = read_size(svg_text)
    png_path = svg_path.with_suffix(".png")

    if png_path.exists():
        png_path.unlink()
    # 用临时 user-data-dir，避免与正在运行的浏览器实例冲突。
    # `ignore_cleanup_errors=True`：退出 `with` 时 Edge 的 Crashpad 子进程可能仍占用该目录，
    # 清理会抛 `[WinError 145] 目录不是空的` 而把**已渲染成功**的图也当失败（与 mermaid2png.py 同源问题）。
    # 注意：源 SVG 在项目目录里（不在临时目录），所以本工具不受"临时目录被提前删除"影响。
    profile_dir = Path(tempfile.mkdtemp(prefix="svg2png_"))
    try:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
            f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}",
            f"--screenshot={png_path}",
            svg_path.as_uri(),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
        )
        # 浏览器 stderr 可能混用 GBK/UTF-8：**不要**交给子进程管道的解码器（会把装饰器线程
        # 的 UnicodeDecodeError 打到控制台，虽不影响出图但会吓到使用者），自己按 UTF-8 宽松解码
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
        # 等落盘（最多 10 秒）：`subprocess.run` 返回 ≠ PNG 已写完
        for _ in range(40):
            if png_path.exists() and png_path.stat().st_size > 0:
                break
            time.sleep(0.25)
        if not png_path.exists():
            raise RuntimeError(
                f"截图失败（退出码 {result.returncode}）：{stderr_text.strip()[:300]}"
            )
    finally:
        # 清理失败不影响结果（Crashpad 占用时 ignore_errors 兜住）
        shutil.rmtree(profile_dir, ignore_errors=True)
    return png_path


def main() -> int:
    parser = argparse.ArgumentParser(description="批量把 SVG 线框图导出为 PNG（浏览器无头截图）")
    parser.add_argument("target", help="SVG 文件所在目录，或单个 SVG 文件")
    parser.add_argument("--scale", type=int, default=2, help="设备像素比，默认 2（2 倍图）")
    args = parser.parse_args()

    browser = find_browser()
    print(f"使用浏览器：{browser}")

    target = Path(args.target).resolve()
    svgs = [target] if target.is_file() else sorted(target.glob("*.svg"))
    if not svgs:
        print(f"未找到 SVG 文件：{target}")
        return 1

    failed = 0
    for svg in svgs:
        try:
            png = convert(browser, svg, args.scale)
            size_kb = png.stat().st_size // 1024
            print(f"  [ok] {svg.name} → {png.name}（{size_kb} KB）")
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响其余导出
            failed += 1
            print(f"  [fail] {svg.name}: {exc}")
    print(f"完成：成功 {len(svgs) - failed} / {len(svgs)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
