#!/usr/bin/env python3
"""
snapshot.py  —  TradingView チャートのスナップショットを取得する
ZeroClaw の screenshot ツールとの連携、または単独で使用可能。

使い方:
  python3 snapshot.py --symbol BTCUSDT --interval 1H
  python3 snapshot.py --url "https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT"
  python3 snapshot.py --symbol EURUSD --theme dark --width 1920 --height 1080
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Playwright が利用可能か確認 ──────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── 出力ディレクトリ ─────────────────────────
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/workspace/snapshots"))
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ── TradingView のテーマ・インターバル ────────
TV_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1H": "60", "2H": "120", "4H": "240",
    "1D": "D", "1W": "W", "1M": "M",
}


def build_tradingview_url(symbol: str, interval: str, theme: str) -> str:
    """TradingView チャート URL を組み立てる"""
    tv_interval = TV_INTERVALS.get(interval, "D")
    # TradingView の公開チャート URL 形式
    # (ログイン不要・パブリック表示)
    base = "https://www.tradingview.com/chart/"
    params = f"?symbol={symbol}&interval={tv_interval}&theme={theme}&style=1&timezone=Asia%2FTokyo"
    return base + params


def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\-.]', '_', name)


def take_screenshot_playwright(
    url: str,
    output_path: Path,
    width: int = 1440,
    height: int = 900,
    wait_ms: int = 8000,
) -> bool:
    """Playwright (Chromium) でスクリーンショットを撮影"""
    if not PLAYWRIGHT_AVAILABLE:
        print("[ERROR] playwright がインストールされていません")
        print("        pip3 install playwright && playwright install chromium")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )
        page = browser.new_page(viewport={"width": width, "height": height})

        print(f"  → URL を読み込み中: {url}")
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except PWTimeout:
            print("  ⚠ networkidle タイムアウト — そのまま続行します")

        # チャートのレンダリングを待つ
        print(f"  → {wait_ms // 1000}秒 待機中...")
        time.sleep(wait_ms / 1000)

        # 広告・ポップアップを非表示にする CSS を注入
        page.add_style_tag(content="""
            /* TradingView 広告・バナーを非表示 */
            .tv-dialog, .tv-dialog__modal-container,
            div[class*="popup"], div[class*="banner"],
            div[class*="ad-"], div[class*="notification"] {
                display: none !important;
            }
        """)

        page.screenshot(path=str(output_path), full_page=False)
        browser.close()
    return True


def take_screenshot_chromium_cli(
    url: str,
    output_path: Path,
    width: int = 1440,
    height: int = 900,
) -> bool:
    """Chromium CLI でスクリーンショットを撮影 (Playwright の代替)"""
    import subprocess

    chromium_bin = "chromium"
    for candidate in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]:
        if os.system(f"which {candidate} > /dev/null 2>&1") == 0:
            chromium_bin = candidate
            break

    cmd = [
        "xvfb-run", "--auto-servernum",
        chromium_bin,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        f"--window-size={width},{height}",
        f"--screenshot={output_path}",
        url,
    ]
    print(f"  → Chromium CLI で撮影: {' '.join(cmd[:4])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"  [ERROR] {result.stderr[:200]}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="TradingView チャートのスナップショットを取得"
    )
    parser.add_argument("--symbol",   default="BTCUSDT",   help="ティッカーシンボル (例: BTCUSDT, EURUSD)")
    parser.add_argument("--interval", default="1D",        help="インターバル (1m/5m/15m/1H/4H/1D/1W)")
    parser.add_argument("--theme",    default="dark",      choices=["dark", "light"], help="チャートテーマ")
    parser.add_argument("--width",    type=int, default=1440, help="スクリーン幅 (px)")
    parser.add_argument("--height",   type=int, default=900,  help="スクリーン高さ (px)")
    parser.add_argument("--wait",     type=int, default=8000, help="チャートロード待機 (ms)")
    parser.add_argument("--url",      default=None,        help="直接 URL を指定 (--symbol を上書き)")
    parser.add_argument("--output",   default=None,        help="出力ファイルパス (省略で自動命名)")
    parser.add_argument("--method",   default="playwright", choices=["playwright", "chromium"],
                        help="撮影方法")
    args = parser.parse_args()

    # ── URL 決定 ─────────────────────────────
    if args.url:
        url = args.url
        label = sanitize_filename(args.url.split("//")[-1][:40])
    else:
        url = build_tradingview_url(args.symbol, args.interval, args.theme)
        label = f"{sanitize_filename(args.symbol)}_{args.interval}"

    # ── 出力ファイル名 ─────────────────────────
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = SNAPSHOT_DIR / f"tv_{label}_{ts}.png"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print(f"  TradingView スナップショット")
    print(f"  シンボル  : {args.symbol}")
    print(f"  インターバル: {args.interval}")
    print(f"  URL       : {url}")
    print(f"  出力先    : {output_path}")
    print("=" * 50)

    # ── 撮影実行 ─────────────────────────────
    ok = False
    if args.method == "playwright":
        ok = take_screenshot_playwright(url, output_path, args.width, args.height, args.wait)
    else:
        ok = take_screenshot_chromium_cli(url, output_path, args.width, args.height)

    if ok and output_path.exists():
        size_kb = output_path.stat().st_size // 1024
        print(f"\n  ✓ 保存完了: {output_path}  ({size_kb} KB)")
    else:
        print("\n  ✗ スナップショット取得に失敗しました")
        sys.exit(1)


if __name__ == "__main__":
    main()
