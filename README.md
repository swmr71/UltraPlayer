# BGM Hand Sign Player

Webカメラでハンドサインを認識してBGMを操作するPythonアプリ。

よく使うページへのリンクは [links.html](links.html) にまとめてあります
(`http://127.0.0.1:8000/links.html` などで開いてください)。

## セットアップ

```bash
pip install -r requirements.txt
```

Windowsでmp3再生時に問題が出る場合は、SDL2周りの依存が必要になることがあります。
その場合は `pip install pygame --upgrade` を試してください。

## 使い方

1. `tracks` フォルダに再生したいmp3ファイルを入れる
   (ファイル名の昇順で再生リストになります)。
   [bgm-library](#bgm-library-曲のアップロードid管理) アプリでアップロードした曲は
   `tracks/tracks.json` に曲名・表示用曲名(伏字対応)・作者などのメタデータ付きで
   自動的に登録されます(こちらが優先して読み込まれます)。

2. ハンドサイン認識(カメラ)はデフォルトではオフです。`control.html` / 音声 / API
   だけで操作する場合はそのまま実行:

```bash
python main.py --dir ./tracks
```

   カメラでハンドサイン操作もしたい場合は `--hand-sign` を付けて実行:

```bash
python main.py --dir ./tracks --hand-sign
```

3. `--hand-sign` 指定時、カメラに向かって手を映すと以下のジェスチャーで操作できます。

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

ジェスチャーに加えて、声でも操作できます。[Vosk](https://alphacephei.com/vosk/) による
オフライン音声認識を使っているため、ネット接続は不要です。

```bash
pip install vosk pyaudio
python main.py --dir ./tracks --voice
```

日本語モデル `vosk-model-small-ja-0.22` を [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)
からダウンロードし、`models/vosk-model-small-ja-0.22` に配置してください。

| 発話例 | 動作 |
|---|---|
| 「再生」「プレイ」 | 再生 |
| 「一時停止」「止めて」「ポーズ」 | 一時停止 |
| 「停止」「ストップ」 | 停止 |
| 「次」「スキップ」 | 次の曲 |
| 「前」「戻って」 | 前の曲 |
| 「音量上げ」「大きく」 | 音量アップ |
| 「音量下げ」「小さく」 | 音量ダウン |

- 認識対象は上記コマンドの単語だけに絞った語彙制約(グラマー)をかけているため、
  関係ない言葉に惑わされにくくなっています。
- ジェスチャーと音声は同時に使えます。両方同時にコマンドが来ても、
  内部的には同じキューで順番に処理されるので競合しません。
- `pyaudio` はOS依存のビルドが必要な場合があります。
  - Windows: `pip install pyaudio` で大抵通ります
  - Mac: `brew install portaudio` してから `pip install pyaudio`
  - Linux: `sudo apt install portaudio19-dev` してから `pip install pyaudio`

## オプション

```bash
python main.py --dir ./tracks --hand-sign --camera 0 --cooldown 1.0 --voice --api-port 8787 --program program.json
```

- `--hand-sign`: カメラでハンドサイン認識を有効化 (デフォルトはオフ)
- `--camera`: 複数カメラがある場合のデバイス番号 (デフォルト 0、`--hand-sign` 指定時のみ使用)
- `--cooldown`: 同じジェスチャーを連続で誤爆させないための待ち時間(秒)
- `--voice`: 音声コマンドを有効化
- `--api-port`: 現在再生中の曲情報とキャリブレーション状態を提供するHTTP APIのポート (デフォルト 8787、0で無効化)
- `--program`: 行事の次第(演目リスト)を定義したJSONファイル。指定すると `N` キーで
  次の項目に進行できる (下記「行事の次第と連動させる」参照)

## 行事の次第と連動させる (発表会・学芸会向け)

劇や演奏など、BGMを流さずに進行する項目が混ざる行事向けの機能です。

```bash
python main.py --dir ./tracks --program program.json
```

`N` キーを押すたびに次の項目へ進みます。項目にBGM(プレイリスト)が設定されて
いれば「転換中」としてそのBGMを順番に再生し(最後まで流れたら先頭に戻って
ループ)、もう一度 `N` を押すとBGMが止まり「上演中」に切り替わります。BGMが
無い項目はそのまま即座に「上演中」になります。

`--program` 指定時は `/now-playing` の内容も変わり、上演中は項目名のみ、
転換中は次の項目名と再生中BGMを返します(詳しくは下記API表を参照)。

`program.json` は手で書かず、[bgm-library](#bgm-library-曲のアップロードid管理)
アプリのUIから発表項目にライブラリの曲を割り当てて保存してください
(サンプル: [program.example.json](program.example.json))。

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

## bgm-library (曲のアップロード・ID管理)

BGMファイルをアップロードするとUUIDをファイル名にして保存し、曲名とは別に
「表示用曲名」(タイトルの一部を伏字にできる。例: `YAJU&U` → `■■■■&U`)・
作者・「当方でBGM化(編集・二次利用)した音源」の注記を管理できるNode.jsアプリです。
[行事の次第](#行事の次第と連動させる-発表会・学芸会向け)への曲の割り当ても
ここから行います。

```bash
cd bgm-library
npm install
cp .env.example .env   # 必要に応じて編集 (後述)
npm start
```

`http://localhost:4000` にアクセスすると管理画面が開きます。

- 曲を追加: 音源ファイル + 曲名を入力してアップロード。`../tracks/` にUUIDファイル名
  で保存され、`../tracks/tracks.json` にメタデータが記録されます。
- 作者検索補助: [last.fm](https://www.lastfm.jp/api/account/create) の無料APIキーを
  `.env` の `LASTFM_API_KEY` に設定すると、曲名から作者候補を検索できます
  (魔王魂などのフリーBGM素材は商用配信されていないため基本的にヒットしません。
  市販曲を使う場合の検索補助として使えます)。
- YouTubeからダウンロード: URLを入力して「情報取得」でタイトル・投稿者を自動入力し、
  「ダウンロードしてライブラリに追加」で音声を抽出してそのまま登録できます。
  [yt-dlp](https://github.com/yt-dlp/yt-dlp) と [ffmpeg](https://ffmpeg.org/) が
  別途必要です(`pip install yt-dlp` + ffmpegをPATHに通す)。ダウンロードした音源の
  著作権・利用規約の確認は利用者側の責任で行ってください。
- ボーカル除去(AI): 登録済みの曲の「ボーカル除去」ボタンを押すと、
  [Demucs](https://github.com/facebookresearch/demucs) でインストゥルメンタル版を
  生成し、`(Instrumental)` 付きの新しい曲としてライブラリに追加します(元の曲は
  そのまま残ります)。`pip install demucs`(main.py と同じvenv)が必要で、CPU実行の
  場合は曲の長さと同程度〜数倍の処理時間がかかります。
- 行事の次第: 発表項目を追加し、各項目にライブラリの曲をプレイリストとして
  複数割り当てられます(`../program.json` に保存され、`main.py --program` が
  読み込みます)。

`.env` の `TRACKS_DIR` / `PROGRAM_FILE` は、`main.py` の `--dir` / `--program` と
同じ場所を指すようにしてください(デフォルトは `../tracks` / `../program.json`)。

## ローカルAPI

`main.py` 実行中、以下のエンドポイントが `http://127.0.0.1:8787` で提供されます。

| エンドポイント | メソッド | 内容 |
|---|---|---|
| `/now-playing` | GET | `--program` 未指定時: 現在の再生状態 (`track`, `playing`, `volume`, `index`, `total_tracks`)。`--program` 指定時: 上演中なら `{"mode": "performing", "current_item": "..."}`、転換中なら `{"mode": "transition", "next_item": "...", "bgm": {"title": ..., "author": ..., "arranged": ...}}` |
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
