# UltraPlayer 使い方ガイド

行事(発表会・学芸会など)の転換BGM再生 + 曲名テロップ表示を、ハンドサイン・音声・
Web管理画面から操作できるシステム。このファイルは全機能を網羅した使い方リファレンス。
セットアップの概要は [README.md](README.md) も参照。

## 目次

- [全体構成](#全体構成)
- [起動手順](#起動手順)
- [main.py のオプション一覧](#mainpy-のオプション一覧)
- [BGMライブラリ管理 (bgm-library)](#bgmライブラリ管理-bgm-library)
- [転換用プレイリスト](#転換用プレイリスト)
- [行事の次第 (プログラム進行)](#行事の次第-プログラム進行)
- [管理画面 (control.html)](#管理画面-controlhtml)
- [配信用画面 (monitor1.html / monitor2.html / display.html)](#配信用画面-monitor1html--monitor2html--displayhtml)
- [ハンドサイン操作](#ハンドサイン操作-hand-sign-指定時)
- [音声コマンド](#音声コマンド---voice-指定時)
- [links.html (リンク一覧)](#linkshtml-リンク一覧)
- [main.py ローカルAPI リファレンス](#mainpy-ローカルapi-リファレンス)
- [bgm-library API リファレンス](#bgm-library-api-リファレンス)
- [トラブルシューティング](#トラブルシューティング)

---

## 全体構成

| コンポーネント | 役割 | 既定ポート |
|---|---|---|
| `main.py` | BGM再生本体。ハンドサイン/音声/API経由の操作を受け付け、状態をHTTP APIで公開。`control.html`・`monitor1/2.html`・`display.html`・`links.html` もこの中から配信する | `8787` |
| `bgm-library/` (Node.js) | 曲のアップロード・YouTubeダウンロード・AIボーカル除去・行事の次第の編集 | `4000` |

`bgm-library` は `main.py` 起動時に子プロセスとして自動的に一緒に起動される
(`node` と `node_modules` が揃っている場合。`--no-library` で無効化可)。
自動起動時、`main.py` の `--dir` / `--program` の値が環境変数 `TRACKS_DIR` /
`PROGRAM_FILE` として bgm-library に渡され、`bgm-library/.env` の値より優先
される。つまり `--dir` / `--program` にリポジトリ外のフォルダを指定しても、
`bgm-library/.env` を手動で書き換える必要はない(`.env` の値は `bgm-library`
を単体起動 (`npm start`) したときだけ使われる)。

`main.py` は `0.0.0.0` で待ち受けているため、同じLAN(ルーター配下)の他端末からも
`http://(このPCのIP):8787/...` でアクセスできる(認証機構は無いので、信頼できる
ネットワークでのみ使うこと)。`bgm-library` も既定でLAN内から到達可能。

```
[bgm-library:4000]  --編集--> tracks/tracks.json, program.json
        ^                              |
        | (アップロード/DL/除去)         v
     (ブラウザ)                  [main.py:8787] --配信--> control.html / monitor1-2.html / display.html / links.html
                                      ^
                                      | ハンドサイン(カメラ) / 音声(マイク) / control.htmlのボタン
                                   操作者
```

## 起動手順

1. **bgm-library の初回セットアップ** (曲を登録・編集する場合。1回だけでOK)
   ```bash
   cd bgm-library
   npm install
   cp .env.example .env   # 必要に応じて編集
   ```

2. **main.py** (プロジェクトルートで)
   ```bash
   python main.py --dir ./tracks
   ```
   デフォルトはハンドサインOFF・カメラ未使用のヘッドレス動作。`control.html`・音声・
   APIだけで操作する場合はこれで十分。起動すると `http://127.0.0.1:8787/links.html`
   から `control.html` 等の各ページも開けるようになる(別途HTTPサーバーを立てる
   必要はない)。`file://` で直接開くと `fetch` がブロックされるため、必ず
   `http://127.0.0.1:8787/...` の形式で開くこと。

   `node` と `bgm-library/node_modules` が揃っていれば、**bgm-libraryもこの時に
   自動で一緒に起動する**(`http://127.0.0.1:4000`)。自動起動したくない場合は
   `--no-library` を付ける。bgm-library だけ単体で起動したい場合は今まで通り
   `cd bgm-library && npm start` でも動く。

`Ctrl+C` で `main.py` を終了すると、自動起動したbgm-libraryも一緒に終了する
(`--hand-sign` 有効時はカメラウィンドウで `q` キーでも終了可)。

## main.py のオプション一覧

```bash
python main.py --dir ./tracks --hand-sign --camera 0 --cooldown 1.0 --voice --api-port 8787 --program program.json
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--dir` | `./tracks` | 音源フォルダ。`tracks.json` があればそちらを優先して読み込む |
| `--hand-sign` | オフ | カメラでハンドサイン認識を有効化する。指定しないとカメラは一切開かない |
| `--camera` | `0` | カメラデバイス番号 (`--hand-sign` 指定時のみ使用) |
| `--cooldown` | `1.0` | 同一ジェスチャーの連続発火防止秒数 |
| `--voice` | オフ | 音声コマンドを有効化 (要 `vosk`, `pyaudio` と日本語モデル) |
| `--api-port` | `8787` | ローカルAPIのポート。`0` で無効化 |
| `--program` | なし | 行事の次第JSON。指定すると `N` キー / `/program/advance` で演目を進行できる |
| `--no-library` | オフ | bgm-library (Node.js) の自動起動をスキップする |
| `--profile-startup` | オフ | 起動の各ステップの所要時間を計測してコンソールに表示する(起動が遅いときの切り分け用) |

`--hand-sign` を付けない場合、`main.py` はカメラなしのヘッドレスループで動作し、
`control.html` / 音声 / APIからのコマンドを処理し続ける(`Ctrl+C` で終了)。

## BGMライブラリ管理 (bgm-library)

`http://127.0.0.1:4000` で開く管理画面。曲は `tracks/tracks.json` に
`{id, filename, title, displayTitle, author, arranged, note, sourceUrl?, sourceTrackId?}`
の形で記録され、実ファイルは `tracks/<id>.mp3` として保存される。

### 曲を追加する3つの方法

1. **ファイルをアップロード**: mp3/wav/oggファイルと曲名を指定してアップロード
2. **YouTubeからダウンロード**: URLを入力 →「情報取得」でタイトル/投稿者を自動入力
   → 内容を確認・編集して「ダウンロードしてライブラリに追加」
   (要 `yt-dlp` + `ffmpeg`。著作権・利用規約の確認は利用者側の責任)
3. **ボーカル除去**: 登録済みの曲の「ボーカル除去」ボタン →
   [Demucs](https://github.com/facebookresearch/demucs) でインストゥルメンタル版を
   生成し、`(Instrumental)` 付きの新しい曲として追加(元の曲は残る)。
   要 `pip install demucs`(main.pyと同じvenv)。ボタンを押すと即座に処理が始まり、
   完了までボタンに `処理中... N%` と進捗が表示される。

   - **モデル**: 既定で高品質な `htdemucs_ft`(4モデルのアンサンブルで既定の
     `htdemucs` より高精度だが約4倍遅い)。`bgm-library/.env` の `DEMUCS_MODEL`
     で変更可(速度優先なら `htdemucs` に戻せる)。
   - **実行デバイス**: 起動時に自動検出し、NVIDIA GPU(CUDA)→ Apple Silicon
     (MPS)→ CPU の優先順で使えるものを選ぶ(起動ログに `ボーカル除去: モデル=...
     デバイス=...` と表示される)。`.env` の `DEMUCS_DEVICE` に `cuda`/`mps`/`cpu`
     を指定すると固定できる。CPU実行の場合は曲の長さと同程度〜数倍の処理時間が
     かかる(CPUのマルチプロセス並列化は単一トラックだとオーバーヘッドの方が
     大きく逆に遅くなるため未使用)。

### 各曲のフィールド

| フィールド | 用途 |
|---|---|
| `title` | 内部管理用のフル曲名 |
| `displayTitle` | 画面表示用。著作権上フルで出せない曲名は一部伏字にできる (例: `YAJU&U` → `■■■■&U`) |
| `author` | 作者/アーティスト名。last.fm検索補助 (`.env` に `LASTFM_API_KEY` 設定時) で候補を検索できる。フリーBGM素材(魔王魂など)は基本ヒットしない |
| `arranged` | 「当方でBGM化(編集・二次利用)した音源」の注記フラグ。ボーカル除去で生成した曲は自動でON |
| `note` | 自由記述の注記 |

last.fm APIキーは https://www.lastfm.jp/api/account/create で無料取得できる
(Premium等の契約は不要)。取得したら `bgm-library/.env` の `LASTFM_API_KEY` に設定し、
サーバーを再起動する。

## 転換用プレイリスト

劇や演奏の転換で流すBGMは、`bgm-library` の「転換用プレイリスト」セクションで
名前を付けて作成する(例: 「開幕BGM」「転換1」)。1つのプレイリストに複数の曲を
順番に登録でき、同じプレイリストを複数の演目で使い回せる。曲の追加・並べ替え・
削除、任意の注記(例: 「演目1→2用」のようなメモ)を付けられる。プレイリストの
実体は `tracks/playlists.json`。

既定では、プレイリストが最後まで流れたら自動で先頭に戻ってループする
(転換用・上演中用どちらも)。各プレイリストの「最後まで流れたら繰り返す」
チェックボックスをオフにすると、1回流れ切ったところで無音のまま止まる
(次の演目に進むまで)。「歌のインストを上演中BGMに使っていて、流れ終わったら
勝手に頭から繰り返されると困る」といったケースに使う。

## 行事の次第 (プログラム進行)

劇や演奏など、BGMを流さずに進行する演目が混ざる行事(発表会・学芸会)向けの機能。
`bgm-library` の管理画面の「行事の次第」セクションで編集し、`program.json` に保存される。

- 演目を追加し、各演目のドロップダウンから2種類のプレイリストをそれぞれ選んで付け外しできる
  - **転換に使うプレイリスト**: その演目に「入るとき」(転換中)に流すBGM
  - **上演中に流すプレイリスト**: その演目が「上演中」の間ずっと流すBGM
    (劇の劇伴・演奏中のBGMなど)
  - どちらも選ばなければ完全にBGMなしの演目になる
- 選んだプレイリストの中身(曲順)はプレビュー表示される。編集は「転換用プレイリスト」
  セクション側で行う

`main.py --program program.json` で起動すると、`N` キー(または `control.html` の
「次の演目へ」ボタン、または `POST /program/advance`)で進行できる。`B` キー
(または「戻る」ボタン、`POST /program/back`)で直前の操作を1つ取り消せる。

- **開始前 → 進める**: 演目1に転換用プレイリストが割り当てられていれば、まずその
  転換(BGM再生)から明示的に始まる。割り当てが無ければ演目1がいきなり
  「上演中」になる(どちらで始まるかは事前に管理画面・配信画面に表示される)
- **上演中 → 次の演目に転換用BGMがある**: そのプレイリストを再生開始し「転換中」になる。
  最後の曲まで流れたら自動的に先頭に戻ってループする(無音にならない)
- **転換中 → もう一度進める**: 転換用BGMを止め、次の演目の「上演中」になる。この演目に
  上演中プレイリストが割り当てられていれば、切り替わった瞬間からそのBGMが流れ始め、
  こちらも自動でループする(無ければBGMなしの上演中になる)
- **上演中 → 次の演目に転換用BGMがない**: 即座にその演目の「上演中」になる
- **最後の演目で進める**: 「次第は最後の演目です」と表示され、状態は変化しない
- **戻る**: 直前に「進める」で行った変化を1つ取り消す(転換中なら1つ前の上演中に、
  上演中ならその転換中に、というように逆順にたどれる。開始前まで戻ると
  それ以上は戻れない。上演中BGMが流れていた場合はそのBGMも一緒に止まる)

`/now-playing` API のレスポンス形式もこの状態に応じて変わる(後述のAPIリファレンス参照)。

### 同じプレイリストを使い回したときの再生位置

同じプレイリスト(転換用・上演中用どちらも)を複数の演目で使っている場合、
2回目以降は毎回1曲目からではなく、前回そのプレイリストで流れ終わった曲の
次の曲から再生される(最後まで行けば先頭に戻る)。`戻る` で巻き戻すと、
この再開位置も一緒に元に戻る。

### 今どの曲が流せるかを明示

`control.html` の次第カードには、今の状態で実際に流れる(流れている)プレイリストが
表示される。BGMが実際に再生中(転換中、または上演中プレイリストが割り当てられた
演目の上演中)なら「再生中のプレイリスト」として現在再生中の曲を強調表示し、
何も流れていなければ「次に進めると流れるプレイリスト」としてこの後 `N` を押したときに
流れる予定の曲を先に確認できる。

### リハーサル後のリセット

同じプレイリストを使い回したときの再開位置(上記)や「戻る」履歴は、`main.py`
を再起動しない限り進行状態としてプロセス内に残り続ける。リハーサルをそのまま
本番に持ち越したくない場合は、`control.html` の次第カードにある
「↺ 最初からにリセット」ボタン(または `POST /program/reset`)を本番直前に押すと、
`main.py` を再起動せずに進行状態(現在の演目・戻る履歴・再開位置すべて)を
開始前の状態に戻せる。誤操作防止のため確認ダイアログが出る。

### 後方互換

古い形式(演目に `"bgm": [曲id, ...]` を直接指定)で保存された `program.json` も
そのまま動作する。`bgm-library` の管理画面でその演目にプレイリストを新たに割り当てて
保存すると、`playlistId` 形式に置き換わる。

## 管理画面 (control.html)

操作者専用のダッシュボード。**OBSのシーンには絶対に追加しないこと**。

- **NOW PLAYING**: 現在の曲名・再生状態・音量・トラック番号
- **コントロール**: ▶/⏸(再生/一時停止)・■(停止)・⏮/⏭(前後の曲)・🔉/🔊(音量)・
  🔁 リピート・🔒/🔓 プレイリスト制限(後述)
- **次第カード** (`--program` 使用時のみ表示): 開始前/上演中/転換中の状態、
  今流れている(流れる予定の)プレイリストのプレビュー、「戻る (B)」ボタン
  (戻れない場合は自動的に無効化される)、「次の演目へ (N)」ボタン
- **PLAYLIST**: ライブラリの全曲を表示、再生中の曲をハイライト。`arranged` な曲には
  `[BGM化]` タグが付く
- **配信画面の位置調整**: `monitor1.html` / `monitor2.html` の物理的な高さ(cm)と
  上下オフセット(px)をここから遠隔調整する(モニター側は操作不要)

### リピート

🔁ボタンで通常再生(次第の転換以外)の曲末尾ループを切り替える。ONの間は曲が
終わると同じ曲を繰り返す。行事の次第の転換用プレイリストは元々自動でループする
仕様なので、これとは独立している。

### プレイリスト制限 (⏮/⏭の範囲を限定)

デフォルトで🔒(制限ON)になっている。この状態では ⏮/⏭ は「今の演目に紐づいた
転換用プレイリストに登録されている曲」の範囲内でしか動かない
(転換中ならそのプレイリスト、上演中・開始前なら次に進めると流れる予定の
プレイリストが対象)。対象のプレイリストに曲が1つも無い場合、⏮/⏭ は何もしない。
`--program` を指定していない場合はこの制限自体が働かない(演目の概念が無いため)。

誤って無関係な曲へ飛ばないための安全装置だが、🔓ボタンでいつでも解除でき、
解除中はライブラリの全曲を自由に行き来できる。もう一度押せば制限に戻る。

## 配信用画面 (monitor1.html / monitor2.html / display.html)

- **`monitor1.html`(左) / `monitor2.html`(右)**: 2枚のモニター(サイズ・解像度が
  違ってもOK)を横に繋げて1つの巨大パネルとして曲名を表示する。OBSの
  **Browser Source** としてそれぞれ追加するのが推奨。
  - 通常再生時・上演中: 2画面をまたぐ巨大文字で曲名/演目名を表示
  - 転換中 (`--program` 使用時): 2画面が独立表示に切り替わる
    - Monitor 1(左): 「Next: 次の演目名」を大きく表示(上に「転換中」バッジ)
    - Monitor 2(右): 「再生中: 曲名/作者」を小さく表示
- **`display.html`**: 単体モニター用のシンプル版。`monitor1/2.html` と併用しないこと。
- 位置調整は `control.html` から行う(`/calib` API経由でポーリング反映、0.5〜1秒程度)。

## ハンドサイン操作 (`--hand-sign` 指定時)

| ジェスチャー | 動作 |
|---|---|
| ✋ パー(5本開く) | 再生 / 一時停止トグル |
| ✊ グー(全指を閉じる) | 停止(曲の先頭に戻る) |
| 👍 サムズアップ(親指だけ立てて上向き) | 音量アップ |
| 👎 サムズダウン(親指だけ立てて下向き) | 音量ダウン |
| 👉 人差し指だけ伸ばして右に傾ける | 次の曲 |
| 👈 人差し指だけ伸ばして左に傾ける | 前の曲 |

カメラウィンドウで `q` キーで終了、`--program` 指定時は `n` キーで次の演目へ進行。
認識は距離比ベース(回転に強い)+直近数フレーム一致で確定させる安定化フィルタ付き。

## 音声コマンド (`--voice` 指定時)

[Vosk](https://alphacephei.com/vosk/) によるオフライン音声認識(ネット接続不要)。
`models/vosk-model-small-ja-0.22` に日本語モデルの配置が必要
([alphacephei.com/vosk/models](https://alphacephei.com/vosk/models) からダウンロード)。

| 発話例 | 動作 |
|---|---|
| 「再生」「プレイ」 | 再生 |
| 「一時停止」「止めて」「ポーズ」 | 一時停止 |
| 「停止」「ストップ」 | 停止 |
| 「次」「スキップ」 | 次の曲 |
| 「前」「戻って」 | 前の曲 |
| 「音量上げ」「大きく」 | 音量アップ |
| 「音量下げ」「小さく」 | 音量ダウン |

認識対象は上記コマンドの単語だけに絞った語彙制約をかけているため、関係ない言葉に
惑わされにくい。ジェスチャーと音声は同じコマンドキューで処理されるため同時使用可。

## links.html (リンク一覧)

`main.py` 実行中に `http://127.0.0.1:8787/links.html` を開くと、
`control.html` / `bgm-library` / `monitor1.html` / `monitor2.html` / `display.html`
へのリンクと、`main.py` のAPI一覧をまとめて確認できる。

## main.py ローカルAPI リファレンス

ベースURL: `http://127.0.0.1:8787` (`--api-port` で変更可)。全レスポンスはJSON、
CORSヘッダー付き(ブラウザ/OBSから直接fetch可能)。

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/now-playing` | 配信画面向け。`--program` 未使用時は `{track: {title, author, arranged, note?}, playing, volume, index, total_tracks, repeat, restricted, restricted_active, tracks}`。使用時は開始前なら `{mode: "ready", starts_with: "transition"\|"performing", next_item}`、上演中なら `{mode: "performing", current_item, bgm?: {title, author, arranged, note?}}`(上演中プレイリストがある演目のみ `bgm` を含む)、転換中なら `{mode: "transition", next_item, bgm: {title, author, arranged, note?}}` |
| `GET` | `/admin/status` | 管理画面向け。`{player: <player.status()と同じ>, program: <programがあればadmin_status()、なければnull>}`。`program.admin_status()` は `status()` に `started`, `can_go_back`, `current_idx`, `total_items`, `items`(演目名一覧)を追加したもの。さらに転換中は `current_playlist`(`{id,title,author}` の配列)と `current_playlist_index`、それ以外は `upcoming_playlist`(次に進めると流れる予定のプレイリスト)を含む |
| `POST` | `/command` | 再生操作。Body: `{"command": "PLAY_PAUSE"\|"STOP"\|"NEXT"\|"PREV"\|"VOL_UP"\|"VOL_DOWN"\|"REPEAT_TOGGLE"\|"RESTRICT_TOGGLE"}`。202で受理、内部のコマンドキューに積まれメインループで実行される |
| `POST` | `/program/advance` | 次第を次の演目へ進める(`--program` 未指定時は400) |
| `POST` | `/program/back` | 直前の `/program/advance` を1つ取り消す(`--program` 未指定時は400) |
| `POST` | `/program/reset` | 進行状態を開始前(未開始)にリセットする。同じプレイリストの再開位置(`playlist_positions`)や戻る履歴も含めて全部クリアされる。リハーサルの続き位置が本番に持ち越されないよう、本番前に`control.html`の「↺ 最初からにリセット」ボタン(またはこのAPI)を呼ぶ想定(`--program` 未指定時は400) |
| `GET` | `/calib` | モニター1・2のキャリブレーション状態 `{"1": {heightCm, yOffsetPx}, "2": {...}}` |
| `POST` | `/calib` | キャリブレーション更新。Body例: `{"monitor": "1", "heightCm": 30}` または `{"monitor": "1", "yOffsetPx": 10}` |

## bgm-library API リファレンス

ベースURL: `http://127.0.0.1:4000` (`.env` の `PORT` で変更可)。

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/api/tracks` | ライブラリ全曲を返す |
| `POST` | `/api/tracks` | ファイルアップロード (multipart/form-data: `file`, `title`, `author?`, `displayTitle?`, `arranged?`, `note?`) |
| `PATCH` | `/api/tracks/:id` | メタデータ編集 (`title`/`displayTitle`/`author`/`note`/`arranged` の一部) |
| `DELETE` | `/api/tracks/:id` | 曲を削除(ファイルごと)。行事の次第から参照中なら `warning` を返す |
| `GET` | `/api/youtube-info?url=` | YouTube動画のタイトル・投稿者を取得(ダウンロードはしない) |
| `POST` | `/api/tracks/from-youtube` | YouTubeから音声をダウンロードして登録。Body: `{url, title, author?, displayTitle?, arranged?, note?}` |
| `POST` | `/api/tracks/:id/remove-vocals` | Demucsでボーカル除去を開始し、`{jobId}` を即返す(処理はバックグラウンドで継続) |
| `GET` | `/api/jobs/:id` | ジョブの進捗確認。`{status: "running"\|"done"\|"error", progress: 0-100, result?, error?}` |
| `GET` | `/api/artist-search?q=` | last.fmで作者候補を検索 (`LASTFM_API_KEY` 未設定なら `{enabled: false}`) |
| `GET` | `/api/playlists` | 転換用プレイリスト一覧を返す |
| `POST` | `/api/playlists` | プレイリスト作成。Body: `{name, trackIds?, note?}` |
| `PATCH` | `/api/playlists/:id` | プレイリスト編集 (`name`/`trackIds`/`note` の一部) |
| `DELETE` | `/api/playlists/:id` | プレイリストを削除。行事の次第から参照中なら `warning` を返す |
| `GET` | `/api/program` | 行事の次第を取得 |
| `PUT` | `/api/program` | 行事の次第を保存。Body: `[{name, playlistId: 転換用プレイリストid\|null, performingPlaylistId: 上演中用プレイリストid\|null}, ...]` の配列 |

## トラブルシューティング

- **ジェスチャーが誤爆/認識されにくい**: `main.py` の `GestureRecognizer` の
  `EXTENDED_RATIO`(既定 `1.15`)や、`NEXT`/`PREV`・`VOL_UP`/`VOL_DOWN` 判定の
  座標差しきい値(`0.08`, `0.1`)を調整する。
- **手が2つ映ると誤爆しやすい**: `max_num_hands=1` にしてあるため通常は問題ないが、
  複数人操作を想定する場合は `main.py` 内の値を変更する。
- **認識精度を上げたい**: `model_complexity=0` を `1` に変更すると精度は上がるが
  重くなる(ノートPC等では `0` 推奨)。
- **カメラプレビュー内の日本語(トラック名)が文字化けする**: `main.py` の
  `_JP_FONT_CANDIDATES` にお使いの日本語フォントパスを追加する。
- **音声認識が反応しない/精度が低い**: マイクに近づく、`models/vosk-model-small-ja-0.22`
  が正しく配置されているか確認する。
- **`monitor1/2.html` や `control.html` が真っ暗/未接続表示のまま**: `main.py` が
  起動しているか、`file://` で直接開いていないか(`http://127.0.0.1:8787/...` の
  形式で開く)を確認する。
- **YouTubeダウンロードが403などで失敗する**: `yt-dlp` が古いと失敗しやすい。
  `pip install --upgrade yt-dlp` で更新する。
- **ボーカル除去/YouTubeダウンロードのボタンが常に失敗する**: `bgm-library` の
  起動ログに `yt-dlp が見つかりません` / `demucs' が実行できません` の警告が
  出ていないか確認する。`PYTHON_CMD` / `YT_DLP_CMD` / `FFMPEG_CMD` を `.env` で
  明示指定することもできる。
- **ボーカル除去がMac(Apple Silicon)のGPUを使ってくれない/遅い**: 起動ログの
  `ボーカル除去: モデル=... デバイス=...` を確認する。`デバイス=cpu` のままなら
  `python -c "import torch; print(torch.backends.mps.is_available())"` で
  そのvenvのtorchがMPS対応か確認する(`False` ならtorchをMPS対応版に入れ直す)。
  `.env` の `DEMUCS_DEVICE=mps` で強制指定もできる。
