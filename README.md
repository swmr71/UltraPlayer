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
python main.py --dir ./tracks --camera 0 --cooldown 1.0 --voice
```

- `--camera`: 複数カメラがある場合のデバイス番号 (デフォルト 0)
- `--cooldown`: 同じジェスチャーを連続で誤爆させないための待ち時間(秒)
- `--voice`: 音声コマンドを有効化

## 調整のヒント

- ジェスチャーが認識されにくい/誤爆する場合は `GestureRecognizer` 内のしきい値
  (`0.08`, `0.1` など、指先と手首の座標差の閾値) を調整してください。
- 手が2つカメラに映ると誤爆しやすいので、`max_num_hands=1` にしています。
  複数人での操作を想定する場合は `main.py` 内の値を変更してください。
- 認識精度を上げたい場合は `model_complexity=0` を `1` に変更すると精度は上がりますが
  処理が重くなります(ノートPC等では0推奨)。
