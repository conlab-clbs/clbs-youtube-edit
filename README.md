# clbs-youtube-edit

顔出しAIアバター動画（HeyGen等）を、**無発音タグ付き台本**から自動編集し、**Premiere Pro XML** と **ほぼ完成形のMP4（テロップ・見出し焼き込み済み）** を一気に書き出す [Claude Code](https://claude.com/claude-code) スキルです。

VSL・ウェビナー・YouTube本編など、顔出し解説動画の編集を自動化します。

---

## 特徴

- **無発音タグ方式** — 台本の `[見出し：◯◯] / [スライドN] / [ピクチャーN] / [BロールN] / [カムリターン]` はアバターに読ませない。Whisper文字起こし × 台本本文の **difflib 照合** でタグ位置を逆算するので、VADのタグ誤検出に依存せず堅牢。
- **沈黙ジェットカット** — HeyGenの `<break time="0.8s" />` で作った文間の沈黙を切って編集点を増やし、「カメラ目線で淀みなく喋り続ける不気味さ」を回避。
- **非対称カット** — 喋り終わりに余韻を残し（中高年でも追える）、喋り出しはキビキビ。`--keep-tail` / `--keep-lead` で調整可能。
- **完成MP4まで自動生成** — スライド＋右上角丸ワイプ・左ピクチャー・全画面Bロール・左上見出しバー・下端デザインテロップを合成し焼き込み。
- **完全同期** — 素材・見出し・テロップの出現/消去がジェットカット境界にスナップされ、常に同一フレームで切り替わる。
- **Premiere XMLも同時出力** — 手仕上げしたい場合はXMEMLを読み込み（V1-V5＋A1/A2のトラック分離）。

## デモ（処理の流れ）

```
script.txt（無発音タグ＋<break>）＋ video.mp4
  │
  ├─ analyze_pro.py        無音ジェットカット ＋ difflib照合 ＋ テロップ ＋ 見出しサプレッション
  ├─ export_premiere_pro.py  Premiere XML（V1-V5 ＋ A1/A2）
  └─ render_pro.py         全合成MP4（テロップ・見出し焼き込み）
        ↓
   final.mp4 ＋ project_premiere.xml ＋ telop_jetcut.srt
```

## 必要環境

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/)（`ffmpeg` がPATHにあること。または環境変数 `FFMPEG_PATH`）
- 日本語の太ゴシックフォント（macOSは標準のヒラギノ／Linuxは `fonts-noto-cjk` を推奨）

```bash
pip install -r requirements.txt
# Linux で日本語フォントが無い場合:
#   sudo apt-get install fonts-noto-cjk
```

## 使い方

プロジェクトフォルダを用意します（最小構成は `video.mp4` と `script.txt`）。

```
プロジェクト/
├── video.mp4              # HeyGenアバター動画（タグは無発音／<break>は沈黙）
├── script.txt            # 無発音タグ＋<break time="0.8s" /> 付き台本
├── スライド/slide_001.png  # 任意（16:9・下20%は無地でテロップ確保）
├── ピクチャー/picture_01.png# 任意（4:3 or 1:1）
└── broll/broll01.mp4      # 任意（セリフより長い尺）
```

実行：

```bash
P="/path/to/プロジェクト"
python3 scripts/analyze_pro.py "$P" --model small   # 1) 解析
python3 scripts/export_premiere_pro.py "$P"         # 2) Premiere XML
python3 scripts/render_pro.py "$P"                  # 3) 完成MP4
```

Claude Code からは「このフォルダのYouTube動画を編集して」と話しかけるだけで一連が実行されます。

## 台本タグ仕様

| タグ | 用途 | 画面 | 発音 |
|---|---|---|---|
| `[見出し：◯◯]` | 左上見出しバー（次の見出しまで継続。スライド/Bロール中は非表示） | アバター/ピクチャー時のみ表示 | しない |
| `[スライドN]` | 重要ノウハウ・図解 | スライド全画面＋右上ワイプ | しない |
| `[ピクチャーN]` | 感情的イメージ・具体物 | アバター＋左ポップアップ | しない |
| `[BロールN]` | シネマティック演出 | Bロール全画面（声のみ継続） | しない |
| `[カムリターン]` | 素材を終了しアバターに戻す | カメラ復帰 | しない |
| `<break time="0.8s" />` | 沈黙ポーズ（ジェットカット対象） | 沈黙 | 沈黙 |

台本は姉妹スキル **clbs-video-script-pro** で執筆できます（`examples/` にサンプルあり）。

## 主なオプション

| 引数 (analyze_pro.py) | 既定 | 説明 |
|---|---|---|
| `--model` | small | Whisperモデル（tiny/base/small/medium…） |
| `--min-silence` | 0.30 | これ以上の沈黙を切る対象に |
| `--keep-tail` | 0.40 | 文末に残す余韻（大きいほど落ち着く） |
| `--keep-lead` | 0.08 | 喋り出し前に残す間（小さいほどキビキビ） |
| `--telop-lead` | 0.12 | テロップ先行表示の秒数 |

| 環境変数 (render_pro.py) | 既定 | 説明 |
|---|---|---|
| `CLBS_PRESET` | veryfast | x264プリセット（確認は ultrafast） |
| `CLBS_CRF` | 21 | 画質（小さいほど高画質。本番20前後） |
| `CLBS_FONT` | 自動解決 | テロップ/見出しフォント |
| `CLBS_WIPE_CROP` | 760:760:910:170 | ワイプ切り抜き `w:h:x:y` |

## トラック構成（Premiere XML）

```
V1 メイン動画（ジェットカット済み）  V2 ピクチャー  V3 スライド
V4 Bロール  V5 ワイプ（右上・角丸）   A1/A2 音声L/R
```

## ライセンス

[MIT](LICENSE)

## 関連

- 台本執筆スキル: clbs-video-script-pro
- 素材生成: [Higgsfield](https://higgsfield.ai)（スライド/ピクチャー=Nano Banana 2、Bロール=Veo3）
