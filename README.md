# ZeroClaw + Ollama — TradingView スナップショット (Windows DevContainer)

Windows の VS Code DevContainer 上で **ZeroClaw**（Rust 製 AI エージェント）と
**Ollama**（ローカル LLM）を組み合わせ、約定照会 CSV から銘柄を自動読み取りして
TradingView の 1 分足チャートを一括撮影する環境です。

---

## 🗂 ディレクトリ構成

```
zeroclaw-tradingview/
├── .devcontainer/
│   ├── devcontainer.json        # DevContainer 設定（GPU 構成込み）
│   ├── docker-compose.yml       # Ollama + ZeroClaw コンテナ定義
│   ├── docker-compose.gpu.yml   # NVIDIA GPU オーバーライド
│   ├── Dockerfile               # ZeroClaw + Chromium + Playwright 環境
│   └── post-create.sh           # 初回起動時の自動セットアップ
├── scripts/
│   ├── batch_snapshot.py        # 約定照会CSV → 一括撮影 (メインスクリプト)
│   └── snapshot.py              # 単体撮影スクリプト
├── skills/
│   └── tradingview_snapshot.toml  # ZeroClaw スキル定義
├── snapshots/                   # 撮影画像の保存先 (Windows 側と共有)
└── README.md
```

---

## 🚀 セットアップ手順

### Step 1. 必要ツールのインストール（Windows 側）

| ツール | URL |
|--------|-----|
| Docker Desktop | https://www.docker.com/products/docker-desktop |
| VS Code | https://code.visualstudio.com |
| VS Code 拡張: Dev Containers | VS Code 拡張機能マーケットプレイスで「Dev Containers」を検索 |

> Ollama は Windows 側のインストール不要です。コンテナとして自動起動します。

---

### Step 2. GPU の動作確認（PowerShell）

```powershell
docker run --rm --gpus=all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi
```

GPU 名と VRAM が表示されれば OK です。このプロジェクトは RTX 3060 (6GB) 対応済みです。

---

### Step 3. フォルダの配置

ダウンロードした ZIP を展開し、好きな場所に配置します。

```
C:\Users\あなたの名前\zeroclaw-tradingview\   ← 例
```

---

### Step 4. DevContainer の起動

1. VS Code でメニュー → `ファイル` → `フォルダーを開く` → `zeroclaw-tradingview` を選択
2. VS Code 左下の青い `><` アイコンをクリック
3. `Dev Containers: Reopen in Container` を選択
4. 初回ビルドが始まります（目安: 10〜15 分）

ビルドの流れ:
```
[1/4] Docker イメージのビルド        （Rust + ZeroClaw + Playwright）
[2/4] Ollama コンテナの起動
[3/4] llava + llama3.2 の自動ダウンロード  （初回のみ、10〜30 分）
[4/4] post-create.sh による初期設定
```

> 右下の「show log」をクリックすると進捗をリアルタイム確認できます。
> モデルデータは `ollama-models` ボリュームに永続化されるため、
> 2 回目以降の再ビルドでは再ダウンロード不要です。

---

### Step 5. 動作確認（コンテナ内ターミナル: Ctrl + @）

```bash
# Ollama が起動しているか確認
curl http://ollama:11434/api/tags
# → {"models":[{"name":"llava:latest",...},{"name":"llama3.2:latest",...}]} が返ればOK

# ZeroClaw の確認
zeroclaw --version
zeroclaw status
```

---

## 📸 一括スナップショットの使い方

### 約定照会 CSV の配置

証券会社からダウンロードした約定照会 CSV を `snapshots/` と同じ階層か
`/workspace/` 配下に置きます。

必要な列: `約定日`・`コード`・`銘柄名`

```csv
"約定日","受渡日","コード","銘柄名", ...
"2026/03/06 09:03:40","2026/03/10","5016","ＪＸ金属", ...
"2026/03/06 12:32:51","2026/03/10","6702","富士通", ...
```

同一銘柄・同一日の重複は自動で除去され、1銘柄 1枚だけ撮影します。

---

### 撮影コマンド（コンテナ内ターミナル）

```bash
# CSV 内の全銘柄・全約定日を撮影
python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/20260306_約定照会.csv

# 日付を絞り込む
python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/20260306_約定照会.csv \
  --date 2026-03-06

# ライトテーマ・高解像度
python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/20260306_約定照会.csv \
  --theme light --width 2560 --height 1440

# チャートの読み込み待機を延長する場合（回線が遅い環境）
python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/20260306_約定照会.csv \
  --wait 15000
```

### 保存先の構造

```
snapshots/
└── 20260306/
    ├── TSE_5016_1m_20260306.png   ← ＪＸ金属
    └── TSE_6702_1m_20260306.png   ← 富士通
```

`snapshots/` フォルダは Windows 側からも直接参照できます。

---

## 🔧 オプション一覧

| オプション | デフォルト | 説明 |
|------------|-----------|------|
| `--csv` | （必須） | 約定照会 CSV のパス |
| `--date` | 全日 | 撮影日を絞り込む (YYYY-MM-DD) |
| `--outdir` | `/workspace/snapshots` | 保存先ディレクトリ |
| `--theme` | `dark` | `dark` または `light` |
| `--width` | `1920` | 画像幅 px |
| `--height` | `1080` | 画像高さ px |
| `--wait` | `10000` | チャート読込待機 ms |
| `--interval-sec` | `3` | 銘柄間のインターバル秒 |

---

## 🤖 ZeroClaw エージェントでチャート分析

撮影した画像を `llava`（ビジョン対応モデル）に渡して AI 分析させることができます。

```bash
zeroclaw agent

# インタラクティブセッション内で:
> /workspace/snapshots/20260306/TSE_5016_1m_20260306.png を分析して
> エントリー・エグジットのタイミングとして適切だったか評価して
```

---

## 🛠 トラブルシューティング

### Ollama に接続できない
```bash
docker compose ps ollama          # 状態確認
docker logs ollama -f             # ログ確認
docker compose restart ollama     # 再起動
curl http://ollama:11434/api/tags # 疎通確認
```

### スクリーンショットが真っ黒・真っ白
```bash
# 待機時間を延長する
python3 /workspace/scripts/batch_snapshot.py --csv ... --wait 15000
```

### Chromium が起動しない
```bash
xvfb-run chromium --headless --no-sandbox \
  --screenshot=/tmp/test.png https://example.com
ls -lh /tmp/test.png
```

### ZeroClaw コマンドが見つからない
```bash
export PATH=/usr/local/cargo/bin:$PATH
which zeroclaw
```

### GPU が認識されない
```powershell
# Windows PowerShell で確認
wsl --update
wsl --shutdown
docker run --rm --gpus=all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi
```

---

## 📌 関連リンク

- Ollama: https://ollama.ai
- Docker Desktop: https://www.docker.com/products/docker-desktop
- VS Code Dev Containers: https://code.visualstudio.com/docs/devcontainers/containers
