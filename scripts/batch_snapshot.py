#!/usr/bin/env python3
"""
batch_snapshot.py  —  約定照会CSVから銘柄と日付を読み取り、1分足チャートを一括撮影
                      撮影後、約定時刻・売買区分をPillowでチャートに重ねて表示

使い方:
  # CSV内の全約定日・全銘柄を撮影
  python3 batch_snapshot.py --csv /workspace/20260306_約定照会.csv

  # 日付を絞り込む
  python3 batch_snapshot.py --csv /workspace/20260306_約定照会.csv --date 2026-03-06

  # テーマ・解像度を変更
  python3 batch_snapshot.py --csv /workspace/20260306_約定照会.csv --theme light --width 2560 --height 1440

対応CSVフォーマット:
  証券会社の約定照会CSVを想定。以下の列を使用します:
    - 約定日      : 約定日時 (YYYY/MM/DD HH:MM:SS 形式)
    - コード      : 銘柄コード (4桁数字)
    - 銘柄名      : 銘柄名 (ログ表示用)
    - 売買        : 買建 / 売埋 / 売建 / 買埋
    - 約定単価(円): 約定価格
    - 約定数量(株/口): 株数

マーカー凡例:
  ▲ 緑  買建 (ロングエントリー)
  ▽ 赤  売埋 (ロングエグジット)
  ▼ 赤  売建 (ショートエントリー)
  △ 緑  買埋 (ショートエグジット)
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

DEFAULT_OUT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/workspace/snapshots"))
EXCHANGE = "TSE"   # 東証銘柄固定

# TSE通常取引時間 (分単位)
TSE_OPEN  = 9 * 60        # 09:00
TSE_CLOSE = 15 * 60 + 30  # 15:30

# 売買区分 → (color, up, label)
TRADE_STYLE = {
    "買建": {"color": (0, 210, 120), "up": True,  "label": "L.Entry"},   # 緑  ロングエントリー
    "売埋": {"color": (255, 100, 200), "up": False, "label": "L.Exit"},  # ピンク ロングエグジット
    "売建": {"color": (255, 80, 80), "up": False, "label": "S.Entry"},   # 赤  ショートエントリー
    "買埋": {"color": (80, 160, 255), "up": True,  "label": "S.Exit"},   # 青  ショートエグジット
}
# ナンピン追加エントリーの色
NANPIN_COLOR = (255, 180, 0)  # オレンジ


# ══════════════════════════════════════════════
# CSV パース
# ══════════════════════════════════════════════

def load_yakujo_csv(csv_path: Path, filter_date: str | None) -> tuple[list[dict], dict]:
    """
    約定照会CSVを読み込み、
      targets : (date, code, name) のユニーク組み合わせリスト  → 撮影対象
      trades  : {(date, code): [trade_dict, ...]}              → マーカー用
    を返す。
    filter_date: "YYYY-MM-DD" 形式で指定すると該当日のみ抽出 (Noneで全日)
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print(f"[ERROR] CSVにデータがありません: {csv_path}")
        sys.exit(1)

    # 列名の前後スペース・クォートを除去
    rows = [{k.strip().strip('"'): v.strip().strip('"') for k, v in row.items()} for row in rows]

    # 必須列の確認
    required = {"約定日", "コード", "銘柄名"}
    actual = set(rows[0].keys())
    missing = required - actual
    if missing:
        print(f"[ERROR] CSV に必要な列が見つかりません: {missing}")
        print(f"        実際の列名: {list(actual)}")
        sys.exit(1)

    seen   = {}                      # (date_str, code) → name
    trades = defaultdict(list)       # (date_str, code) → [trade_dict, ...]

    for row in rows:
        raw_date = row["約定日"]
        code     = row["コード"]
        name     = row["銘柄名"]

        # コードが4桁数字でない行はスキップ
        if not re.match(r"^\d{4}$", code):
            continue

        # 約定日時パース
        try:
            dt = datetime.strptime(raw_date, "%Y/%m/%d %H:%M:%S")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(raw_date[:10], "%Y/%m/%d")
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError:
                print(f"  [WARN] 日付の解析に失敗: '{raw_date}' — スキップ")
                continue

        # 日付フィルタ
        if filter_date and date_str != filter_date:
            continue

        key = (date_str, code)
        if key not in seen:
            seen[key] = name

        # 売買・価格・数量を取得（列が無い場合は None）
        baibai = row.get("売買", "").strip()
        try:
            price = float(row.get("約定単価(円)", "0").replace(",", ""))
        except ValueError:
            price = 0.0
        try:
            qty = int(row.get("約定数量(株/口)", "0").replace(",", ""))
        except ValueError:
            qty = 0

        if baibai and baibai in TRADE_STYLE:
            trades[key].append({
                "dt":     dt,
                "baibai": baibai,
                "price":  price,
                "qty":    qty,
                "nanpin": False,  # 後でマーク
            })

    if not seen:
        msg = "該当する銘柄がありません"
        if filter_date:
            msg += f" (--date {filter_date})"
        print(f"[ERROR] {msg}")
        sys.exit(1)

    # ナンピン検出: 銘柄ごとにポジションを追跡し、追加エントリーをマーク
    for key, trade_list in trades.items():
        trade_list.sort(key=lambda t: t["dt"])
        pos_qty   = 0
        pos_dir   = None   # "long" or "short"
        group_entries: list[dict] = []

        for tr in trade_list:
            b = tr["baibai"]
            q = tr["qty"]
            p = tr["price"]

            if b in ("買建", "売建"):
                direction = "long" if b == "買建" else "short"
                if pos_qty > 0 and pos_dir == direction:
                    # 同方向で既にポジションあり
                    # 直前エントリーと同秒 → 分割執行なのでナンピンではない
                    last_entry_dt = group_entries[-1]["dt"] if group_entries else None
                    is_same_second = (last_entry_dt and tr["dt"] == last_entry_dt)
                    if not is_same_second:
                        tr["nanpin"] = True
                else:
                    # 新規エントリー
                    pos_dir = direction
                    group_entries = []
                group_entries.append(tr)
                pos_qty += q

            elif b in ("売埋", "買埋"):
                # グループの平均取得単価を全エントリーに付与
                if group_entries:
                    total_cost = sum(e["price"] * e["qty"] for e in group_entries)
                    total_qty  = sum(e["qty"] for e in group_entries)
                    avg_price  = total_cost / total_qty if total_qty else 0
                    group_id   = id(group_entries[0])
                    for e in group_entries:
                        e["group_avg"]   = avg_price
                        e["group_id"]    = group_id
                        e["group_count"] = len(group_entries)
                    tr["group_avg"]   = avg_price
                    tr["group_id"]    = group_id
                    tr["group_count"] = len(group_entries)
                pos_qty = max(0, pos_qty - q)
                if pos_qty == 0:
                    pos_dir = None
                    group_entries = []

    # date → code 順でソート
    targets = [
        {"date": k[0], "code": k[1], "name": v}
        for k, v in sorted(seen.items())
    ]
    return targets, dict(trades)


