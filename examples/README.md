# examples

`clbs-youtube-edit` 用のサンプル入力（テキストのみ）。動画・画像素材は容量の都合で含めていません。

| ファイル | 内容 |
|---|---|
| `script.txt` | 無発音タグ＋`<break time="0.8s" />` 付きの完成台本（編集スキルの入力） |
| `heygen_script.txt` | HeyGen貼り付け用（`[...]`タグを除去し break のみ残した版） |
| `media_list.yaml` | 素材台帳（見出し/スライド/ピクチャー/Bロールの生成プロンプト） |

## 試し方

1. `heygen_script.txt` を HeyGen（AI Studio・16:9）に貼り付けて `video.mp4` を生成
2. `media_list.yaml` を元にスライド/ピクチャー/Bロール素材を生成（Higgsfield等）
3. プロジェクトフォルダに `video.mp4` / `script.txt` / 素材を置く
4. ルートの README.md の手順で 3 スクリプトを実行
