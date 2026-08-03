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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import mediapipe as mp
import pygame


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

    def _finger_states(self, landmarks, handedness_label: str):
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

    def recognize(self, landmarks, handedness_label: str):
        thumb, index, middle, ring, pinky = self._finger_states(landmarks, handedness_label)
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
        now = time.time()
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
        import json

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


# ------------------------------------------------------------
# BGMプレイヤー本体
# ------------------------------------------------------------
class BGMPlayer:
    def __init__(self, track_dir: str):
        pygame.mixer.init()
        self.tracks = sorted(
            glob.glob(os.path.join(track_dir, "*.mp3"))
            + glob.glob(os.path.join(track_dir, "*.wav"))
            + glob.glob(os.path.join(track_dir, "*.ogg"))
        )
        if not self.tracks:
            print(f"[警告] {track_dir} に音源ファイルが見つかりません (mp3/wav/ogg)")
        self.index = 0
        self.volume = 0.5
        self.playing = False
        pygame.mixer.music.set_volume(self.volume)
        if self.tracks:
            self._load_current()

    def _load_current(self):
        pygame.mixer.music.load(self.tracks[self.index])

    def current_name(self):
        if not self.tracks:
            return "(no track)"
        return os.path.basename(self.tracks[self.index])

    def status(self) -> dict:
        """現在の再生状態をJSON化しやすい辞書で返す (外部API公開用)"""
        return {
            "track": self.current_name(),
            "playing": self.playing,
            "volume": round(self.volume, 2),
            "index": self.index,
            "total_tracks": len(self.tracks),
        }

    def toggle_play_pause(self):
        if not self.tracks:
            return
        if self.playing:
            pygame.mixer.music.pause()
            self.playing = False
        else:
            if pygame.mixer.music.get_pos() == -1:
                pygame.mixer.music.play()
            else:
                pygame.mixer.music.unpause()
            self.playing = True

    def stop(self):
        pygame.mixer.music.stop()
        self.playing = False

    def next_track(self):
        if not self.tracks:
            return
        self.index = (self.index + 1) % len(self.tracks)
        self._load_current()
        pygame.mixer.music.play()
        self.playing = True

    def prev_track(self):
        if not self.tracks:
            return
        self.index = (self.index - 1) % len(self.tracks)
        self._load_current()
        pygame.mixer.music.play()
        self.playing = True

    def volume_up(self):
        self.volume = min(1.0, self.volume + 0.1)
        pygame.mixer.music.set_volume(self.volume)

    def volume_down(self):
        self.volume = max(0.0, self.volume - 0.1)
        pygame.mixer.music.set_volume(self.volume)


# ------------------------------------------------------------
# 現在再生中の曲情報を外部から取得するための簡易HTTP API
# ------------------------------------------------------------
def make_now_playing_server(player: "BGMPlayer", port: int) -> ThreadingHTTPServer:
    """GET /now-playing で player.status() のJSONを返すHTTPサーバーを作る"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/now-playing"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(player.status(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # コンソールを汚さないようアクセスログは出さない

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def apply_command(player: "BGMPlayer", command: str, prefix: str = "") -> str:
    """ジェスチャー・音声どちらから来たコマンドも同じ処理にまとめる"""
    if command == "PLAY_PAUSE":
        player.toggle_play_pause()
        state = "PLAY" if player.playing else "PAUSE"
        return f"[{prefix}] {state}"
    elif command == "STOP":
        player.stop()
        return f"[{prefix}] STOP"
    elif command == "NEXT":
        player.next_track()
        return f"[{prefix}] NEXT -> {player.current_name()}"
    elif command == "PREV":
        player.prev_track()
        return f"[{prefix}] PREV -> {player.current_name()}"
    elif command == "VOL_UP":
        player.volume_up()
        return f"[{prefix}] VOL {int(player.volume * 100)}%"
    elif command == "VOL_DOWN":
        player.volume_down()
        return f"[{prefix}] VOL {int(player.volume * 100)}%"
    return ""


# ------------------------------------------------------------
# メインループ
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ハンドサインで操作するBGMプレイヤー")
    parser.add_argument("--dir", type=str, default="./tracks", help="音源フォルダ (mp3/wav/ogg)")
    parser.add_argument("--camera", type=int, default=0, help="カメラデバイス番号")
    parser.add_argument("--cooldown", type=float, default=1.0, help="同一ジェスチャーの連続発火防止秒数")
    parser.add_argument("--voice", action="store_true", help="音声コマンドも有効化する (要 vosk, pyaudio)")
    parser.add_argument("--api-port", type=int, default=8787, help="現在再生中の曲情報を返すHTTP APIのポート (0で無効化)")
    args = parser.parse_args()

    player = BGMPlayer(args.dir)
    recognizer = GestureRecognizer(cooldown_sec=args.cooldown)

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

    api_server = None
    api_thread = None
    if args.api_port:
        try:
            api_server = make_now_playing_server(player, args.api_port)
            api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
            api_thread.start()
            print(f"[API] http://127.0.0.1:{args.api_port}/now-playing で再生情報を取得できます")
        except OSError as e:
            print(f"[警告] APIサーバーを起動できませんでした: {e}")

    mp_hands = mp.solutions.hands
    mp_drawing = mp.solutions.drawing_utils

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print("[エラー] カメラを開けませんでした。--camera の番号を確認してください。")
        sys.exit(1)

    status_text = "READY"

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

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)

            if result.multi_hand_landmarks and result.multi_handedness:
                hand_landmarks = result.multi_hand_landmarks[0]
                handedness_label = result.multi_handedness[0].classification[0].label

                mp_drawing.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                gesture = recognizer.recognize(hand_landmarks.landmark, handedness_label)
                stable_gesture = recognizer.stabilize(gesture)

                if recognizer.fire(stable_gesture):
                    status_text = apply_command(player, stable_gesture, prefix="HAND")
            else:
                recognizer.stabilize(None)
                recognizer.fire(None)

            # 音声コマンドキューを消化 (ノンブロッキング)
            while not command_queue.empty():
                voice_command = command_queue.get_nowait()
                status_text = apply_command(player, voice_command, prefix="VOICE")

            # 画面にステータス表示
            cv2.rectangle(frame, (0, 0), (frame.shape[1], 70), (0, 0, 0), -1)
            cv2.putText(frame, f"Track: {player.current_name()}", (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(frame, f"Status: {status_text}  Vol: {int(player.volume * 100)}%", (10, 55),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            cv2.imshow("BGM Hand Sign Player (qで終了)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cap.release()
    cv2.destroyAllWindows()
    pygame.mixer.quit()
    if voice_controller:
        voice_controller.stop()
    if api_server:
        api_server.shutdown()


if __name__ == "__main__":
    main()
