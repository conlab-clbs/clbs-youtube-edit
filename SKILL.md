---
name: clbs-youtube-edit
description: 顔出しHeyGenアバター動画（VSL・ウェビナー・YouTube本編）を、無発音タグ付き台本から自動編集して「Premiere Pro XML」と「ほぼ完成形のMP4（テロップ・見出し焼き込み済み）」を一気に書き出すスキル。文間の沈黙を非対称ジェットカット（喋り終わりに余韻を残す）し、Whisper文字起こし×台本のdifflib照合で無発音タグ（見出し/スライド/ピクチャー/Bロール/カムリターン）の位置を逆算、スライド＋右上角丸ワイプ・左ピクチャー・全画面Bロール・左上見出しバー・下端デザインテロップを自動合成する。「YouTube動画編集」「顔出し動画を編集」「アバター動画編集」「HeyGen動画編集」「ジェットカット」「完成MP4まで書き出し」「テロップ焼き込み」「見出しバー」「Premiere XML」などで使用。台本作成は clbs-video-script-pro、英語版は clbs-video-edit-en を使う。
---

# clbs-youtube-edit

顔出しアバター動画（HeyGen等）＋無発音タグ付き台本から、**ジェットカット → 素材自動配置 → テロップ/見出し焼き込み**まで実行し、**Premiere Pro XML** と **ほぼ完成形の final.mp4** を同時に書き出す。

## 設計思想（既存 clbs-video-edit / slidevideo-edit との違い）

- **タグは無発音**：台本の `[見出し：◯◯] / [スライドN] / [ピクチャーN] / [BロールN] / [カムリターン]` はアバターに読み上げさせない。位置決めは VAD のタグ検出ではなく **Whisper文字起こし × 台本本文の difflib 照合**で逆算する（堅牢）。
- **沈黙ジェットカット**：HeyGenの `<break time="0.8s" />` で作った文間の沈黙を切って編集点を増やし、「カメラ目線で淀みなく喋り続ける不気味さ」を回避する。
- **非対称カット**：喋り終わりに余韻を残し（中高年向け）、喋り出しはキビキビ。
- **完成MP4まで出す**：従来スキルが手仕上げ前提のXMLだけだったのに対し、テロップ・見出しを焼き込んだ完成形MP4も書き出す。

## 入力フォルダ

```
プロジェクト/
├── video.mp4              # 必須。HeyGenアバター動画（無発音タグ／breakは沈黙）
├── script.txt            # 必須。無発音タグ＋<break time="0.8s" /> 付き台本
├── スライド/slide_001.png  # 任意（[スライドN]対応・16:9・下20%は無地でテロップ確保）
├── ピクチャー/picture_01.png# 任意（[ピクチャーN]対応・4:3 or 1:1）
└── broll/broll01.mp4      # 任意（[BロールN]対応・セリフより長い尺）
```

台本タグ仕様・素材生成は `clbs-video-script-pro`（台本執筆スキル）と1対1で対応する。

## 実行手順（3スクリプト）

```bash
P="/path/to/プロジェクト"
# 1) 解析：沈黙ジェットカット＋difflib照合＋テロップ＋見出しサプレッション
python3 scripts/analyze_pro.py "$P" --model small
# 2) Premiere XML（手仕上げ用）
python3 scripts/export_premiere_pro.py "$P"
# 3) 完成MP4（テロップ・見出し焼き込み）
python3 scripts/render_pro.py "$P"
```

出力：`final.mp4`（完成形）／`project_premiere.xml`／`telop_jetcut.srt`／`timing_plan.json` ほか。

### 主なパラメータ（analyze_pro.py）

| 引数 | 既定 | 説明 |
|---|---|---|
| `--model` | small | Whisperモデル |
| `--min-silence` | 0.30 | これ以上の沈黙を切る対象に |
| `--keep-tail` | 0.40 | 文末に残す余韻（大きいほど落ち着く／中高年向け） |
| `--keep-lead` | 0.08 | 次の喋り出し前に残す間（小さいほどキビキビ） |
| `--telop-lead` | 0.12 | テロップを音声より先行表示する秒数 |

### レンダ環境変数（render_pro.py）

- `CLBS_PRESET`（既定 veryfast）/ `CLBS_CRF`（既定 21）：本番は `veryfast` / `20`、確認は `ultrafast` / `24`
- `CLBS_FONT`：テロップ・見出しのフォント（既定は環境自動解決：macOSヒラギノ角ゴW6→Linux Noto Bold→Windows游ゴB）
- `CLBS_WIPE_CROP="w:h:x:y"`：ワイプ切り抜き（既定 `760:760:910:170`＝顔＋肩・中央）

## レイアウト契約（不変）

| トラック/レイヤー | 内容 |
|---|---|
| V1 | メイン動画（ジェットカット済みアバター） |
| V2 | ピクチャー（左寄せ・高さ608） |
| V3 | スライド（全画面・下20%テロップ余白） |
| V4 | Bロール（全画面・尺不足はループ） |
| V5 | ワイプ（スライド中のみ・右上・252×252角丸・顔センター） |
| A1/A2 | 音声 左ch/右ch（ステレオ） |
| 焼き込み | 見出しバー（左上・ゴールド枠／スライド・Bロール中は非表示）＋テロップ（下端・90px・紺#233B6C縁取り） |

**同期保証**：素材・見出し・テロップの開始は、対応テロップの開始（＝ジェットカット・セグメント境界）にスナップされ、常に同一フレームで切り替わる。素材の消去もセグメント末端＝テロップ終端に一致。

## 必要環境

- Python 3 / ffmpeg
- pip: `openai-whisper torch torchaudio python-Levenshtein budoux Pillow PyMuPDF pyyaml`
- 日本語太ゴシックフォント（macOSは標準、Linuxは `fonts-noto-cjk`）

## 関連スキル

- 台本執筆（上流）：`clbs-video-script-pro`（無発音タグ＋break＋見出しタグ＋media_list.yaml）
- 素材生成：Higgsfield（スライド/ピクチャー=Nano Banana 2、Bロール=Veo3）
- 英語版：`clbs-video-edit-en`
