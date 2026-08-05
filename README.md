# BGM Hand Sign Player

Webカメラでハンドサインを認識してBGMを操作するPythonアプリ。

## セットアップ

```bash
pip install -r requirements.txt
```

Windowsでmp3再生時に問題が出る場合は、SDL2周りの依存が必要になることがあります。
その場合は `pip install pygame --upgrade` を試してください。

## 使い方

1. `tracks` フォルダに再生したいmp3ファイルを入れる
   (ファイル名の昇順で再生リストになります)

2. 実行:

```bash
python main.py --dir ./tracks
```

3. カメラに向かって手を映すと、以下のジェスチャーで操作できます。

| ジェスチャー | 動作 |
|---|---|
| ✋ パー(5本開く) | 再生 / 一時停止トグル |
| ✊ グー | 停止(曲の先頭に戻る) |
| 👍 サムズアップ(親指だけ立てて上向き) | 音量アップ |
| 👎 サムズダウン(親指だけ立てて下向き) | 音量ダウン |
| 👉 人差し指だけ伸ばして右に傾ける | 次の曲 |
| 👈 人差し指だけ伸ばして左に傾ける | 前の曲 |

`q` キーでウィンドウを閉じて終了します。

## 音声コマンド (任意)

ジェスチャーに加えて、声でも操作できます。

```bash
pip install SpeechRecognition pyaudio
python main.py --dir ./tracks --voice
```

| 発話例 | 動作 |
|---|---|
| 「再生」「プレイ」 | 再生 |
| 「一時停止」「止めて」「ポーズ」 | 一時停止 |
| 「停止」「ストップ」 | 停止 |
| 「次」「スキップ」 | 次の曲 |
| 「前」「戻って」 | 前の曲 |
| 「音量上げ」「大きく」 | 音量アップ |
| 「音量下げ」「小さく」 | 音量ダウン |

- Google音声認識APIを使うのでネット接続が必要です(無料枠)。
- ジェスチャーと音声は同時に使えます。両方同時にコマンドが来ても、
  内部的には同じキューで順番に処理されるので競合しません。
- `pyaudio` はOS依存のビルドが必要な場合があります。
  - Windows: `pip install pyaudio` で大抵通ります
  - Mac: `brew install portaudio` してから `pip install pyaudio`
  - Linux: `sudo apt install portaudio19-dev` してから `pip install pyaudio`

## オプション

```bash
python main.py --dir ./tracks --camera 0 --cooldown 1.0 --voice --api-port 8787
```

- `--camera`: 複数カメラがある場合のデバイス番号 (デフォルト 0)
- `--cooldown`: 同じジェスチャーを連続で誤爆させないための待ち時間(秒)
- `--voice`: 音声コマンドを有効化
- `--api-port`: 現在再生中の曲情報とキャリブレーション状態を提供するHTTP APIのポート (デフォルト 8787、0で無効化)

## 配信用画面 (monitor1.html / monitor2.html)

`main.py` 実行中は `http://127.0.0.1:8787` でローカルAPIサーバーが立ち上がります。
`display-common.js` / `display-common.css` を共有ロジックとする `monitor1.html`(左)・
`monitor2.html`(右) の2枚のHTMLをそれぞれのモニターでフルスクリーン表示すると、
再生中の曲名が2画面をまたいで1つの巨大な文字として表示されます。

```
python main.py --dir ./tracks
```

を実行した状態で、`monitor1.html` と `monitor2.html` をブラウザ(またはOBSの
Browser Source)で開いてください。`file://` で直接開くと `fetch` がブロックされる
環境があるため、うまく表示されない場合は下記のように簡易HTTPサーバー経由で配信してください。

```bash
python -m http.server 8000
```

→ `http://127.0.0.1:8000/monitor1.html` / `monitor2.html` を開く

- 2枚のモニターの解像度・物理サイズが違っても、後述のキャリブレーションで
  文字サイズと継ぎ目位置がぴったり合うように自動計算されます。
- モニター側のページ自体はキー操作を必要としません(すべて `control.html` から遠隔で調整します)。

### OBSでの配信

- OBSのシーンに **「ブラウザソース」** で `monitor1.html` / `monitor2.html` の
  URLを直接追加するのが最も安定します(ウィンドウキャプチャよりも確実です)。
- カメラのプレビュー用ウィンドウ(`cv2.imshow` の操作者向け映像)はOBSに映す必要は
  ありません。ハンドサイン確認用のローカルプレビューなので、OBSのシーンには
  含めず、配信は `monitor1.html` / `monitor2.html` のブラウザソースだけで構成してください。

## 管理画面 (control.html)

`control.html` は操作者(自分)専用のダッシュボードです。**OBSのシーンには
絶対に追加しないでください** — 現在の曲名・再生状態・音量・プレイリスト全体に加え、
配信用の2画面(`monitor1.html` / `monitor2.html`)の位置調整パネルがあります。

```
http://127.0.0.1:8000/control.html
```

### 配信画面の位置調整

2枚のモニターのサイズ・解像度が違っても文字の大きさと継ぎ目が合うように、
以下の手順でキャリブレーションします。

1. `control.html` を開く
2. 各モニターの実際の高さを定規で測り、「MONITOR 1 / 2」の「画面の高さ(cm)」欄に入力
3. 上下にズレている場合は `▲/▼`(2px刻み)・`▲▲/▼▼`(10px刻み)ボタンで微調整

入力した値は `main.py` のAPIサーバー(`calib_state.json`)に保存され、
`monitor1.html` / `monitor2.html` 側がポーリングで自動的に反映します
(モニター側での操作は不要です。OBSのBrowser Source内は直接操作できないため、
このような「管理画面から遠隔で調整する」方式にしています)。

## ローカルAPI

`main.py` 実行中、以下のエンドポイントが `http://127.0.0.1:8787` で提供されます。

| エンドポイント | メソッド | 内容 |
|---|---|---|
| `/now-playing` | GET | 現在の再生状態 (`track`, `playing`, `volume`, `index`, `total_tracks`, `tracks`) |
| `/calib` | GET | モニター1・2のキャリブレーション状態 (`heightCm`, `yOffsetPx`) |
| `/calib` | POST | キャリブレーション状態の更新。Body例: `{"monitor": "1", "heightCm": 30}` |

## 調整のヒント

- ジェスチャーが認識されにくい/誤爆する場合は `GestureRecognizer` 内のしきい値
  (`0.08`, `0.1` など、指先と手首の座標差の閾値) を調整してください。
- 手が2つカメラに映ると誤爆しやすいので、`max_num_hands=1` にしています。
  複数人での操作を想定する場合は `main.py` 内の値を変更してください。
- 認識精度を上げたい場合は `model_complexity=0` を `1` に変更すると精度は上がりますが
  処理が重くなります(ノートPC等では0推奨)。
- カメラプレビュー内の日本語(トラック名など)は `cv2.putText` ではなくPIL経由の
  `draw_text_ja()` で描画しています。Windows標準の日本語フォント(メイリオ等)が
  見つからない環境では文字化けする場合があるので、`main.py` 内の
  `_JP_FONT_CANDIDATES` にお使いのフォントパスを追加してください。