# ══════════════════════════════════════════════
# URL 生成
# ══════════════════════════════════════════════

def day_range_unix(date_str: str, tz_name: str = "Asia/Tokyo") -> tuple[int, int]:
    """YYYY-MM-DD をタイムゾーン付き Unix タイムスタンプ (開始/終了) に変換"""
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    dt_start = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=tz
    )
    dt_end = dt_start + timedelta(days=1) - timedelta(seconds=1)
    return int(dt_start.timestamp()), int(dt_end.timestamp())


def build_url(code: str, ts_from: int, ts_to: int, theme: str) -> str:
    """TradingView チャートURL (TSE銘柄・1分足・指定日固定)"""
    return (
        "https://www.tradingview.com/chart/"
        f"?symbol={EXCHANGE}%3A{code}"
        f"&interval=1"
        f"&from={ts_from}"
        f"&to={ts_to}"
        f"&theme={theme}"
        f"&style=1"
        f"&timezone=Asia%2FTokyo"
        f"&hide_side_toolbar=0"
    )


# ══════════════════════════════════════════════
# Pillow マーカー合成
# ══════════════════════════════════════════════

def time_to_x(dt: datetime, chart_left: int, chart_right: int) -> int:
    """
    約定時刻 → チャート上のX座標 (ピクセル)
    TSE通常取引時間 09:00〜15:30 を chart_left〜chart_right にマッピング
    """
    total_minutes = TSE_CLOSE - TSE_OPEN   # 390分
    t_minutes = dt.hour * 60 + dt.minute + dt.second / 60 - TSE_OPEN
    t_minutes = max(0, min(total_minutes, t_minutes))
    ratio = t_minutes / total_minutes
    return int(chart_left + ratio * (chart_right - chart_left))


