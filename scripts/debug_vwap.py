#!/usr/bin/env python3
"""
debug_vwap.py — TradingViewのIndicatorsボタン操作をデバッグする
実行: python3 /workspace/scripts/debug_vwap.py
"""
import time
from playwright.sync_api import sync_playwright

URL = (
    "https://www.tradingview.com/chart/"
    "?symbol=TSE%3A5016&interval=1"
    "&theme=dark&style=1&timezone=Asia%2FTokyo"
)
SCREENSHOT_DIR = "/workspace/snapshots/debug"

import os
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu",
              "--window-size=1920,1080"],
    )
    page = browser.new_page(viewport={"width": 1920, "height": 1080})

    print("▸ TradingViewを開いています...")
    page.goto(URL, wait_until="networkidle", timeout=40_000)
    time.sleep(8)

    page.add_style_tag(content="""
        .tv-dialog, div[class*="popup"], div[class*="banner"],
        div[class*="toast"], div[class*="cookie"] { display: none !important; }
    """)
    time.sleep(1)

    # ① ページ読み込み後のスクリーンショット
    page.screenshot(path=f"{SCREENSHOT_DIR}/step1_loaded.png")
    print("  ✓ step1_loaded.png — チャート表示確認")

    # ② ボタン候補を全部リストアップ
    print("\n▸ ページ上のボタン一覧 (aria-label付き):")
    buttons = page.locator("button[aria-label]").all()
    for btn in buttons[:30]:
        label = btn.get_attribute("aria-label")
        print(f"  aria-label: {label!r}")

    print("\n▸ 'Indicators' を含むテキストの要素:")
    indicators_els = page.locator("*:has-text('Indicators')").all()
    for el in indicators_els[:10]:
        tag = el.evaluate("el => el.tagName")
        cls = el.get_attribute("class") or ""
        txt = el.inner_text()[:50] if el.inner_text() else ""
        print(f"  <{tag}> class={cls[:60]!r} text={txt!r}")

    # ③ Indicatorsボタンをクリックして検索
    print("\n▸ Indicatorsボタンを探してクリック...")
    clicked = False
    selectors = [
        "button[aria-label='Indicators, Metrics & Strategies']",
        "button[aria-label='Indicators']",
        "button[data-name='indicators-button']",
        "#header-toolbar-indicators",
        "button:has-text('Indicators')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                print(f"  ✓ ヒット: {sel!r}")
                btn.click()
                clicked = True
                break
        except Exception as e:
            print(f"  ✗ {sel!r}: {e}")

    if not clicked:
        print("  ✗ Indicatorsボタンが見つかりませんでした")
        page.screenshot(path=f"{SCREENSHOT_DIR}/step2_no_button.png")
        browser.close()
        exit(1)

    time.sleep(2)
    page.screenshot(path=f"{SCREENSHOT_DIR}/step2_indicators_open.png")
    print("  ✓ step2_indicators_open.png — ダイアログ確認")

    # ④ 検索ボックスを探す
    print("\n▸ 検索ボックスを探しています...")
    search_selectors = [
        "input[placeholder*='Search']",
        "input[data-name='search-input']",
        "input[placeholder*='search']",
        "input[type='text']",
    ]
    search_found = False
    for sel in search_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                print(f"  ✓ ヒット: {sel!r}")
                el.fill("VWAP")
                search_found = True
                break
        except Exception as e:
            print(f"  ✗ {sel!r}: {e}")

    if not search_found:
        print("  ✗ 検索ボックスが見つかりませんでした")
        page.screenshot(path=f"{SCREENSHOT_DIR}/step3_no_search.png")
        browser.close()
        exit(1)

    time.sleep(1.5)
    page.screenshot(path=f"{SCREENSHOT_DIR}/step3_search_vwap.png")
    print("  ✓ step3_search_vwap.png — 検索結果確認")

    # ⑤ VWAP候補の要素をリストアップ
    print("\n▸ 'VWAP' を含む要素一覧:")
    vwap_els = page.locator("*:has-text('VWAP')").all()
    for el in vwap_els[:15]:
        tag  = el.evaluate("el => el.tagName")
        cls  = el.get_attribute("class") or ""
        attr = el.get_attribute("data-title") or ""
        txt  = ""
        try:
            txt = el.inner_text()[:60]
        except Exception:
            pass
        print(f"  <{tag}> data-title={attr!r} class={cls[:50]!r} text={txt!r}")

    browser.close()

print(f"\n完了。画像は {SCREENSHOT_DIR}/ を確認してください。")
print("この出力内容をClaude に貼り付けてください。")
