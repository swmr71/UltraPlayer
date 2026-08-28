"""
BGM Hand Sign Player
=====================
Webカメラでハンドサインを認識してBGMを操作するプレイヤー。

ジェスチャー一覧:
    ✋ パー (5本開く)      -> 再生 / 一時停止 トグル
    ✊ グー               -> 停止 (曲の先頭に戻る)
    👍 サムズアップ        -> 音量アップ
    👎 サムズダウン        -> 音量ダウン
    👉 人差し指のみ・右向き -> 次の曲
    👈 人差し指のみ・左向き -> 前の曲

音声コマンド (任意, --voice で有効化):
    「再生」「止めて / 一時停止」「停止」「次」「前」「音量上げて」「音量下げて」

依存ライブラリ:
    pip install opencv-python mediapipe pygame
    pip install vosk pyaudio   # --voice を使う場合のみ
    # さらに models/vosk-model-small-ja-0.22 に日本語モデルを配置すること
    # (https://alphacephei.com/vosk/models からダウンロード)

使い方:
    python main.py --dir ./tracks
    python main.py --dir ./tracks --voice
    python main.py --dir ./tracks --program program.json   # 行事の次第と連動 (Nキーで次の演目へ)

行事の次第(演目リスト)と連動させる場合:
    --program で program.json のようなファイルを指定すると、Nキーで次の演目に
    進行できる。演目にBGMが指定されていれば転換中としてそのBGMを再生し、
    もう一度Nキーを押すと上演中(BGM停止)に切り替わる。
    /now-playing API は、上演中は演目名のみ、転換中は次の演目名+再生中BGMを返す。
    詳しくは program.example.json を参照。

    BGMはtracks.jsonの曲id (UUID) で指定する。tracks.json や、発表演目への
    曲割り当ては手で書かず、bgm-library/ の管理アプリ (Node.js) から行う。
        cd bgm-library
        npm install
        npm start
    で http://127.0.0.1:4000 が起動し、曲のアップロード・作者/伏字タイトルの
    編集・行事の次第への割り当てができる (TRACKS_DIR/PROGRAM_FILE を
    main.py の --dir / --program と同じ場所に向けておくこと)。
"""

import argparse
import os
import time
import glob
import sys
import threading
import queue
import math
import json
import re
import shutil
import signal
import subprocess
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pygame
from PIL import Image, ImageDraw, ImageFont

# cv2 / mediapipe / numpy はカメラ(--hand-sign)専用で、特にmediapipeの import
# だけで数秒かかることがある。--hand-sign を使わない起動(既定)を軽くするため、
# 実際に --hand-sign が指定されたときだけ main() 内で遅延importする
# (import後はここへ代入されるモジュールレベル変数を、下のdraw_text_ja等が使う)。
cv2 = None
mp = None
np = None


# ------------------------------------------------------------
# 日本語テキスト描画 (cv2.putTextは日本語グリフを描画できず ??? になるため、
# PILで日本語フォントを使って描画してからcv2の画像に戻す)
# ------------------------------------------------------------
_JP_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
_jp_font_cache = {}


def _get_jp_font(size: int):
    if size in _jp_font_cache:
        return _jp_font_cache[size]
    font = None
    for path in _JP_FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _jp_font_cache[size] = font
    return font


def draw_texts_ja(frame, items):
    """複数行の日本語テキストをまとめて描画するヘルパー。

    items: [(text, org, font_size, color(BGR)), ...]

    1行ごとに呼ぶとフレーム全体のBGR<->RGB変換とコピーが行数分だけ走って重いので、
    1フレーム分の行をまとめて渡し、変換を1往復で済ませる。
    """
    if not items:
        return
    pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)
    for text, org, font_size, color in items:
        draw.text(org, text, font=_get_jp_font(font_size), fill=(color[2], color[1], color[0]))
    frame[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def draw_text_ja(frame, text: str, org, font_size: int = 24, color=(255, 255, 255)):
    """1行だけ描画するヘルパー (draw_texts_ja の薄いラッパー)"""
    draw_texts_ja(frame, [(text, org, font_size, color)])


# ------------------------------------------------------------
# ジェスチャー判定ロジック
# ------------------------------------------------------------
class GestureRecognizer:
    """MediaPipeの手のランドマークからジェスチャー名を判定するクラス"""

    # ランドマークID (MediaPipe Hands仕様)
    TIP_IDS = [4, 8, 12, 16, 20]      # 親指, 人差し指, 中指, 薬指, 小指の指先
    PIP_IDS = [3, 6, 10, 14, 18]      # それぞれの第2関節付近

    # 指が「開いている」と判定するための、TIP-WRIST距離 / PIP-WRIST距離 の最低比率
    # 1.0だとTIPがPIPよりわずかでも遠ければ開扱いになりノイズに弱いので余裕を持たせる
    EXTENDED_RATIO = 1.15

    def __init__(self, cooldown_sec: float = 1.0, stable_frames: int = 4):
        self.cooldown_sec = cooldown_sec
        self.last_gesture = None
        self.last_fire_time = 0.0
        self.history = deque(maxlen=stable_frames)

    @staticmethod
    def _dist(a, b):
        return math.hypot(a.x - b.x, a.y - b.y)

    def _finger_states(self, landmarks):
        """各指が伸びているかどうかを bool のリストで返す [親指, 人差し指, 中指, 薬指, 小指]

        手首や指の付け根からの距離比で判定することで、手をどの向きに
        回転させても (縦持ち・横持ちでも) 安定して判定できるようにしている。
        """
        wrist = landmarks[0]
        states = []

        # 親指は横に折りたたむ構造なので、手首基準ではなく「小指の付け根(17)」
        # からの距離で判定する (折りたたむと手のひら側=小指付け根に近づく)
        pinky_mcp = landmarks[17]
        thumb_tip_dist = self._dist(landmarks[self.TIP_IDS[0]], pinky_mcp)
        thumb_mcp_dist = self._dist(landmarks[2], pinky_mcp)
        states.append(thumb_tip_dist > thumb_mcp_dist * self.EXTENDED_RATIO)

        # 他の4本は手首からの距離比で判定 (回転しても崩れにくい)
        for tip_id, pip_id in zip(self.TIP_IDS[1:], self.PIP_IDS[1:]):
            tip_dist = self._dist(landmarks[tip_id], wrist)
            pip_dist = self._dist(landmarks[pip_id], wrist)
            states.append(tip_dist > pip_dist * self.EXTENDED_RATIO)

        return states  # [thumb, index, middle, ring, pinky]

    def recognize(self, landmarks):
        thumb, index, middle, ring, pinky = self._finger_states(landmarks)
        count = sum([thumb, index, middle, ring, pinky])

        # パー: 5本すべて開いている
        if count == 5:
            return "PLAY_PAUSE"

        # グー: すべて閉じている
        if count == 0:
            return "STOP"

        # 人差し指だけ伸びている -> 左右の向きで次/前を判定
        if index and not middle and not ring and not pinky and not thumb:
            wrist_x = landmarks[0].x
            tip_x = landmarks[8].x
            if tip_x - wrist_x > 0.08:
                return "NEXT"
            elif wrist_x - tip_x > 0.08:
                return "PREV"
            return None

        # 親指だけ伸びている -> 上下の向きで音量調整
        if thumb and not index and not middle and not ring and not pinky:
            thumb_tip_y = landmarks[4].y
            wrist_y = landmarks[0].y
            if wrist_y - thumb_tip_y > 0.1:
                return "VOL_UP"
            elif thumb_tip_y - wrist_y > 0.1:
                return "VOL_DOWN"
            return None

        return None

    def stabilize(self, gesture: str) -> str:
        """直近 stable_frames フレーム全てで同じジェスチャーが出たときだけ確定させる。
        一瞬のブレやカメラ角度による誤認識を弾くためのフィルタ。
        """
        self.history.append(gesture)
        if gesture is None:
            return None
        if len(self.history) < self.history.maxlen:
            return None
        if all(g == gesture for g in self.history):
            return gesture
        return None

    def fire(self, gesture: str) -> bool:
        """クールダウンを考慮して、同じジェスチャーの連続発火を防ぐ"""
        now = time.monotonic()
        if gesture is None:
            self.last_gesture = None
            return False
        if gesture == self.last_gesture and (now - self.last_fire_time) < self.cooldown_sec:
            return False
        self.last_gesture = gesture
        self.last_fire_time = now
        return True


# ------------------------------------------------------------
# 音声コマンド認識 (別スレッドでマイクを聴き続ける)
# ------------------------------------------------------------
class VoiceController:
    """マイク入力から音声コマンドを認識し、command_queue にジェスチャー名と
    同じ文字列(PLAY_PAUSE, STOP, NEXT, PREV, VOL_UP, VOL_DOWN)を積むクラス。
    ジェスチャー側と同じキューを共有することで、メインループ側の処理を一本化できる。

    Vosk (オフライン音声認識) を使い、認識対象の単語をグラマー(語彙制約)として
    明示的に渡すことで、コマンド以外の言葉に惑わされにくくしている。
    """

    MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "vosk-model-small-ja-0.22")
    SAMPLE_RATE = 16000

    # 認識テキストに含まれるキーワード -> コマンド名
    KEYWORD_MAP = [
        (["一時停止", "止めて", "ポーズ"], "PLAY_PAUSE"),
        (["再生", "プレイ"], "PLAY_PAUSE"),
        (["停止", "ストップ"], "STOP"),
        (["次", "スキップ"], "NEXT"),
        (["前", "戻って"], "PREV"),
        (["音量上げ", "ボリューム上げ", "上げる", "大きく"], "VOL_UP"),
        (["音量下げ", "ボリューム下げ", "下げる", "小さく"], "VOL_DOWN"),
    ]

    def __init__(self, command_queue: "queue.Queue[str]"):
        self.command_queue = command_queue
        self._stop_flag = threading.Event()
        self._thread = None

        if not os.path.isdir(self.MODEL_DIR):
            raise RuntimeError(
                f"Voskモデルが見つかりません: {self.MODEL_DIR}\n"
                "https://alphacephei.com/vosk/models から "
                "vosk-model-small-ja-0.22 をダウンロードして models/ に配置してください。"
            )

        # 遅延importにして、--voiceを使わない人はインストール不要にする
        import vosk
        import pyaudio

        vosk.SetLogLevel(-1)
        self._json = json
        self._pyaudio = pyaudio

        self.model = vosk.Model(self.MODEL_DIR)

        # 認識対象の単語だけに絞ったグラマーを作る (語彙制約で誤認識を減らす)
        grammar_words = sorted({w for keywords, _ in self.KEYWORD_MAP for w in keywords})
        grammar_words.append("[unk]")
        self.recognizer = vosk.KaldiRecognizer(self.model, self.SAMPLE_RATE, json.dumps(grammar_words, ensure_ascii=False))

    def _match_command(self, text: str):
        text = text.replace(" ", "")
        for keywords, command in self.KEYWORD_MAP:
            if any(k in text for k in keywords):
                return command
        return None

    def _listen_loop(self):
        try:
            self._listen_loop_inner()
        except Exception as e:
            # スレッド内の例外はプロセスを落とさないが、黙って死ぬと
            # 「音声コマンドだけ効かない」状態の原因が分からなくなる。
            print(f"[警告] 音声認識を停止しました (マイクが外れた等): {e}")

    def _listen_loop_inner(self):
        pa = self._pyaudio.PyAudio()
        stream = pa.open(
            format=self._pyaudio.paInt16,
            channels=1,
            rate=self.SAMPLE_RATE,
            input=True,
            frames_per_buffer=4000,
        )
        stream.start_stream()
        try:
            while not self._stop_flag.is_set():
                data = stream.read(4000, exception_on_overflow=False)
                if self.recognizer.AcceptWaveform(data):
                    result = self._json.loads(self.recognizer.Result())
                    text = result.get("text", "").strip()
                    if not text:
                        continue

                    command = self._match_command(text)
                    if command:
                        self.command_queue.put(command)
                        print(f"[音声] 認識: 「{text}」 -> {command}")
                    else:
                        print(f"[音声] 認識: 「{text}」 (対応コマンドなし)")
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

    def start(self):
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()


