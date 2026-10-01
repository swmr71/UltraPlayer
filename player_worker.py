"""
再生子プロセス (player_worker.py)
==================================
pygame.mixer.music の呼び出しだけを担う、main.py とは別プロセス。

なぜ分離するか: SDL_mixer(pygame.mixerの内部実装)はネイティブ層で動作するため、
壊れた音源ファイルや音声デバイスの瞬断などが原因でクラッシュ/ハングした場合、
Pythonの try/except では捕まえられずプロセスごと落ちることがある。この処理を
別プロセスに閉じ込めておけば、そちらが落ちてもハンドサイン認識・HTTP制御サーバー・
映像同期などmain.py側の機能は道連れにならず、main.py側のウォッチドッグが
検知して再生プロセスだけを再起動できる。

プロトコル: 標準入力から1行1コマンド(JSON)を読み、標準出力へ1行1応答(JSON)を返す。
    リクエスト:  {"id": 1, "cmd": "load", "path": "..."}
    成功応答:    {"id": 1, "ok": true, ...追加のフィールド}
    失敗応答:    {"id": 1, "ok": false, "error": "..."}
main.py側からは常に1リクエスト1応答(次を送る前に応答を待つ)なので、
このプロセス側は単純な逐次処理でよい。
"""

import json
import os
import sys

# pygameはimport時に "Hello from the pygame community..." を標準出力に書く。
# ここは標準出力を1行1JSON応答のIPCチャンネルとして使っているため、
# importより前に無効化しておかないと応答がJSONとして解釈できなくなる。
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


def _respond(req_id, ok, **fields):
    try:
        sys.stdout.write(json.dumps({"id": req_id, "ok": ok, **fields}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass  # 応答すら送れない(パイプが壊れている等)なら、じきに親側がタイムアウトで気づく


def main():
    try:
        import pygame
    except Exception as e:
        print(f"[player_worker] pygameをimportできませんでした: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    mixer_ready = False

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError:
            continue  # 壊れた行は無視する(同期は次の正常な行で回復する)
        req_id = req.get("id")
        cmd = req.get("cmd")

        try:
            if cmd == "init":
                pygame.mixer.init()
                mixer_ready = True
                _respond(req_id, True)
            elif cmd == "ping":
                _respond(req_id, True)
            elif cmd == "quit":
                _respond(req_id, True)
                if mixer_ready:
                    pygame.mixer.quit()
                return
            elif not mixer_ready:
                _respond(req_id, False, error="mixer未初期化です (initを先に呼んでください)")
            elif cmd == "load":
                pygame.mixer.music.load(req["path"])
                _respond(req_id, True)
            elif cmd == "play":
                pygame.mixer.music.play(fade_ms=int(req.get("fade_ms", 0)))
                _respond(req_id, True)
            elif cmd == "pause":
                pygame.mixer.music.pause()
                _respond(req_id, True)
            elif cmd == "unpause":
                pygame.mixer.music.unpause()
                _respond(req_id, True)
            elif cmd == "stop":
                pygame.mixer.music.stop()
                _respond(req_id, True)
            elif cmd == "fadeout":
                pygame.mixer.music.fadeout(int(req.get("fade_ms", 0)))
                _respond(req_id, True)
            elif cmd == "set_pos":
                pygame.mixer.music.set_pos(float(req["seconds"]))
                _respond(req_id, True)
            elif cmd == "set_volume":
                pygame.mixer.music.set_volume(float(req["volume"]))
                _respond(req_id, True)
            elif cmd == "get_busy":
                _respond(req_id, True, busy=bool(pygame.mixer.music.get_busy()))
            else:
                _respond(req_id, False, error=f"不明なコマンドです: {cmd}")
        except Exception as e:
            _respond(req_id, False, error=str(e))


if __name__ == "__main__":
    main()