def draw_triangle(draw: ImageDraw.Draw, cx: int, cy: int, size: int,
                  up: bool, fill: tuple, outline: tuple) -> None:
    """上向き▲ or 下向き▽ を描画"""
    if up:
        pts = [(cx, cy - size), (cx - size, cy + size), (cx + size, cy + size)]
    else:
        pts = [(cx, cy + size), (cx - size, cy - size), (cx + size, cy - size)]
    draw.polygon(pts, fill=fill, outline=outline)


def _triangle_pts(cx: int, cy: int, size: int, up: bool) -> list[tuple]:
    if up:
        return [(cx, cy - size), (cx - size, cy + size), (cx + size, cy + size)]
    else:
        return [(cx, cy + size), (cx - size, cy - size), (cx + size, cy - size)]


def _draw_legend(draw: ImageDraw.Draw, width: int, height: int,
                 font, theme: str) -> None:
    items = [
        ("▲ L.Entry (Long Entry)",    (0, 210, 120)),  # 緑
        ("▽ L.Exit  (Long Exit)",     (80, 160, 255)),  # 青
        ("▼ S.Entry (Short Entry)",   (255, 80, 80)),  # 赤
        ("△ S.Exit  (Short Exit)",    (255, 100, 200)),  # ピンク
        ("▲ Nanpin  (Add to pos)",    (255, 180,   0)),  # オレンジ
    ]
    lw, lh = 220, len(items) * 18 + 12
    lx = width  - lw - 10
    ly = height - lh - 10
    draw.rectangle([lx, ly, lx+lw, ly+lh], fill=(0, 0, 0, 180))
    for i, (text, color) in enumerate(items):
        draw.text((lx+8, ly+6+i*18), text, font=font, fill=(*color, 230))