# 曲を切り替えるときのフェードアウト/フェードインの長さ(ミリ秒)。
# 上演中BGMの切り替え(ProgramController.tick()等)だけは0を渡してフェード無しにする。
DEFAULT_FADE_MS = 400


# ------------------------------------------------------------
# BGMプレイヤー本体
# ------------------------------------------------------------
class BGMPlayer:
    """音源を管理して再生するクラス。

    track_dir に tracks.json (bgm-library アプリが書き出すライブラリ台帳) が
    あればそれを読み込み、id・曲名・表示用曲名(伏字対応)・作者・「当方でBGM化した
    二次利用」注記を持つ構造化データとして扱う。tracks.json が無い場合は従来通り
    フォルダ内の mp3/wav/ogg を素朴に列挙する (曲名などのメタデータは無し)。
    """

    def __init__(self, track_dir: str):
        # 音声デバイス未接続・他アプリの排他占有・リモートデスクトップ接続中などで
        # 失敗する。pygameの生の例外のままだと原因が分かりにくいので、
        # 呼び出し側(main)がそのまま表示できるメッセージに包み直す。
        try:
            pygame.mixer.init()
        except Exception as e:
            raise RuntimeError(
                "音声デバイスを初期化できませんでした。デバイスが接続されているか、"
                f"他のアプリが排他モードで占有していないか確認してください ({e})"
            ) from e
        self.track_dir = track_dir
        self.library = self._load_library(track_dir)
        if not self.library:
            print(f"[警告] {track_dir} に音源ファイルが見つかりません (mp3/wav/ogg)")
        self._library_mtime = self._tracks_json_mtime()
        self.index = 0
        self.volume = 0.5
        self.playing = False
        # pygame.mixer.music.get_busy() は一時停止中もFalseを返すため、
        # 「一時停止/停止で意図的に音を止めた」のか「曲が自然に終わった」のかを
        # tick()側で区別するために使う (paused=Trueの間は自然終了扱いしない)。
        self.paused = False
        self.repeat = False  # 通常再生時、曲が終わったら繰り返すかどうか
        self.restricted = True  # ⏭/⏮ を allowed_ids 内の曲だけに制限するかどうか
        self.locked = False  # ONの間、control.htmlからの操作系リクエストをすべて拒否する
        self.allowed_ids = None  # 制限対象の曲idのリスト (Noneなら制限データ無し=無制限、順序はプレイリストの再生順)
        # 再生経過秒数の自前管理。pygame.mixer.music.get_pos()はseek(set_pos)後も
        # 実際の再生位置に追従せず、最初にplay()した時刻からの経過時間を返し続ける
        # だけなので、シーク機能のためにここで基準値+開始時刻から計算する。
        self._elapsed_base = 0.0        # 一時停止中/直近シーク時点での経過秒数
        self._elapsed_started_at = None  # 上記基準からの計測開始wall clock時刻 (Noneなら停止/一時停止中)
        self._apply_volume()
        if self.library:
            self._load_current()

    @staticmethod
    def _load_library(track_dir: str):
        manifest_path = os.path.join(track_dir, "tracks.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                library = []
                for e in entries:
                    path = os.path.join(track_dir, e["filename"])
                    if not os.path.isfile(path):
                        print(f"[警告] tracks.jsonに記載のファイルが見つかりません: {e['filename']}")
                        continue
                    library.append({
                        "id": e["id"],
                        "path": path,
                        "title": e.get("title", e["filename"]),
                        "displayTitle": e.get("displayTitle") or e.get("title", e["filename"]),
                        "author": e.get("author", ""),
                        "arranged": bool(e.get("arranged", False)),
                        "note": e.get("note", ""),
                        "durationSec": e.get("durationSec"),  # bgm-libraryがffprobeで取得しキャッシュしたもの
                    })
                return library
            except Exception as e:
                print(f"[警告] tracks.jsonの読み込みに失敗しました: {e}")

        # tracks.json が無い場合は従来通りフォルダを素朴に列挙する (後方互換)
        paths = sorted(
            glob.glob(os.path.join(track_dir, "*.mp3"))
            + glob.glob(os.path.join(track_dir, "*.wav"))
            + glob.glob(os.path.join(track_dir, "*.ogg"))
        )
        return [
            {
                "id": os.path.splitext(os.path.basename(p))[0],
                "path": p,
                "title": os.path.basename(p),
                "displayTitle": os.path.basename(p),
                "author": "",
                "arranged": False,
                "note": "",
            }
            for p in paths
        ]

    def _tracks_json_mtime(self):
        path = os.path.join(self.track_dir, "tracks.json")
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def reload_library_if_changed(self):
        """bgm-libraryでの編集(曲の追加/削除/メタデータ変更)をmain.py再起動
        無しで反映するため、tracks.jsonの更新日時を見て変わっていれば読み直す。
        再生中の曲がライブラリ内の位置(index)を基準にしているため、
        再読み込み後も曲id基準で同じ曲を指し続けるようにインデックスを補正する
        (見つからなくなっていた場合は範囲内に収める)。
        """
        mtime = self._tracks_json_mtime()
        if mtime is None or mtime == self._library_mtime:
            return
        self._library_mtime = mtime
        track = self.current_track()
        current_id = track["id"] if track else None
        new_library = self._load_library(self.track_dir)
        if not new_library:
            return  # 読み込みエラー等で空になった場合は既存のライブラリを維持する

        new_index = 0
        if current_id is not None:
            for i, t in enumerate(new_library):
                if t["id"] == current_id:
                    new_index = i
                    break
            else:
                new_index = min(self.index, len(new_library) - 1)
        # 曲が減った場合、差し替え直後の一瞬だけ index が新ライブラリの範囲外に
        # なりうる (HTTPスレッドが同時に current_track() を読む)。先に index を
        # 縮めてから差し替えることで、その窓を無くす。
        self.index = min(self.index, len(new_library) - 1)
        self.library = new_library
        self.index = new_index

    def _load_current(self) -> bool:
        """現在のインデックスの曲をロードする。壊れたファイルなど
        (pygame.error 等)が原因で失敗しても例外を外に出さずFalseを返す。
        本番中にこの1曲のせいでアプリ全体が落ちるのを防ぐため。
        """
        try:
            pygame.mixer.music.load(self.library[self.index]["path"])
            return True
        except Exception as e:
            path = self.library[self.index]["path"]
            print(f"[警告] 曲を読み込めませんでした (ファイルが壊れている可能性があります): {path} ({e})")
            return False

    def _play_loaded(self, fade_ms: int = 0) -> bool:
        """ロード済みの曲の再生を開始する。_load_current() と同じく、
        失敗しても例外を外に出さずFalseを返す。

        play() は load() が成功していても、デバイスが途中で失われた等で
        pygame.error を投げうる。ここを素通しにすると、ハンドサイン経由の
        ⏭/⏮ でプロセスごと落ちる経路になっていた。
        """
        try:
            pygame.mixer.music.play(fade_ms=fade_ms)
            return True
        except Exception as e:
            print(f"[警告] 再生を開始できませんでした: {e}")
            return False

    def current_track(self):
        # HTTPスレッド(/now-playing, /admin/status)からも呼ばれる。
        # reload_library_if_changed() によるライブラリ差し替えと競合しても
        # IndexError でハンドラを落とさないよう、必ず範囲を確認する。
        library = self.library
        if not library or not (0 <= self.index < len(library)):
            return None
        return library[self.index]

    def current_name(self):
        """画面/API表示用の曲名 (伏字対応済みのdisplayTitle) を返す"""
        track = self.current_track()
        return track["displayTitle"] if track else "(no track)"

    def current_public(self) -> dict:
        """外部公開してよい範囲の現在曲情報 (内部title/ファイルパスは含めない)"""
        track = self.current_track()
        if not track:
            return {"title": "(no track)", "author": "", "arranged": False}
        info = {"title": track["displayTitle"], "author": track["author"], "arranged": track["arranged"]}
        if track["note"]:
            info["note"] = track["note"]
        if track.get("durationSec") is not None:
            info["durationSec"] = track["durationSec"]
        return info

    def elapsed_sec(self) -> float:
        """現在の曲の再生経過秒数 (再生してない/停止直後は0)。"""
        if self._elapsed_started_at is not None:
            return self._elapsed_base + (time.monotonic() - self._elapsed_started_at)
        return self._elapsed_base

    def seek(self, seconds: float):
        """曲の再生位置を指定秒数に移動する (シークバー用)。
        pygame/SDL_mixerの制約でファイル形式によっては効かない/不正確なことが
        あるため、失敗しても例外は出さず警告だけにする。
        """
        if not self.library:
            return
        if not math.isfinite(seconds):
            print(f"[警告] シーク位置が不正です (有限の数値ではありません): {seconds}")
            return
        seconds = max(0.0, seconds)
        # 曲の長さが分かっていれば、その手前までに丸める
        # (曲長を超える位置に飛ばすと環境によって無音のまま止まるため)
        track = self.current_track()
        duration = track.get("durationSec") if track else None
        if duration:
            seconds = min(seconds, max(0.0, duration - 0.5))
        try:
            pygame.mixer.music.set_pos(seconds)
        except Exception as e:
            print(f"[警告] シークに失敗しました (このファイル形式では非対応の可能性があります): {e}")
            return
        self._elapsed_base = seconds
        self._elapsed_started_at = time.monotonic() if self.playing else None

    def status(self) -> dict:
        """現在の再生状態をJSON化しやすい辞書で返す (外部API公開用)"""
        return {
            "track": self.current_public(),
            "playing": self.playing,
            "volume": round(self.volume, 2),
            "index": self.index,
            "total_tracks": len(self.library),
            "repeat": self.repeat,
            "restricted": self.restricted,
            "locked": self.locked,
            "elapsed": round(self.elapsed_sec(), 1),
            "tracks": [
                {"id": t["id"], "title": t["displayTitle"], "author": t["author"], "arranged": t["arranged"]}
                for t in self.library
            ],
        }

    def toggle_play_pause(self):
        if not self.library:
            return
        if self.playing:
            try:
                pygame.mixer.music.pause()
            except Exception as e:
                print(f"[警告] 一時停止に失敗しました: {e}")
            # pause()が失敗しても状態は「停止した」に倒す。実際に鳴り続けていても
            # 次の▶で unpause -> play にフォールバックできる。
            self.playing = False
            self.paused = True
            self._elapsed_base = self.elapsed_sec()
            self._elapsed_started_at = None
        else:
            try:
                if pygame.mixer.music.get_pos() == -1:
                    pygame.mixer.music.play()
                    self._elapsed_base = 0.0
                else:
                    pygame.mixer.music.unpause()
                self.playing = True
                self.paused = False
                self._elapsed_started_at = time.monotonic()
            except Exception as e:
                print(f"[警告] 再生に失敗しました: {e}")
                self.playing = False
                self.paused = False
                self._elapsed_started_at = None

    def stop(self, fade_ms: int = 0):
        """fade_ms>0 なら、止める前に鳴っている曲をフェードアウトしてから止める
        (次第の転換先にBGMが無い=無音になる場合に使う)。"""
        self._fadeout_current(fade_ms)
        try:
            pygame.mixer.music.stop()
        except Exception as e:
            print(f"[警告] 停止に失敗しました: {e}")
        self.playing = False
        self.paused = True
        self._elapsed_base = 0.0
        self._elapsed_started_at = None

    def tick(self):
        """毎フレーム呼ぶ想定。曲が自然に終了したときの後処理を行う
        (リピート有効なら同じ曲を再生し直す。無効ならplayingをFalseにして
        実際の無音状態と一致させる)。ProgramController側で既に処理される
        場合(転換中の曲送り)はそちらが先に曲を再生し直すので競合しない。
        """
        if not self.playing:
            return
        if pygame.mixer.music.get_busy():
            return
        if self.repeat:
            try:
                pygame.mixer.music.play()
                self._elapsed_base = 0.0
                self._elapsed_started_at = time.monotonic()
            except Exception as e:
                print(f"[警告] リピート再生に失敗しました: {e}")
                self.playing = False
                self._elapsed_started_at = None
        else:
            self.playing = False
            self._elapsed_started_at = None

    def toggle_repeat(self):
        self.repeat = not self.repeat

    def toggle_restricted(self):
        self.restricted = not self.restricted

    def _navigable_indices(self):
        """⏭/⏮ で移動してよい曲のインデックス一覧。
        restricted かつ allowed_ids が設定されていれば、その曲idに限定し、
        ライブラリ全体の並び順ではなく allowed_ids (プレイリストの再生順)
        の並び順で返す (でないと⏭/⏮がプレイリストの順番を無視してしまう)。
        """
        if self.restricted and self.allowed_ids is not None:
            index_by_id = {t["id"]: i for i, t in enumerate(self.library)}
            return [index_by_id[tid] for tid in self.allowed_ids if tid in index_by_id]
        return list(range(len(self.library)))

    def _fadeout_current(self, fade_ms: int):
        """曲を切り替える前に、実際に鳴っている曲をフェードアウトする。
        pygame/SDL_mixerは音声ストリームを1本しか持てず本当のクロスフェードは
        できないため、フェードアウト→無音→次の曲をフェードイン、の疑似クロスフェード
        にしている。fadeout()は呼んだ瞬間に返る非同期処理なので、完了を待たずに
        次の曲をロードすると今のフェードが中断されてしまう。そのため完了まで
        (fade_ms分だけ)ここでブロックしてから戻る。
        """
        if fade_ms <= 0:
            return
        if not self.playing:
            return  # 一時停止中/未再生(既に無音)ならフェードアウトの必要は無い
        try:
            pygame.mixer.music.fadeout(fade_ms)
        except Exception as e:
            # フェードできなくても曲の切り替え自体は続行する (無音のまま切り替わるだけ)
            print(f"[警告] フェードアウトに失敗しました: {e}")
            return
        time.sleep(fade_ms / 1000)

    def _navigate(self, direction: int, fade_ms: int = DEFAULT_FADE_MS):
        if not self.library:
            return
        candidates = self._navigable_indices()
        if not candidates:
            return  # 制限中で、現在の演目のプレイリストに曲が登録されていない
        if self.index in candidates:
            pos = (candidates.index(self.index) + direction) % len(candidates)
        else:
            pos = 0 if direction > 0 else -1
        self._fadeout_current(fade_ms)
        # 壊れたファイル等で読み込みに失敗したら、無限ループしないよう
        # 候補の数だけ試して次へスキップする(全滅なら諦めて無音のまま止める)。
        for _ in range(len(candidates)):
            self.index = candidates[pos]
            if self._load_current() and self._play_loaded(fade_ms):
                self.playing = True
                self.paused = False
                self._elapsed_base = 0.0
                self._elapsed_started_at = time.monotonic()
                return
            pos = (pos + direction) % len(candidates)
        self.playing = False
        self._elapsed_started_at = None

    def next_track(self, fade_ms: int = DEFAULT_FADE_MS):
        self._navigate(1, fade_ms)

    def prev_track(self, fade_ms: int = DEFAULT_FADE_MS):
        self._navigate(-1, fade_ms)

    def _apply_volume(self):
        try:
            pygame.mixer.music.set_volume(self.volume)
        except Exception as e:
            print(f"[警告] 音量の変更に失敗しました: {e}")

    def volume_up(self):
        self.volume = min(1.0, self.volume + 0.1)
        self._apply_volume()

    def volume_down(self):
        self.volume = max(0.0, self.volume - 0.1)
        self._apply_volume()

    def play_by_id(self, track_id: str, fade_ms: int = DEFAULT_FADE_MS) -> bool:
        """曲idを指定して再生する (行事プログラムの転換BGM用)。
        ファイルが壊れている等でロードに失敗した場合はFalseを返す
        (呼び出し側で「見つかりません」と同じ扱いで警告される)。
        fade_ms>0 なら曲の切り替えをフェードアウト→フェードインする
        (呼び出し側は上演中BGMの切り替えでは0を渡し、フェード無しにする)。
        """
        for i, t in enumerate(self.library):
            if t["id"] == track_id:
                self.index = i
                self._fadeout_current(fade_ms)
                if not self._load_current() or not self._play_loaded(fade_ms):
                    self.playing = False
                    self._elapsed_started_at = None
                    return False
                self.playing = True
                self.paused = False
                self._elapsed_base = 0.0
                self._elapsed_started_at = time.monotonic()
                return True
        return False

    def load_by_id(self, track_id: str) -> bool:
        """曲idを指定してロードだけ行い、再生はしない
        (上演中BGMの「自動再生しない」設定用。一時停止状態にしておき、
        操作者が▶を押した時点で toggle_play_pause() が先頭から再生を始める)。
        """
        for i, t in enumerate(self.library):
            if t["id"] == track_id:
                self.index = i
                if not self._load_current():
                    self.playing = False
                    self._elapsed_started_at = None
                    return False
                self.playing = False
                self.paused = True
                self._elapsed_base = 0.0
                self._elapsed_started_at = None
                return True
        return False


# ------------------------------------------------------------
# 行事の次第(演目リスト)を読み込み・管理するための補助
# ------------------------------------------------------------
def load_playlists(track_dir: str) -> dict:
    """tracks/playlists.json (bgm-library で作成する名前付き転換用プレイリスト) を
    id -> {"trackIds": [...], "loop": bool} の辞書として読み込む。
    "loop" は最後まで流れたら先頭に戻ってループするか(既定True、bgm-libraryの
    チェックボックスで曲ごとにOFFにできる)。

    "結合プレイリスト" (sourcePlaylistIds に他のプレイリストidを順番に指定した
    もの) は、参照先プレイリストの曲を順につなげたものとしてここで展開する
    (以降のコードは通常のプレイリストと同じ trackIds のリストとして扱える)。
    循環参照は空扱いにして無限ループを防ぐ。ファイルが無ければ空辞書を返す。
    """
    path = os.path.join(track_dir, "playlists.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception as e:
        print(f"[警告] playlists.jsonの読み込みに失敗しました: {e}")
        return {}

    # 手で編集して形が崩れている場合 (配列でない・要素が辞書でない・idが無い) に
    # ここで落とさない。この関数はメインループから毎回呼ばれうるので、
    # 例外を出すと本番中に警告が出続けることになる。
    if not isinstance(entries, list):
        print("[警告] playlists.json はプレイリストの配列である必要があります")
        return {}
    raw = {}
    for p in entries:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            print(f"[警告] playlists.json に id を持たない要素があるため読み飛ばします: {p!r:.60}")
            continue
        raw[p["id"]] = p

    def resolve(pid, ancestors=frozenset()):
        if pid in ancestors:
            return []
        p = raw.get(pid)
        if not p:
            return []
        # sourcePlaylistIdsが「キーとして存在するか」で結合プレイリスト扱いする
        # (空配列でもtrackIdsへフォールバックしない)。中身の有無ではなく
        # `or []`で判定すると、結合先を1つも追加してない結合プレイリストが
        # 古いtrackIdsを再生してしまう事故になる。
        source_ids = p.get("sourcePlaylistIds")
        if source_ids is not None:
            next_ancestors = ancestors | {pid}
            ids = []
            for sid in source_ids:
                ids.extend(resolve(sid, next_ancestors))
            return ids
        return list(p.get("trackIds", []))

    return {pid: {"trackIds": resolve(pid), "loop": p.get("loop", True)} for pid, p in raw.items()}


def load_videos(track_dir: str) -> dict:
    """tracks/videos.json (bgm-library で作成する動画ライブラリ) を
    id -> {"filename", "title", "displayTitle"} の辞書として読み込む。
    ファイルが無ければ空辞書を返す(動画機能を使っていない場合はこれで良い)。
    """
    path = os.path.join(track_dir, "videos.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception as e:
        print(f"[警告] videos.jsonの読み込みに失敗しました: {e}")
        return {}

    if not isinstance(entries, list):
        print("[警告] videos.json は動画の配列である必要があります")
        return {}
    result = {}
    for v in entries:
        if not isinstance(v, dict) or not isinstance(v.get("id"), str):
            print(f"[警告] videos.json に id を持たない要素があるため読み飛ばします: {v!r:.60}")
            continue
        result[v["id"]] = {
            "filename": v.get("filename", ""),
            "title": v.get("title", ""),
            "displayTitle": v.get("displayTitle") or v.get("title", ""),
        }
    return result


# program.json 形式:
#   [
#     {"name": "開会の言葉", "playlistId": null},
#     {"name": "劇『桃太郎』", "playlistId": "3fa2c1e4-....", "performingPlaylistId": "9b7e-...."},
#     {"name": "合唱", "playlistId": null}
#   ]
# "playlistId" はライブラリ(playlists.json)の名前付きプレイリストのidを指し、
# その演目へ「転換する際」に流すBGMを表す。"performingPlaylistId" は、その演目が
# 「上演中」の間ずっと流すBGM(劇の劇伴・演奏中のBGMなど)を表す、別のプレイリスト。
# どちらも bgm-library アプリのUIから演目に割り当てて保存すると、この形式で
# 書き出される。どちらも無い(null)演目は完全にBGMなしで上演される。
# プレイリストは1曲以上あれば順番に再生し、最後まで再生し終えたら先頭に戻って
# ループする(次に進むまで無音にしない)。
#
# 後方互換: 旧形式の "bgm": [曲id, ...] (プレイリストを介さないインライン指定、
# 転換用のみ) が残っている演目は、そのまま読み込んで使う。
# ------------------------------------------------------------
class ProgramController:
    def __init__(self, path: str, player: "BGMPlayer"):
        if not os.path.exists(path):
            raise RuntimeError(
                f"{path} が見つかりません。bgm-library の「行事の次第」で保存するか、"
                "program.example.json を参考に作成してください"
            )
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            raise RuntimeError(
                f"{path} が空です。bgm-library の「行事の次第」で演目を追加して保存してください"
            )
        try:
            self.items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{path} のJSONが壊れています ({e})") from e
        if not self.items:
            raise RuntimeError(f"{path} に演目がありません")
        # 以降のコードは演目が辞書であることを前提に item.get(...) / item["name"] を
        # 各所で使う。手で編集した program.json が壊れていると、本番中の advance() や
        # /admin/status で例外が飛び続けることになるので、起動時にまとめて弾く。
        # (main() はこの例外を捕まえて「次第なし」で起動を続ける)
        if not isinstance(self.items, list):
            raise RuntimeError(f"{path} は演目の配列である必要があります")
        for i, item in enumerate(self.items):
            if not isinstance(item, dict):
                raise RuntimeError(f"{path} の{i + 1}番目の演目がオブジェクトではありません")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"{path} の{i + 1}番目の演目に name がありません")
        self.player = player
        self.playlists = load_playlists(player.track_dir)
        self._playlists_mtime = self._playlists_json_mtime()
        self.videos = load_videos(player.track_dir)
        self._videos_mtime = self._videos_json_mtime()

        self.started = False  # 最初の進行(N)がまだ押されていない状態
        self.current_idx = 0
        self.target_idx = None
        self.mode = "performing"  # "performing"(上演中) | "transition"(転換中)
        self.bgm_queue = []
        self.bgm_pos = 0
        self.active_playlist_id = None  # 現在bgm_queueの元になっているプレイリストid
        self.active_playlist_loops = True  # 現在のbgm_queueが最後まで行ったらループするか
        self._history = []  # advance()前のスナップショットのスタック (戻る用)
        # 同じプレイリストを複数の演目(転換用・上演中用問わず)で使い回したとき、
        # 毎回1曲目からではなく前回流れ終わった曲の次から再生されるようにするための
        # 再開位置 (playlistId -> 次に再生を始めるインデックス)
        self.playlist_positions = {}

    def _bgm_ids(self, item: dict):
        """転換時に流すBGM(曲idリスト)。プレイリストの代わりに曲を1曲だけ
        直接割り当てる(trackId)こともでき、その場合はその1曲だけのキューになる
        (プレイリストを作らずに済む単曲指定用)。"""
        playlist_id = item.get("playlistId")
        if playlist_id:
            return list(self.playlists.get(playlist_id, {}).get("trackIds", []))
        track_id = item.get("trackId")
        if track_id:
            return [track_id]
        bgm = item.get("bgm") or []  # 後方互換 (インライン指定)
        if isinstance(bgm, str):
            bgm = [bgm]
        return bgm

    def _performing_bgm_ids(self, item: dict):
        """上演中ずっと流すBGM(曲idリスト)。転換用同様、プレイリストの代わりに
        performingTrackId で曲を1曲だけ直接割り当てられる。"""
        playlist_id = item.get("performingPlaylistId")
        if playlist_id:
            return list(self.playlists.get(playlist_id, {}).get("trackIds", []))
        track_id = item.get("performingTrackId")
        if track_id:
            return [track_id]
        return []

    def _video_info(self, item: dict, id_field: str, side_field: str, muted_field: str, sync_field: str):
        """転換用・上演中用共通の動画情報取り出し処理。演目に <id_field> が
        割り当てられ、videos.json 上に実際に存在する場合のみ辞書を返す
        (それ以外はNone)。<side_field>/<muted_field>/<sync_field>は
        bgm-libraryの次第編集画面で演目ごとに設定する。
        """
        video_id = item.get(id_field)
        if not video_id:
            return None
        video = self.videos.get(video_id)
        if not video:
            return None
        return {
            "title": video["displayTitle"],
            "url": f"/media/{video['filename']}",
            "side": "right" if item.get(side_field) == "right" else "left",
            "muted": bool(item.get(muted_field)),
            "syncPlayback": item.get(sync_field) is not False,
        }

    def _transition_video(self, item: dict):
        """転換中に流す動画の外部公開用情報 (videoId で割り当て)。"""
        return self._video_info(item, "videoId", "videoSide", "videoMuted", "videoSyncPlayback")

    def _performing_video(self, item: dict):
        """上演中に流す動画の外部公開用情報 (performingVideoId で割り当て)。"""
        return self._video_info(
            item, "performingVideoId", "performingVideoSide", "performingVideoMuted", "performingVideoSyncPlayback"
        )

    def _playlists_json_mtime(self):
        path = os.path.join(self.player.track_dir, "playlists.json")
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def reload_playlists_if_changed(self):
        """bgm-libraryでプレイリストを編集(曲の追加/並べ替え/結合設定変更等)した
        内容を、main.py再起動無しで反映するため、playlists.jsonの更新日時を見て
        変わっていれば読み直す。今流れている曲自体は差し替えないが、次にこの
        プレイリストが(ループや次の演目への進行で)再生されるときから新しい
        内容が使われる。
        """
        mtime = self._playlists_json_mtime()
        if mtime is None or mtime == self._playlists_mtime:
            return
        self._playlists_mtime = mtime
        self.playlists = load_playlists(self.player.track_dir)

    def _videos_json_mtime(self):
        path = os.path.join(self.player.track_dir, "videos.json")
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def reload_videos_if_changed(self):
        """bgm-libraryで動画を追加/削除した内容を、main.py再起動無しで反映する。
        再生中の動画自体は差し替えない(次に演目が切り替わったときから新しい
        内容が使われる)。reload_playlists_if_changed()と同じ仕組み。
        """
        mtime = self._videos_json_mtime()
        if mtime is None or mtime == self._videos_mtime:
            return
        self._videos_mtime = mtime
        self.videos = load_videos(self.player.track_dir)

    def _resolve_loop(self, item: dict, playlist_field: str, track_field: str) -> bool:
        """このBGMが最後まで流れたら先頭に戻ってループするかを、割り当て方に
        応じて決める。
        - プレイリスト指定: そのプレイリストの「ループ」設定に従う
        - 単体の曲を直接指定: 1回流したら無音のまま止める(プレイリストと違い
          「ずっと流し続けたい」という意図でわざわざ選んでいるわけではないため、
          曲が終わるたびに勝手に繰り返されると困る場合がある)
        - どちらも無い(旧形式のインライン指定 "bgm"): 従来通り常にループ
        """
        playlist_id = item.get(playlist_field)
        if playlist_id:
            return self._playlist_loops(playlist_id)
        if item.get(track_field):
            return False
        return True

    def _playlist_loops(self, playlist_id) -> bool:
        """指定プレイリストが最後まで流れたら先頭に戻ってループするか。
        プレイリスト未指定(旧形式のインライン指定含む)は常にTrue(従来通り)。
        """
        if not playlist_id:
            return True
        return self.playlists.get(playlist_id, {}).get("loop", True)

    def _snapshot(self) -> dict:
        return {
            "started": self.started,
            "current_idx": self.current_idx,
            "target_idx": self.target_idx,
            "mode": self.mode,
            "bgm_queue": list(self.bgm_queue),
            "bgm_pos": self.bgm_pos,
            "active_playlist_id": self.active_playlist_id,
            "active_playlist_loops": self.active_playlist_loops,
            "playlist_positions": dict(self.playlist_positions),
        }

    def _restore(self, snap: dict):
        self.started = snap["started"]
        self.current_idx = snap["current_idx"]
        self.target_idx = snap["target_idx"]
        self.mode = snap["mode"]
        self.bgm_queue = snap["bgm_queue"]
        self.bgm_pos = snap["bgm_pos"]
        self.active_playlist_id = snap["active_playlist_id"]
        self.active_playlist_loops = snap.get("active_playlist_loops", True)
        self.playlist_positions = snap["playlist_positions"]

    def _record_resume_position(self):
        """今流しているプレイリストの再開位置を記録する
        (次に同じプレイリストを使うときは続きの曲から)。"""
        if self.active_playlist_id and self.bgm_queue:
            self.playlist_positions[self.active_playlist_id] = (self.bgm_pos + 1) % len(self.bgm_queue)

    def _performing_fade_ms(self, idx=None) -> int:
        """上演中BGMを切り替えるときのフェード時間。その演目の
        performingAutoplay(自動再生)がONならフェードし、OFF(操作者が
        任意のタイミングで▶を押して手動再生する演目)ならフェードしない。
        """
        item = self.items[self.current_idx if idx is None else idx]
        return DEFAULT_FADE_MS if bool(item.get("performingAutoplay")) else 0

    def fade_ms_for_current(self) -> int:
        """今のモードで次に曲を切り替えるときのフェード時間。上演中は
        _performing_fade_ms() に従い、それ以外(転換中・開始前)は常にフェードする。
        """
        if self.mode == "performing":
            return self._performing_fade_ms()
        return DEFAULT_FADE_MS

    def _play_queue(self, track_ids, playlist_id, autoplay=True, fade_ms=DEFAULT_FADE_MS, loop=True):
        self.bgm_queue = track_ids
        self.bgm_pos = self.playlist_positions.get(playlist_id, 0) % len(track_ids) if playlist_id else 0
        self.active_playlist_id = playlist_id
        self.active_playlist_loops = loop
        track_id = self.bgm_queue[self.bgm_pos]
        ok = self.player.play_by_id(track_id, fade_ms=fade_ms) if autoplay else self.player.load_by_id(track_id)
        if not ok:
            print(f"[警告] ライブラリに該当曲が見つかりません (id={track_id})")

    def _start_transition(self, target_idx: int, fade_ms=DEFAULT_FADE_MS) -> str:
        """転換中に入る。転換用プレイリストが割り当てられてなければ
        (または割り当て先が空プレイリストなら)無音のまま転換中になる
        (自動でperformingへスキップしない。次のNで上演開始する)。
        fade_ms は直前が上演中BGM(曲の途中)だった場合に呼び出し側から渡す
        (その演目のperformingAutoplayに従うかどうかは呼び出し側で決める)。
        """
        item = self.items[target_idx]
        bgm_ids = self._bgm_ids(item)
        if bgm_ids:
            loop = self._resolve_loop(item, "playlistId", "trackId")
            self._play_queue(bgm_ids, item.get("playlistId"), fade_ms=fade_ms, loop=loop)
        else:
            self.player.stop(fade_ms=fade_ms)
            self.bgm_queue = []
            self.bgm_pos = 0
            self.active_playlist_id = None
            self.active_playlist_loops = True
        self.target_idx = target_idx
        self.mode = "transition"
        suffix = "" if bgm_ids else " (無音)"
        return f"[PROGRAM] 転換中 -> {item['name']}{suffix}"

    def _start_performing(self, idx: int) -> str:
        item = self.items[idx]
        performing_ids = self._performing_bgm_ids(item)
        # performingAutoplay: 上演中BGMを転換直後に自動再生するか。
        # デフォルトはFalse(自動再生しない)。曲だけ頭出ししておき、
        # 操作者が任意のタイミングで▶を押すまで鳴らさない
        # (演目によっては開始と同時に鳴らしたくない場合があるため)。
        autoplay = bool(item.get("performingAutoplay"))
        if performing_ids:
            # 自動再生する演目は転換用BGMからフェードで切り替わり、
            # 手動再生(▶待ち)の演目はフェードしない(頭出しするだけで鳴らないため)。
            loop = self._resolve_loop(item, "performingPlaylistId", "performingTrackId")
            self._play_queue(performing_ids, item.get("performingPlaylistId"), autoplay=autoplay,
                              fade_ms=(DEFAULT_FADE_MS if autoplay else 0), loop=loop)
        else:
            # BGM無しの演目でも、転換用BGMが鳴っていればフェードアウトして止める。
            self.player.stop(fade_ms=DEFAULT_FADE_MS)
            self.bgm_queue = []
            self.bgm_pos = 0
            self.active_playlist_id = None
        self.current_idx = idx
        self.target_idx = None
        self.mode = "performing"
        if not performing_ids:
            suffix = ""
        elif autoplay:
            suffix = " (BGMあり・自動再生)"
        else:
            suffix = " (BGMあり・手動再生待ち)"
        return f"[PROGRAM] 上演開始: {item['name']}{suffix}"

    def advance(self) -> str:
        """次第を1つ進める。最初の呼び出しは演目1への転換から始まる
        (転換用プレイリストが割り当てられてなければ無音の転換になる。
        BGMの有無に関わらず、必ず転換中を経てからもう一度Nで上演開始する)。
        """
        if not self.started:
            snap = self._snapshot()
            self.started = True
            msg = self._start_transition(0)
            self._history.append(snap)
            return msg

        if self.mode == "transition":
            snap = self._snapshot()
            self._record_resume_position()
            msg = self._start_performing(self.target_idx)
            self._history.append(snap)
            return msg

        next_idx = self.current_idx + 1
        if next_idx >= len(self.items):
            return "[PROGRAM] 次第は最後の演目です"

        # ここに来るのは必ず上演中(曲の途中で次の演目に進む)。今の演目の
        # performingAutoplayに従ってフェードするか決める(次の転換用BGM側の
        # 設定ではなく、今フェードアウトする側=上演中BGMの設定で決まる)。
        fade_ms = self.fade_ms_for_current()
        snap = self._snapshot()
        self._record_resume_position()
        msg = self._start_transition(next_idx, fade_ms=fade_ms)
        self._history.append(snap)
        return msg

    def back(self) -> str:
        """直前の advance() を取り消して1つ前の状態に戻す。"""
        if not self._history:
            return "[PROGRAM] これ以上戻れません"
        # 上演中(曲の途中)から戻る場合、フェードするかは戻る前の演目の
        # performingAutoplayに従う(restore後は current_idx が変わってしまうため
        # 先に見ておく)。
        fade_ms_leaving_performing = self.fade_ms_for_current() if self.mode == "performing" else None
        self._restore(self._history.pop())
        if not self.started:
            self.player.stop()
            return "[PROGRAM] 開始前に戻りました"
        if self.bgm_queue:
            track_id = self.bgm_queue[self.bgm_pos]
            # 上演中に戻る場合は、進んだとき同様 performingAutoplay に従う
            # (自動再生しない設定の演目に戻って、勝手に鳴り出さないように)。
            autoplay = True
            if self.mode == "performing":
                autoplay = bool(self.items[self.current_idx].get("performingAutoplay"))
                fade_ms = self._performing_fade_ms()
            elif fade_ms_leaving_performing is not None:
                fade_ms = fade_ms_leaving_performing
            else:
                fade_ms = DEFAULT_FADE_MS
            if autoplay:
                self.player.play_by_id(track_id, fade_ms=fade_ms)
            else:
                self.player.load_by_id(track_id)
        else:
            self.player.stop()
        if self.mode == "transition":
            return f"[PROGRAM] 転換中に戻りました -> {self.items[self.target_idx]['name']}"
        return f"[PROGRAM] 上演中に戻りました: {self.items[self.current_idx]['name']}"

    def reset(self) -> str:
        """進行状態を開始前(未開始)に戻す。リハーサルの続き位置
        (playlist_positions・戻る履歴など)が本番に持ち越されないように、
        本番前に手動で呼び出す想定 (advance/backと違いキー割り当てはしない)。
        """
        self.player.stop()
        self.started = False
        self.current_idx = 0
        self.target_idx = None
        self.mode = "performing"
        self.bgm_queue = []
        self.bgm_pos = 0
        self.active_playlist_id = None
        self.active_playlist_loops = True
        self._history = []
        self.playlist_positions = {}
        return "[PROGRAM] 開始前の状態にリセットしました"

    def play_track_in_current_playlist(self, track_id: str) -> str:
        """再生中のプレイリスト内の指定曲へジャンプする
        (control.htmlのプレイリスト表示をクリックしたときに呼ばれる)。
        bgm_posもここで合わせておくことで、以降のループ/次への進行が
        ジャンプ後の位置を基準に続く。
        """
        if track_id not in self.bgm_queue:
            return "[PROGRAM] 今のプレイリストにその曲はありません"
        if not self.player.play_by_id(track_id, fade_ms=self.fade_ms_for_current()):
            return "[PROGRAM] 曲の再生に失敗しました"
        self.bgm_pos = self.bgm_queue.index(track_id)
        return "[PROGRAM] 曲を切り替えました"

    def tick(self):
        """再生中のBGM(転換用・上演中用どちらも)が最後まで再生し終わったら
        次の曲へ自動的に進める。末尾まで行ったら先頭に戻ってループする
        (プレイリストの「ループ」設定がOFFなら、最後まで行ったところで
        無音のまま止める)。メインループから毎フレーム呼び出す想定。

        pygame.mixer.music.get_busy() は一時停止中もFalseを返すため、
        player.paused (再生/一時停止ボタンや停止ジェスチャーで意図的に
        止めた状態) のときは「曲が自然に終わった」と誤判定しないよう、
        ここで進行しないようにする (でないと一時停止した瞬間に次の曲へ
        勝手に進んでしまう)。
        """
        if not self.bgm_queue:
            return
        if self.player.paused:
            return
        if pygame.mixer.music.get_busy():
            return
        fade_ms = self.fade_ms_for_current()
        if self.player.repeat:
            # リピートONのときはプレイリストを進めず同じ曲を繰り返す。
            # (BGMPlayer.tick()のリピート処理は、ここで先に次の曲を再生してしまうと
            #  get_busy()がTrueになって到達しない。control.htmlの🔁ボタンが
            #  プレイリスト再生中だけ無反応になるのを防ぐため、ここで面倒を見る)
            self.player.play_by_id(self.bgm_queue[self.bgm_pos], fade_ms=fade_ms)
            return
        if not self.active_playlist_loops and self.bgm_pos + 1 >= len(self.bgm_queue):
            self.player.stop()
            self.bgm_queue = []
            self.bgm_pos = 0
            self.active_playlist_id = None
            return
        self.bgm_pos = (self.bgm_pos + 1) % len(self.bgm_queue)
        self.player.play_by_id(self.bgm_queue[self.bgm_pos], fade_ms=fade_ms)

    def status(self) -> dict:
        if not self.started:
            return {
                "mode": "ready",
                "starts_with": "transition",
                "starts_with_bgm": bool(self._bgm_ids(self.items[0])),
                "next_item": self.items[0]["name"],
            }
        if self.mode == "transition":
            info = {
                "mode": "transition",
                "next_item": self.items[self.target_idx]["name"],
            }
            if self.bgm_queue:
                info["bgm"] = self.player.current_public()
            video = self._transition_video(self.items[self.target_idx])
            if video:
                info["video"] = video
                info["playing"] = self.player.playing
            return info
        info = {
            "mode": "performing",
            "current_item": self.items[self.current_idx]["name"],
        }
        if self.bgm_queue:
            info["bgm"] = self.player.current_public()
        video = self._performing_video(self.items[self.current_idx])
        if video:
            info["video"] = video
            # 動画側で「videoSyncPlayback」時に▶/⏸へ追従させるために必要
            # (bgmが無い演目でも動画だけは再生/一時停止を連動させたい場合がある)。
            info["playing"] = self.player.playing
        return info

    def _resolve_tracks(self, track_ids):
        """曲idのリストを、管理画面表示用の {id, title, author} のリストに変換する。
        毎回 player.library から引き直すことで、bgm-libraryで曲を追加/編集した
        直後でも(main.py再起動無しで)反映されるようにしている。
        """
        library_by_id = {t["id"]: t for t in self.player.library}
        result = []
        for tid in track_ids:
            t = library_by_id.get(tid)
            result.append({
                "id": tid,
                "title": t["displayTitle"] if t else "(見つかりません)",
                "author": t["author"] if t else "",
            })
        return result

    def active_playlist_ids(self):
        """今流してよい曲のidリストを返す (BGMPlayerの⏭/⏮制限に使う)。
        BGMが実際に再生中(転換中、または上演中BGM)ならそのプレイリスト、
        何も流れていなければ次に進めると流れる予定のプレイリストを対象にする。
        """
        if self.bgm_queue:
            return list(self.bgm_queue)
        upcoming_idx = 0 if not self.started else self.current_idx + 1
        if upcoming_idx < len(self.items):
            return self._bgm_ids(self.items[upcoming_idx])
        return []

    def admin_status(self) -> dict:
        """管理画面(control.html)向けの詳細ステータス。進行位置・演目一覧に加え、
        今再生できる(転換中・上演中BGM)/ 次に進めると再生される曲のプレイリストを明示する。
        """
        info = self.status()
        info["started"] = self.started
        info["can_go_back"] = bool(self._history)
        info["current_idx"] = self.current_idx
        info["target_idx"] = self.target_idx
        info["total_items"] = len(self.items)
        info["items"] = [item["name"] for item in self.items]

        if self.bgm_queue:
            info["current_playlist"] = self._resolve_tracks(self.bgm_queue)
            # self.bgm_posではなく、実際にplayerが今読み込んでいる曲を優先する。
            # 制限解除(🔓)中に⏭/⏮でこのプレイリスト外へ移動されるとbgm_posが
            # 追随せず、ハイライトが実際に鳴っている曲とズレてしまうため。
            current_track = self.player.current_track()
            if current_track and current_track["id"] in self.bgm_queue:
                info["current_playlist_index"] = self.bgm_queue.index(current_track["id"])
            else:
                info["current_playlist_index"] = self.bgm_pos

        if self.mode != "transition":
            upcoming_idx = 0 if not self.started else self.current_idx + 1
            if upcoming_idx < len(self.items):
                info["upcoming_playlist"] = self._resolve_tracks(self._bgm_ids(self.items[upcoming_idx]))
            else:
                info["upcoming_playlist"] = []
        return info


# ------------------------------------------------------------
# monitor1.html / monitor2.html の物理サイズキャリブレーション状態
# OBSのBrowser Source (別プロセスのブラウザ) はlocalStorageやBroadcastChannelを
# 制御画面と共有できないため、main.py側のHTTP APIを経由して状態をやり取りする。
# control.html から書き込み、monitor1/2.html はポーリングで読み取って反映する。
# ------------------------------------------------------------
class CalibValidationError(ValueError):
    """/calib に想定外の値が来たことを表す (400で返すためのマーカー)"""


class CalibStore:
    FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib_state.json")
    DEFAULT = {
        "1": {"heightCm": None, "yOffsetPx": 0},
        "2": {"heightCm": None, "yOffsetPx": 0},
    }

    def __init__(self):
        self.lock = threading.Lock()
        self.state = dict(self.DEFAULT)
        self._load()

    def _load(self):
        if os.path.isfile(self.FILE_PATH):
            try:
                with open(self.FILE_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for key in ("1", "2"):
                    if key in saved:
                        self.state[key].update(saved[key])
            except Exception:
                pass

    def _save(self):
        try:
            with open(self.FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception:
            pass

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    # 受け付ける値の範囲。ここを抜けた値を保存すると display-common.js の
    # pxPerCm() が NaN になり、配信画面の文字サイズが NaNpx になって文字が消える。
    # しかも calib_state.json に永続化されるので再起動しても直らない。
    LIMITS = {"heightCm": (5.0, 500.0), "yOffsetPx": (-10000.0, 10000.0)}

    @classmethod
    def _validate(cls, key, value):
        lo, hi = cls.LIMITS[key]
        # boolはintのサブクラスなので明示的に弾く
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalibValidationError(f"{key} は数値で指定してください")
        value = float(value)
        if not math.isfinite(value):
            raise CalibValidationError(f"{key} は有限の数値で指定してください")
        if not (lo <= value <= hi):
            raise CalibValidationError(f"{key} は {lo}〜{hi} の範囲で指定してください")
        return int(value) if key == "yOffsetPx" else value

    def update(self, monitor: str, patch: dict):
        with self.lock:
            if monitor not in self.state:
                return None
            for key in self.LIMITS:
                if key not in patch:
                    continue
                value = patch[key]
                # heightCm の null は「未キャリブレートに戻す」の意味で受け付ける
                if value is None and key == "heightCm":
                    self.state[monitor][key] = None
                    continue
                self.state[monitor][key] = self._validate(key, value)
            self._save()
            return json.loads(json.dumps(self.state))


# ------------------------------------------------------------
# 現在再生中の曲情報 & キャリブレーション状態を外部から取得/更新するための簡易HTTP API
# ------------------------------------------------------------
def make_now_playing_server(
    player: "BGMPlayer",
    port: int,
    program: "ProgramController" = None,
    command_queue: "queue.Queue[str]" = None,
) -> ThreadingHTTPServer:
    """GET /now-playing で現在状態、GET/POST /calib でキャリブレーション状態を扱うHTTPサーバーを作る

    program が指定されている場合、/now-playing は行事次第の進行状況
    (上演中なら演目名のみ、転換中なら次の演目名+再生中BGM) を返す。
    未指定の場合は従来通り player.status() を返す。

    command_queue を指定すると、control.html などの管理画面からの操作を
    受け付けられるようになる (POST /command, POST /program/advance)。
    実際の再生操作はメインループ側のスレッドでまとめて処理するため、
    ここではキューに積むだけにして pygame の呼び出しをスレッド間で
    競合させないようにしている。
    """

    calib_store = CalibStore()
    VALID_COMMANDS = {
        "PLAY_PAUSE", "STOP", "NEXT", "PREV", "VOL_UP", "VOL_DOWN",
        "REPEAT_TOGGLE", "RESTRICT_TOGGLE",
    }

    class Handler(BaseHTTPRequestHandler):
        MAX_BODY_BYTES = 64 * 1024

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # 参照系(GET)は他オリジンのOBS/自作ダッシュボードから読めるよう * のまま。
            # 更新系(POST)には付けない (許可すると外部ページから結果を読めてしまう)。
            if self.command == "GET":
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _origin_ok(self) -> bool:
            """ブラウザからのクロスサイトな更新リクエスト(CSRF)を弾く。

            Originヘッダが無い場合(curl等の非ブラウザ)は許可し、ある場合は
            自分自身のHostと一致するときだけ許可する。LAN内の別端末から
            http://<このPCのIP>:8787/control.html を開いた場合も Origin と Host は
            一致するので通る (127.0.0.1 固定にはしない)。
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            try:
                return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()
            except Exception:
                return False

        def _read_json_body(self):
            """POSTボディをJSONとして読む。

            問題があればエラーレスポンスを送信済みにしてNoneを返す
            (呼び出し側は None なら何もせず return するだけでよい)。
            """
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                # Content-Typeを必須にすると、他オリジンからのPOSTがプリフライトを
                # 強制され do_OPTIONS の同一オリジン判定を通らなくなる (CSRF対策)。
                self._send_json({"error": "Content-Type: application/json が必要です"}, status=415)
                return None
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_json({"error": "Content-Length が不正です"}, status=400)
                return None
            if length > self.MAX_BODY_BYTES:
                self._send_json({"error": "リクエストボディが大きすぎます"}, status=413)
                return None
            try:
                raw = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
                return None

        def do_OPTIONS(self):
            # プリフライトも同一オリジンのときだけ許可する。
            # ここで * を返すと、Content-Type必須にしたPOSTが他オリジンから通ってしまう。
            if not self._origin_ok():
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _guard(self, handler):
            """ハンドラ内で想定外の例外が出ても、接続をいきなり切らずに500を返す。

            socketserver が拾うのでプロセスは落ちないが、そのままだと接続が
            リセットされ、control.html には「未接続 (main.py が起動しているか
            確認してください)」と出てしまい原因の切り分けができない。
            """
            try:
                handler()
            except Exception as e:
                print(f"[警告] APIハンドラで想定外のエラー ({self.command} {self.path}): {e}")
                try:
                    self._send_json({"error": "internal error"}, status=500)
                except Exception:
                    pass  # 既にヘッダを送っている等。ここで諦める

        def do_GET(self):
            self._guard(self._do_GET)

        def do_POST(self):
            self._guard(self._do_POST)

        def _do_GET(self):
            if self.path == "/now-playing":
                self._send_json(program.status() if program else player.status())
            elif self.path == "/admin/status":
                self._send_json({
                    "player": player.status(),
                    "program": program.admin_status() if program else None,
                })
            elif self.path == "/calib":
                self._send_json(calib_store.get())
            elif self.path.startswith("/media/"):
                self._serve_media()
            else:
                self._serve_static()

        # links.html / control.html / monitor1-2.html / display.html などを
        # このAPIサーバー自身から配信する。これにより `python -m http.server`
        # を別途立てなくても http://127.0.0.1:<api-port>/control.html 等で開ける。
        # ディレクトリ探索を避けるため、プロジェクト直下のファイル名のみ許可する。
        STATIC_CONTENT_TYPES = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }
        STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
        STATIC_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")

        def _serve_static(self):
            url_path = self.path.split("?", 1)[0]
            if url_path == "/":
                url_path = "/links.html"
            filename = os.path.basename(url_path)
            ext = os.path.splitext(filename)[1].lower()
            file_path = os.path.join(self.STATIC_DIR, filename)

            # basenameだけでもディレクトリ探索は防げるが、Windowsの代替データ
            # ストリーム記法 (control.html:foo) 等を確実に弾くため、素朴な
            # ファイル名の形をしているものだけ通す。
            if not self.STATIC_FILENAME_RE.match(filename):
                self.send_response(404)
                self.end_headers()
                return

            if ext not in self.STATIC_CONTENT_TYPES or not os.path.isfile(file_path):
                self.send_response(404)
                self.end_headers()
                return

            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", self.STATIC_CONTENT_TYPES[ext])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # 上演中に流す動画の配信用 (/media/<filename>)。bgm-libraryの動画ライブラリで
        # 登録した動画ファイルは音源と同じ track_dir に保存されている。
        # HTML5の<video>はシーク/バッファリングにHTTP Rangeリクエストを使うため、
        # Rangeヘッダに対応していないとブラウザによっては再生自体ができない。
        MEDIA_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")
        MEDIA_CONTENT_TYPES = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
        }
        MEDIA_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
        MEDIA_CHUNK_SIZE = 256 * 1024

        def _serve_media(self):
            filename = os.path.basename(self.path.split("?", 1)[0])
            ext = os.path.splitext(filename)[1].lower()
            if not self.MEDIA_FILENAME_RE.match(filename) or ext not in self.MEDIA_CONTENT_TYPES:
                self.send_response(404)
                self.end_headers()
                return
            file_path = os.path.join(player.track_dir, filename)
            if not os.path.isfile(file_path):
                self.send_response(404)
                self.end_headers()
                return

            content_type = self.MEDIA_CONTENT_TYPES[ext]
            file_size = os.path.getsize(file_path)
            start, end = 0, file_size - 1
            is_partial = False

            range_header = self.headers.get("Range")
            if range_header:
                m = self.MEDIA_RANGE_RE.match(range_header)
                if not m:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                start_s, end_s = m.groups()
                start = int(start_s) if start_s else 0
                end = min(int(end_s), file_size - 1) if end_s else file_size - 1
                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                is_partial = True

            length = end - start + 1
            self.send_response(206 if is_partial else 200)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if is_partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()

            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(self.MEDIA_CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # ブラウザがシーク等で途中の接続を切っただけ。エラー扱いにしない

        # 操作ロック中は拒否するエンドポイント (/lock/toggle自体はここに含めない。
        # 含めるとロック中に解除できなくなってしまうため)。
        LOCKABLE_PATHS = {
            "/command", "/seek",
            "/program/advance", "/program/back", "/program/reset", "/program/play-track",
        }

        def _do_POST(self):
            # 更新系はまずCSRF(他サイトからの勝手なPOST)を弾く。
            if not self._origin_ok():
                self._send_json({"error": "cross-origin request rejected"}, status=403)
                return

            if self.path in self.LOCKABLE_PATHS and player.locked:
                self._send_json({"error": "操作ロック中です"}, status=423)
                return

            if self.path == "/lock/toggle":
                player.locked = not player.locked
                self._send_json({"locked": player.locked})
            elif self.path == "/calib":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    updated = calib_store.update(str(payload.get("monitor")), payload)
                except CalibValidationError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                if updated is None:
                    self._send_json({"error": "invalid monitor"}, status=400)
                else:
                    self._send_json(updated)
            elif self.path == "/command":
                if command_queue is None:
                    self._send_json({"error": "command queue unavailable"}, status=503)
                    return
                payload = self._read_json_body()
                if payload is None:
                    return
                command = payload.get("command")
                if command not in VALID_COMMANDS:
                    self._send_json({"error": "invalid command"}, status=400)
                else:
                    command_queue.put(command)
                    self._send_json({"queued": command}, status=202)
            elif self.path == "/seek":
                if command_queue is None:
                    self._send_json({"error": "command queue unavailable"}, status=503)
                    return
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    seconds = float(payload.get("seconds"))
                except (TypeError, ValueError):
                    self._send_json({"error": "seconds には数値を指定してください"}, status=400)
                    return
                # inf/nan は float() を通過してしまうので明示的に弾く
                if not math.isfinite(seconds):
                    self._send_json({"error": "seconds には有限の数値を指定してください"}, status=400)
                    return
                command_queue.put(f"SEEK:{seconds}")
                self._send_json({"queued": "SEEK"}, status=202)
            elif self.path == "/program/advance":
                if program is None or command_queue is None:
                    self._send_json({"error": "program not enabled (--program を指定してください)"}, status=400)
                else:
                    command_queue.put("PROGRAM_NEXT")
                    self._send_json({"queued": "PROGRAM_NEXT"}, status=202)
            elif self.path == "/program/back":
                if program is None or command_queue is None:
                    self._send_json({"error": "program not enabled (--program を指定してください)"}, status=400)
                else:
                    command_queue.put("PROGRAM_BACK")
                    self._send_json({"queued": "PROGRAM_BACK"}, status=202)
            elif self.path == "/program/reset":
                if program is None or command_queue is None:
                    self._send_json({"error": "program not enabled (--program を指定してください)"}, status=400)
                else:
                    command_queue.put("PROGRAM_RESET")
                    self._send_json({"queued": "PROGRAM_RESET"}, status=202)
            elif self.path == "/program/play-track":
                if program is None or command_queue is None:
                    self._send_json({"error": "program not enabled (--program を指定してください)"}, status=400)
                else:
                    payload = self._read_json_body()
                    if payload is None:
                        return
                    track_id = payload.get("trackId")
                    if not track_id or not isinstance(track_id, str):
                        self._send_json({"error": "trackId を指定してください"}, status=400)
                    else:
                        command_queue.put(f"PROGRAM_PLAY_TRACK:{track_id}")
                        self._send_json({"queued": "PROGRAM_PLAY_TRACK"}, status=202)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # コンソールを汚さないようアクセスログは出さない

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


# ------------------------------------------------------------
# ポート衝突からの自動復旧
# 前回のプロセスがCtrl+Cで終了しきれなかった場合など、指定ポートを既に
# 誰かがLISTENしていたら、そのプロセスを見つけて強制終了する。
# ------------------------------------------------------------
def _find_pids_using_port(port: int):
    pids = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    pids.add(parts[4])
        else:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=5
            ).stdout
            pids.update(p for p in out.split() if p)
    except Exception:
        pass
    return pids


def free_port(port: int) -> bool:
    """ポートを使用中のプロセスを強制終了する。何か殺せたらTrueを返す。"""
    pids = _find_pids_using_port(port)
    if not pids:
        return False
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=5)
            else:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
            print(f"[警告] ポート{port}を使用していたプロセス (PID {pid}) を終了しました")
        except Exception as e:
            print(f"[警告] PID {pid} を終了できませんでした: {e}")
    return True


# ------------------------------------------------------------
# bgm-library (Node.js) の自動起動
# main.py と一緒に `cd bgm-library && npm start` する手間を省くため、
# 子プロセスとして起動し、main.py終了時に一緒に終了させる。
# ------------------------------------------------------------
BGM_LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bgm-library")
# bgm-library/server.js 側もポート衝突時に自身で既存プロセスを終了して
# 再試行するので、ここでは何もせず子プロセスに任せる。


def start_bgm_library(tracks_dir: str, program_file: str | None):
    server_js = os.path.join(BGM_LIBRARY_DIR, "server.js")
    if not os.path.isfile(server_js):
        return None

    node_cmd = shutil.which("node")
    if not node_cmd:
        print("[警告] node が見つからないため bgm-library を自動起動できませんでした")
        print("       手動で `cd bgm-library && npm start` してください")
        return None

    if not os.path.isdir(os.path.join(BGM_LIBRARY_DIR, "node_modules")):
        print("[警告] bgm-library/node_modules がないため自動起動をスキップしました")
        print("       `cd bgm-library && npm install` を実行してください")
        return None

    # main.py の --dir / --program をそのまま bgm-library にも伝える。
    # bgm-library/.env の TRACKS_DIR / PROGRAM_FILE は env var が既にあると
    # 上書きしない (dotenvのデフォルト挙動) ため、ここで渡せば .env の手動
    # 編集を忘れて食い違う事故を防げる。
    env = os.environ.copy()
    env["TRACKS_DIR"] = os.path.abspath(tracks_dir)
    if program_file:
        env["PROGRAM_FILE"] = os.path.abspath(program_file)

    try:
        # POSIXでは自分のプロセスグループを持たせ、終了時にツリーごと止められるようにする
        # (Windowsは終了側で taskkill /T を使うのでここでは何もしない)
        popen_kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
        proc = subprocess.Popen(
            [node_cmd, "server.js"], cwd=BGM_LIBRARY_DIR, env=env, **popen_kwargs
        )
        # ポートは .env / PORT 環境変数で変わるうえ main.py 側からは分からないので、
        # ここでURLを断定しない (実際のURLは bgm-library 自身が起動時に出力する)。
        print("[bgm-library] 自動起動しました (URLは上の [bgm-library] のログを参照)")
        return proc
    except Exception as e:
        print(f"[警告] bgm-library を自動起動できませんでした: {e}")
        return None


def stop_bgm_library(proc):
    """bgm-library を、その子プロセスごと終了させる。

    proc.terminate() は node 本体しか止めないため、server.js が起動した
    yt-dlp / demucs (Python) が孤児として残り、GPU/CPUを掴んだまま走り続ける。
    """
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            print(f"[警告] bgm-library のプロセスツリーを終了できませんでした: {e}")
            proc.terminate()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def apply_command(player: "BGMPlayer", command: str, prefix: str = "", program: "ProgramController" = None) -> str:
    """ジェスチャー・音声どちらから来たコマンドも同じ処理にまとめる"""
    if command == "PLAY_PAUSE":
        # 次第使用中、今の演目にBGMが割り当てられていない(bgm_queueが空。
        # 例: 動画だけを割り当てた演目)ときは▶を押しても何もしない。
        # pygame.mixer.musicは stop() 後も直前にロードした曲を保持し続けるため、
        # ここでガードせずに toggle_play_pause() を呼ぶと、BGM無しの演目なのに
        # 前の演目の曲が「勝手に」再生されてしまう(見た目には無関係な曲や
        # プレイリストが紛れ込んだように見える)。
        if program is not None and not program.bgm_queue:
            return f"[{prefix}] PLAY_PAUSE (この演目にはBGMが割り当てられていません)"
        player.toggle_play_pause()
        state = "PLAY" if player.playing else "PAUSE"
        return f"[{prefix}] {state}"
    elif command == "STOP":
        player.stop()
        return f"[{prefix}] STOP"
    elif command == "NEXT":
        fade_ms = program.fade_ms_for_current() if program else DEFAULT_FADE_MS
        player.next_track(fade_ms=fade_ms)
        return f"[{prefix}] NEXT -> {player.current_name()}"
    elif command == "PREV":
        fade_ms = program.fade_ms_for_current() if program else DEFAULT_FADE_MS
        player.prev_track(fade_ms=fade_ms)
        return f"[{prefix}] PREV -> {player.current_name()}"
    elif command == "VOL_UP":
        player.volume_up()
        return f"[{prefix}] VOL {int(player.volume * 100)}%"
    elif command == "VOL_DOWN":
        player.volume_down()
        return f"[{prefix}] VOL {int(player.volume * 100)}%"
    elif command == "REPEAT_TOGGLE":
        player.toggle_repeat()
        return f"[{prefix}] REPEAT {'ON' if player.repeat else 'OFF'}"
    elif command == "RESTRICT_TOGGLE":
        player.toggle_restricted()
        return f"[{prefix}] RESTRICT {'ON' if player.restricted else 'OFF'}"
    return ""


# ------------------------------------------------------------
# メインループ
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ハンドサインで操作するBGMプレイヤー")
    parser.add_argument("--dir", type=str, default="./tracks", help="音源フォルダ (mp3/wav/ogg)")
    parser.add_argument("--hand-sign", action="store_true", help="カメラでハンドサイン認識を有効化する (デフォルトはオフ。control.html/音声/APIのみで操作する場合は指定不要)")
    parser.add_argument("--camera", type=int, default=0, help="カメラデバイス番号 (--hand-sign 指定時のみ使用)")
    parser.add_argument("--cooldown", type=float, default=1.0, help="同一ジェスチャーの連続発火防止秒数")
    parser.add_argument("--voice", action="store_true", help="音声コマンドも有効化する (要 vosk, pyaudio)")
    parser.add_argument("--api-port", type=int, default=8787, help="現在再生中の曲情報を返すHTTP APIのポート (0で無効化)")
    parser.add_argument("--program", type=str, default=None, help="行事の次第(演目リスト)を定義したJSONファイル。指定するとNキーで演目を進行できる")
    parser.add_argument("--no-library", action="store_true", help="bgm-library (Node.js) の自動起動をスキップする")
    parser.add_argument("--profile-startup", action="store_true", help="起動の各ステップの所要時間を計測して表示する")
    args = parser.parse_args()

    _t0 = time.monotonic()
    _last = [_t0]

    def _mark(label: str):
        if not args.profile_startup:
            return
        now = time.monotonic()
        print(f"[起動計測] {label}: {now - _last[0]:.2f}s (累計 {now - _t0:.2f}s)")
        _last[0] = now

    bgm_library_proc = None
    if not args.no_library:
        bgm_library_proc = start_bgm_library(args.dir, args.program)
    _mark("bgm-library 起動")

    try:
        player = BGMPlayer(args.dir)
    except RuntimeError as e:
        print(f"[エラー] {e}")
        stop_bgm_library(bgm_library_proc)
        sys.exit(1)
    _mark("トラック読み込み (BGMPlayer)")
    recognizer = GestureRecognizer(cooldown_sec=args.cooldown)

    program = None
    if args.program:
        try:
            program = ProgramController(args.program, player)
            print(f"[PROGRAM] {args.program} を読み込みました ({len(program.items)}演目)")
        except Exception as e:
            print(f"[警告] 次第ファイルを読み込めませんでした: {e}")
    _mark("次第ファイル読み込み")

    command_queue: "queue.Queue[str]" = queue.Queue()
    voice_controller = None
    if args.voice:
        try:
            voice_controller = VoiceController(command_queue)
            voice_controller.start()
            print("[音声] マイク待受を開始しました")
        except Exception as e:
            print(f"[警告] 音声認識を開始できませんでした: {e}")
            print("       pip install vosk pyaudio を確認してください")
        _mark("音声認識 (vosk) 初期化")

    api_server = None
    api_thread = None
    if args.api_port:
        try:
            api_server = make_now_playing_server(player, args.api_port, program, command_queue)
        except OSError as e:
            print(f"[警告] ポート{args.api_port}が使用中です。既存プロセスを終了して再試行します ({e})")
            if free_port(args.api_port):
                time.sleep(0.5)
                try:
                    api_server = make_now_playing_server(player, args.api_port, program, command_queue)
                except OSError as e2:
                    print(f"[エラー] 再試行しても起動できませんでした: {e2}")
            else:
                print(f"[エラー] ポート{args.api_port}を使用しているプロセスが見つかりませんでした")
        if api_server:
            api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
            api_thread.start()
            print(f"[API] http://127.0.0.1:{args.api_port}/now-playing で再生情報を取得できます")
            print(f"[管理画面] http://127.0.0.1:{args.api_port}/links.html からcontrol.html等を開けます")
    _mark("APIサーバー起動")

    status_text = "READY"

    def drain_command_queue():
        nonlocal status_text
        while not command_queue.empty():
            queued_command = command_queue.get_nowait()
            if queued_command == "PROGRAM_NEXT":
                if program:
                    status_text = program.advance()
            elif queued_command == "PROGRAM_BACK":
                if program:
                    status_text = program.back()
            elif queued_command == "PROGRAM_RESET":
                if program:
                    status_text = program.reset()
            elif queued_command.startswith("PROGRAM_PLAY_TRACK:"):
                if program:
                    track_id = queued_command.split(":", 1)[1]
                    status_text = program.play_track_in_current_playlist(track_id)
            elif queued_command.startswith("SEEK:"):
                try:
                    player.seek(float(queued_command.split(":", 1)[1]))
                except ValueError:
                    pass
            else:
                status_text = apply_command(player, queued_command, prefix="CMD", program=program)

    # ここから先で何が起きても、finally の後片付けは必ず走らせる。
    try:
        if args.hand_sign:
            global cv2, mp, np
            import cv2
            import mediapipe as mp
            import numpy as np
            _mark("カメラ/mediapipe 読み込み")

            mp_hands = mp.solutions.hands
            mp_drawing = mp.solutions.drawing_utils

            cap = cv2.VideoCapture(args.camera)
            if not cap.isOpened():
                print("[エラー] カメラを開けませんでした。--camera の番号を確認してください。")
                sys.exit(1)

            with mp_hands.Hands(
                model_complexity=0,
                min_detection_confidence=0.6,
                min_tracking_confidence=0.6,
                max_num_hands=1,
            ) as hands:
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break

                    try:
                        frame = cv2.flip(frame, 1)
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        result = hands.process(rgb)
                    except Exception as e:
                        print(f"[警告] カメラ画像の処理に失敗しました: {e}")
                        continue

                    # ジェスチャー判定〜再生操作。ここで例外が出るとループを抜けて
                    # プロセスごと落ち、後片付け(bgm-libraryの停止等)も走らないため、
                    # 1フレーム分をまとめて保護する。
                    try:
                        if result.multi_hand_landmarks and result.multi_handedness:
                            hand_landmarks = result.multi_hand_landmarks[0]

                            mp_drawing.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                            gesture = recognizer.recognize(hand_landmarks.landmark)
                            stable_gesture = recognizer.stabilize(gesture)

                            if recognizer.fire(stable_gesture):
                                status_text = apply_command(player, stable_gesture, prefix="HAND", program=program)
                        else:
                            recognizer.stabilize(None)
                            recognizer.fire(None)

                        drain_command_queue()
                        player.reload_library_if_changed()
                        if program:
                            program.reload_playlists_if_changed()
                            program.reload_videos_if_changed()
                            program.tick()
                            player.allowed_ids = list(program.active_playlist_ids())
                        player.tick()
                    except Exception as e:
                        # 本番中にここで想定外の例外(壊れたファイル等)が飛んでも
                        # プロセス全体を落とさず、警告だけ出して進行を続ける。
                        print(f"[エラー] 予期しない問題が発生しましたが、続行します: {e}")

                    # 画面にステータス表示 (日本語ファイル名も文字化けしないようPILで描画)
                    # 表示が作れなくても操作自体は続けられるべきなので、ここも保護する。
                    try:
                        header_h = 100 if program else 70
                        cv2.rectangle(frame, (0, 0), (frame.shape[1], header_h), (0, 0, 0), -1)
                        # 1行ずつ描画するとフレーム全体の色空間変換が行数分だけ走るので、
                        # 表示する行を組み立ててから draw_texts_ja で1回にまとめて描く。
                        overlay_lines = [
                            (f"Track: {player.current_name()}", (10, 8), 20, (255, 255, 255)),
                            (f"Status: {status_text}  Vol: {int(player.volume * 100)}%", (10, 38), 20, (0, 255, 0)),
                        ]
                        if program:
                            p_status = program.status()
                            if p_status["mode"] == "ready":
                                prefix = "転換から開始: " if p_status["starts_with"] == "transition" else "開始: "
                                program_line = f"(開始前) {prefix}{p_status['next_item']}"
                            elif p_status["mode"] == "performing":
                                program_line = f"上演中: {p_status['current_item']}"
                            else:
                                program_line = f"転換中 -> {p_status['next_item']}"
                            overlay_lines.append((f"次第: {program_line}", (10, 68), 20, (0, 255, 255)))
                        draw_texts_ja(frame, overlay_lines)
                    except Exception as e:
                        print(f"[警告] 画面表示の描画に失敗しました: {e}")

                    cv2.imshow("BGM Hand Sign Player (q to quit, n: next item, b: back)", frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        break
                    if program and key in (ord("n"), ord("b")):
                        # qキーでの終了だけは常に効くよう、進行操作もここで保護する。
                        try:
                            status_text = program.advance() if key == ord("n") else program.back()
                        except Exception as e:
                            print(f"[エラー] 次第の進行に失敗しましたが、続行します: {e}")

            cap.release()
            cv2.destroyAllWindows()
        else:
            print("[INFO] ハンドサイン認識はオフです (カメラ未使用)。control.html / 音声 / API で操作してください。")
            print("[INFO] Ctrl+C で終了します。")
            try:
                while True:
                    try:
                        drain_command_queue()
                        player.reload_library_if_changed()
                        if program:
                            program.reload_playlists_if_changed()
                            program.reload_videos_if_changed()
                            program.tick()
                            player.allowed_ids = list(program.active_playlist_ids())
                        player.tick()
                    except Exception as e:
                        # 本番中にここで想定外の例外(壊れたファイル等)が飛んでも
                        # プロセス全体を落とさず、警告だけ出して進行を続ける。
                        print(f"[エラー] 予期しない問題が発生しましたが、続行します: {e}")
                    time.sleep(0.05)
            except KeyboardInterrupt:
                pass

    finally:
        # 例外・sys.exit・Ctrl+C のいずれで抜けても必ず後片付けする。
        # 1つが失敗しても残りを続けたいので個別に保護する。
        for label, cleanup in (
            ("音声デバイス", pygame.mixer.quit),
            ("音声認識", voice_controller.stop if voice_controller else None),
            ("APIサーバー", api_server.shutdown if api_server else None),
            ("bgm-library", (lambda: stop_bgm_library(bgm_library_proc))),
        ):
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception as e:
                print(f"[警告] {label} の終了処理に失敗しました: {e}")


if __name__ == "__main__":
    main()