def overlay_trades(
    image_path: Path,
    trade_list: list[dict],
    width: int,
    height: int,
    theme: str,
) -> None:
    """
    スクリーンショット画像に約定マーカーを合成して上書き保存。

    TradingView 1920x1080 ダークテーマの推定チャート領域:
      X: 左端ツールバー(38px) + 若干の余白 → chart_left ≈ 68
      X: 右端 価格軸(65px) + 右ツールバー(46px) → chart_right ≈ 1809
      Y: ヘッダー(38px) → chart_top ≈ 42
      Y: 時間軸(38px) + ウォーターマーク余白 → chart_bottom ≈ 1020
    解像度が異なる場合は比率でスケーリング。
    """
    if not PILLOW_AVAILABLE:
        print("    [WARN] Pillow 未インストール — マーカー合成をスキップ")
        print("           pip3 install Pillow")
        return
    if not trade_list:
        return

    BASE_W, BASE_H = 1920, 1080
    BASE_LEFT,  BASE_RIGHT  = 68,  1809
    BASE_TOP,   BASE_BOTTOM = 42,  1020

    # 解像度スケーリング
    sx = width  / BASE_W
    sy = height / BASE_H
    chart_left   = int(BASE_LEFT   * sx)
    chart_right  = int(BASE_RIGHT  * sx)
    chart_top    = int(BASE_TOP    * sy)
    chart_bottom = int(BASE_BOTTOM * sy)
    chart_height = chart_bottom - chart_top

    img  = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # フォント
    try:
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        font_xs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except Exception:
        font_sm = ImageFont.load_default()
        font_xs = font_sm

    marker_size = max(7, int(10 * sx))

    # 同一X座標に重なるマーカーを縦方向にずらすため X別インデックス管理
    x_counts: dict[int, int] = defaultdict(int)

    # トレードを時間順にソートして描画位置を計算
    sorted_trades = sorted(trade_list, key=lambda t: t["dt"])
    group_xs = {}  # group_id -> [x,...] (ナンピン用)
    for trade in sorted_trades:
        # X座標計算
        x = time_to_x(trade["dt"], chart_left, chart_right)

        # 同一Xに複数ある場合は縦にずらすインデックスを取得
        n = x_counts[x]
        x_counts[x] += 1

        # トレード属性
        baibai = trade.get("baibai", "")
        style = TRADE_STYLE.get(baibai, None)
        is_nanpin = trade.get("nanpin", False)
        group_avg = trade.get("group_avg", None)
        group_id = trade.get("group_id", None)
        price = trade.get("price", 0.0)
        qty = trade.get("qty", 0)
        label = style["label"] if style else ("Nanpin" if is_nanpin else "")
        color = style["color"] if style else NANPIN_COLOR
        is_up = style["up"] if style else True

        # Y座標
        if is_up:
            cy = chart_bottom - marker_size * 2 - n * (marker_size * 3)
            cy = max(chart_top + marker_size * 2, cy)
        else:
            cy = chart_top + marker_size * 2 + n * (marker_size * 3)
            cy = min(chart_bottom - marker_size * 2, cy)

        # 垂直ガイドライン (半透明)
        draw.line(
            [(x, chart_top), (x, chart_bottom)],
            fill=(*color, 50),
            width=1,
        )

        # マーカー三角形
        draw.polygon(
            _triangle_pts(x, cy, marker_size, is_up),
            fill=(*color, 220),
            outline=(255, 255, 255, 180),
        )

        # ラベルテキスト
        price_str = f"{price:,.1f}" if price else ""
        qty_str   = f" {qty:,}st"  if qty   else ""
        text      = f"{label} {price_str}{qty_str}"
        time_str  = trade["dt"].strftime("%H:%M:%S")

        tx = x + marker_size + 3
        ty = cy - 10
        try:
            bbox = draw.textbbox((tx, ty), text, font=font_sm)
        except Exception:
            bbox = (tx, ty, tx + 100, ty + 14)
        pad = 2
        draw.rectangle(
            [bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad],
            fill=(0, 0, 0, 160),
        )
        draw.text((tx, ty), text, font=font_sm, fill=(*color, 255))
        draw.text((tx, ty + 15), time_str, font=font_xs, fill=(200, 200, 200, 220))

        # ナンピングループの平均取得単価を初回エントリーのラベルに追加
        if (not is_nanpin and baibai in ("買建", "売建")
                and group_avg and trade.get("group_count", 1) > 1):
            avg_text = f"Avg:{group_avg:,.1f}"
            draw.text((tx, ty + 28), avg_text, font=font_xs,
                      fill=(255, 200, 50, 230))

        # グループX座標を記録（ナンピン括弧描画用）
        if group_id:
            group_xs.setdefault(group_id, []).append(x)

    # ナンピングループ括弧を描画
    # グループ内エントリーを横線＋縦ティックで結び、Avgを上部に表示
    drawn_groups = set()
    for trade in sorted_trades:
        gid = trade.get("group_id")
        avg = trade.get("group_avg")
        cnt = trade.get("group_count", 1)
        if gid and avg and cnt > 1 and gid not in drawn_groups:
            drawn_groups.add(gid)
            xs = group_xs.get(gid, [])
            if len(xs) < 2:
                continue
            xs_sorted = sorted(xs)
            bx1, bx2  = xs_sorted[0], xs_sorted[-1]
            by        = chart_bottom - marker_size * 6  # 括弧のY位置
            bc        = NANPIN_COLOR
            tick_h    = 6

            # 横線
            draw.line([(bx1, by), (bx2, by)], fill=(*bc, 200), width=2)
            # 両端ティック
            draw.line([(bx1, by - tick_h), (bx1, by + tick_h)], fill=(*bc, 200), width=2)
            draw.line([(bx2, by - tick_h), (bx2, by + tick_h)], fill=(*bc, 200), width=2)
            # 中間ティック (ナンピン位置)
            for mx in xs_sorted[1:-1]:
                draw.line([(mx, by - tick_h//2), (mx, by + tick_h//2)], fill=(*bc, 180), width=1)

            # Avgラベル
            avg_text = f"Avg:{avg:,.1f}"
            mid_x = (bx1 + bx2) // 2
            try:
                bbox = draw.textbbox((mid_x, by - 20), avg_text, font=font_sm)
            except Exception:
                bbox = (mid_x - 30, by - 22, mid_x + 30, by - 6)
            draw.rectangle(
                [bbox[0]-3, bbox[1]-2, bbox[2]+3, bbox[3]+2],
                fill=(40, 30, 0, 190),
            )
            # anchor="mm" may not be supported on all Pillow versions; use centered draw if available
            try:
                draw.text((mid_x, by - 20), avg_text, font=font_sm,
                          fill=(*bc, 255), anchor="mm")
            except Exception:
                draw.text((mid_x - (bbox[2]-bbox[0])//2, by - 20), avg_text, font=font_sm,
                          fill=(*bc, 255))

    # 凡例パネル (右下)
    _draw_legend(draw, width, height, font_sm, theme)

    # 合成
    composited = Image.alpha_composite(img, overlay).convert("RGB")
    composited.save(str(image_path), format="PNG")


# ══════════════════════════════════════════════
# スクリーンショット撮影
# ══════════════════════════════════════════════

def capture_chart(browser, url: str, output_path: Path, width: int, height: int, wait_ms: int) -> bool:
    """
    Playwright を使ってチャートページを開きスクリーンショットを保存する。
    成功なら True、失敗なら False を返す。
    """
    try:
        page = browser.new_page(viewport={"width": width, "height": height})
        # 一部のサイトはヘッダーやポップアップが出るため、十分に待つ
        page.goto(url, timeout=wait_ms)
        # TradingView は描画に時間がかかることがあるので少し待機
        time.sleep(min(5, wait_ms / 1000.0))
        # 追加の待機で安定させる（要調整）
        page.wait_for_timeout(1000)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=False)
        page.close()
        return True
    except Exception as e:
        try:
            page.close()
        except Exception:
            pass
        print(f"    [ERROR] スクリーンショット取得失敗: {e}")
        return False


# ══════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="約定照会CSVから銘柄と日付を読み取り、1分足チャートを一括撮影"
    )
    parser.add_argument(
        "--csv", required=True,
        help="約定照会CSVのパス  例: /workspace/20260306_約定照会.csv"
    )
    parser.add_argument(
        "--date", default=None,
        help="撮影日を絞り込む (YYYY-MM-DD)  省略で全約定日を撮影"
    )
    parser.add_argument(
        "--outdir", default=str(DEFAULT_OUT_DIR),
        help=f"保存先ディレクトリ (デフォルト: {DEFAULT_OUT_DIR})"
    )
    parser.add_argument(
        "--theme", default="dark", choices=["dark", "light"],
        help="チャートテーマ (デフォルト: dark)"
    )
    parser.add_argument(
        "--width",  type=int, default=1920, help="画像幅 px (デフォルト: 1920)"
    )
    parser.add_argument(
        "--height", type=int, default=1080, help="画像高さ px (デフォルト: 1080)"
    )
    parser.add_argument(
        "--wait", type=int, default=10000,
        help="チャート読込待機時間 ms (デフォルト: 10000)"
    )
    parser.add_argument(
        "--interval-sec", type=int, default=3,
        help="銘柄間のインターバル秒 (デフォルト: 3)"
    )
    parser.add_argument(
        "--no-markers", action="store_true",
        help="約定マーカーの合成をスキップ"
    )

    args = parser.parse_args()

    # CSV読み込み・銘柄抽出
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[ERROR] CSVファイルが見つかりません: {csv_path}")
        sys.exit(1)

    targets, trades = load_yakujo_csv(csv_path, args.date)

    # サマリー表示
    dates = sorted(set(t["date"] for t in targets))
    print("=" * 60)
    print(f"  約定照会 → 一括スナップショット撮影")
    print(f"  CSVファイル  : {csv_path.name}")
    print(f"  対象日       : {', '.join(dates)}")
    print(f"  撮影対象     : {len(targets)} 件（重複除去済み）")
    for t in targets:
        key = (t["date"], t["code"])
        n_trades = len(trades.get(key, []))
        print(f"    [{t['date']}] {t['code']} {t['name']}  ({n_trades}約定)")
    print(f"  インターバル : 1分足 (1日分)")
    print(f"  テーマ       : {args.theme}")
    print(f"  マーカー合成 : {'OFF' if args.no_markers else 'ON'}")
    print(f"  保存先       : {args.outdir}")
    print("=" * 60)
    print()

    if not PLAYWRIGHT_AVAILABLE:
        print("[ERROR] playwright がインストールされていません")
        print("        pip3 install playwright && playwright install chromium")
        sys.exit(1)

    results = {"ok": [], "fail": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                f"--window-size={args.width},{args.height}",
            ],
        )

        for i, t in enumerate(targets, 1):
            date_str = t["date"]
            code     = t["code"]
            name     = t["name"]
            dt_label = date_str.replace("-", "")
            key      = (date_str, code)

            trade_list = trades.get(key, [])
            print(f"[{i:>2}/{len(targets)}] {code} {name}  ({date_str})  約定{len(trade_list)}件")

            ts_from, ts_to = day_range_unix(date_str)
            url = build_url(code, ts_from, ts_to, args.theme)

            out_dir     = Path(args.outdir) / dt_label
            output_path = out_dir / f"{EXCHANGE}_{code}_1m_{dt_label}.png"

            success = capture_chart(browser, url, output_path, args.width, args.height, args.wait)

            if success and output_path.exists():
                # マーカー合成
                if not args.no_markers and trade_list:
                    print(f"    ▸ マーカー合成中... ({len(trade_list)}件)")
                    try:
                        overlay_trades(
                            output_path, trade_list,
                            width=args.width, height=args.height,
                            theme=args.theme,
                        )
                        for tr in trade_list:
                            print(f"      {tr['dt'].strftime('%H:%M:%S')}  {tr['baibai']:4}  "
                                  f"{tr['price']:>8,.1f}円  {tr['qty']:>5,}株")
                    except Exception as e:
                        print(f"    [WARN] マーカー合成エラー: {e}")

                try:
                    size_kb_after = output_path.stat().st_size // 1024
                except Exception:
                    size_kb_after = 0
                print(f"    ✓ 保存: {output_path.name}  ({size_kb_after} KB)")
                results["ok"].append(f"{code} {name}")
            else:
                print(f"    ✗ 失敗: {code} {name}")
                results["fail"].append(f"{code} {name}")

            if i < len(targets):
                time.sleep(args.interval_sec)

        browser.close()

    # 結果サマリー
    print()
    print("=" * 60)
    print(f"  完了: {len(results['ok'])} 件成功 / {len(results['fail'])} 件失敗")
    if results["fail"]:
        print(f"  失敗: {', '.join(results['fail'])}")
    print(f"  保存先: {args.outdir}")
    print("=" * 60)

    if results["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
