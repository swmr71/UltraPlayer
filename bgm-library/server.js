"use strict";
/**
 * BGMライブラリ管理サーバー
 * ----------------------------------------------------------------
 * - BGMファイルをアップロードすると UUID をファイル名にして保存し、
 *   曲名/表示用曲名(伏字対応)/作者/「当方でBGM化した二次利用」注記を
 *   tracks.json (main.py --dir と同じフォルダ) に記録する。
 * - last.fm の track.search で曲名から作者候補を検索できる
 *   (LASTFM_API_KEY 未設定なら無効)。魔王魂などのフリーBGM素材は
 *   商用配信されていないため基本的にヒットしない点に注意。
 * - 行事の次第 (program.json, main.py --program と同じファイル) を
 *   このUIから編集する。転換に使うBGMは、名前付きの共通プレイリスト
 *   (playlists.json) を作成して各演目に付け外しで割り当てる方式。
 * - YouTubeのURLを渡すと yt-dlp + ffmpeg で音声を抽出してライブラリに登録できる
 *   (要 yt-dlp / ffmpeg のインストール)。ダウンロードした音源の著作権・利用規約は
 *   利用者側の責任で確認すること。
 * - 登録済みの曲から「ボーカル除去」でDemucs (要 pip install demucs、main.py と
 *   同じvenv) を実行し、インストゥルメンタル版を新しい曲として追加できる。
 *   モデルは既定で高品質な htdemucs_ft (DEMUCS_MODEL で変更可)。実行デバイスは
 *   起動時に自動検出 (CUDA > Apple Silicon MPS > CPU の優先順、DEMUCS_DEVICE で
 *   固定も可)。GPUが無い環境ではCPU実行になり、曲の長さと同程度〜数倍の処理
 *   時間がかかる。POST /api/tracks/:id/remove-vocals はジョブIDを即返し、
 *   GET /api/jobs/:id をポーリングすると進捗(%)を取得できる。
 */

require("dotenv").config();
const fs = require("fs");
const os = require("os");
const path = require("path");
const express = require("express");
const multer = require("multer");
const { v4: uuidv4 } = require("uuid");
const { execFile, execFileSync, spawn } = require("child_process");
const { promisify } = require("util");
const execFileAsync = promisify(execFile);

const PORT = Number(process.env.PORT || 4000);
// 既定は 0.0.0.0 (LAN内の他端末から管理画面を開ける)。認証は無いので、
// ローカル限定にしたい場合だけ .env で HOST=127.0.0.1 を指定する。
const HOST = process.env.HOST || "0.0.0.0";
const TRACKS_DIR = path.resolve(__dirname, process.env.TRACKS_DIR || "../tracks");
const PROGRAM_FILE = path.resolve(__dirname, process.env.PROGRAM_FILE || "../program.json");
const LIBRARY_FILE = path.join(TRACKS_DIR, "tracks.json");
const PLAYLISTS_FILE = path.join(TRACKS_DIR, "playlists.json");
const VIDEOS_FILE = path.join(TRACKS_DIR, "videos.json");

const LASTFM_API_KEY = process.env.LASTFM_API_KEY || "";
const YT_DLP_CMD = process.env.YT_DLP_CMD || "yt-dlp";
const FFMPEG_CMD = process.env.FFMPEG_CMD || "ffmpeg";
const FFPROBE_CMD = process.env.FFPROBE_CMD || "ffprobe";

// ボーカル除去(Demucs)は main.py と同じvenvのPythonから `python -m demucs` で叩く。
// PYTHON_CMD を明示指定しなければ、隣のvenvを自動検出する。
function resolvePythonCmd() {
  if (process.env.PYTHON_CMD) return process.env.PYTHON_CMD;
  const winVenv = path.resolve(__dirname, "../venv/Scripts/python.exe");
  const posixVenv = path.resolve(__dirname, "../venv/bin/python");
  if (fs.existsSync(winVenv)) return winVenv;
  if (fs.existsSync(posixVenv)) return posixVenv;
  return process.platform === "win32" ? "python" : "python3";
}
const PYTHON_CMD = resolvePythonCmd();

// ボーカル除去に使うDemucsのモデル名。既定は高品質な htdemucs_ft (4モデルの
// アンサンブルで既定の htdemucs より高精度だが約4倍遅い)。処理時間を優先する
// 場合は .env で DEMUCS_MODEL=htdemucs に戻せる。
const DEMUCS_MODEL = process.env.DEMUCS_MODEL || "htdemucs_ft";

// 実行デバイス。未指定 (auto) なら起動時に CUDA > Apple Silicon(MPS) > CPU の
// 優先順で自動検出する。.env の DEMUCS_DEVICE で "cuda"/"mps"/"cpu" に固定も可。
const DEMUCS_DEVICE_OVERRIDE = process.env.DEMUCS_DEVICE || "";
let demucsDevice = DEMUCS_DEVICE_OVERRIDE || "cpu";

async function detectDemucsDevice() {
  if (DEMUCS_DEVICE_OVERRIDE) return DEMUCS_DEVICE_OVERRIDE;
  try {
    const { stdout } = await execFileAsync(
      PYTHON_CMD,
      [
        "-c",
        "import torch,json;" +
          "mps=bool(getattr(torch.backends,'mps',None) and torch.backends.mps.is_available());" +
          "print(json.dumps({'cuda': torch.cuda.is_available(), 'mps': mps}))",
      ],
      { timeout: 15000 }
    );
    const info = JSON.parse(stdout.trim());
    if (info.cuda) return "cuda";
    if (info.mps) return "mps";
  } catch {
    // torch未インストール等。CPUにフォールバック
  }
  return "cpu";
}

fs.mkdirSync(TRACKS_DIR, { recursive: true });

// ------------------------------------------------------------
// JSON台帳 (tracks.json / program.json / playlists.json) の書き込み
//
// - writeJsonAtomic: 一時ファイルに書いてから rename する。writeFileSync の
//   途中でプロセスが落ちるとJSONが切れ、main.py 側が「tracks.jsonが読めない」
//   → フォルダ直下のmp3を素朴に列挙するモードに黙って降格してしまう
//   (曲名・伏字タイトルが全部UUIDになる)。
// - withLock: load→編集→save を直列化する。ボーカル除去の完了コールバックや
//   backfillDurations は非同期に走るため、その間のアップロード/メタ編集と
//   衝突すると片方の更新が丸ごと消える。
// ------------------------------------------------------------
function writeJsonAtomic(file, data) {
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf-8");
  fs.renameSync(tmp, file);
}

let writeChain = Promise.resolve();
function withLock(fn) {
  const run = writeChain.then(fn, fn);
  // 直前の処理が失敗しても後続を止めない
  writeChain = run.then(
    () => {},
    () => {}
  );
  return run;
}

// ------------------------------------------------------------
// ライブラリ (tracks.json) の読み書き
// ------------------------------------------------------------
function loadLibrary() {
  if (!fs.existsSync(LIBRARY_FILE)) return [];
  try {
    return JSON.parse(fs.readFileSync(LIBRARY_FILE, "utf-8"));
  } catch {
    return [];
  }
}

function saveLibrary(list) {
  writeJsonAtomic(LIBRARY_FILE, list);
}

// ------------------------------------------------------------
// 曲の長さ (秒)。ffprobe (ffmpeg付属) で取得し tracks.json に durationSec として
// キャッシュする。プレイリストの合計時間表示に使う。ffprobeが無い環境では
// null のままになり、その曲は合計時間の計算から除外される。
// ------------------------------------------------------------
async function probeDurationSec(filePath) {
  try {
    const { stdout } = await execFileAsync(
      FFPROBE_CMD,
      ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filePath],
      { timeout: 15000 }
    );
    const sec = parseFloat(stdout.trim());
    return Number.isFinite(sec) ? sec : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------
// 行事の次第 (program.json) の読み書き
// ------------------------------------------------------------
function loadProgram() {
  if (!fs.existsSync(PROGRAM_FILE)) return [];
  try {
    return JSON.parse(fs.readFileSync(PROGRAM_FILE, "utf-8"));
  } catch {
    return [];
  }
}

function saveProgram(items) {
  writeJsonAtomic(PROGRAM_FILE, items);
}

// ------------------------------------------------------------
// 転換用プレイリスト (playlists.json) の読み書き
// 名前付きの共通プレイリストを作成し、各演目の転換に付け外しで割り当てる。
// ------------------------------------------------------------
function loadPlaylists() {
  if (!fs.existsSync(PLAYLISTS_FILE)) return [];
  try {
    return JSON.parse(fs.readFileSync(PLAYLISTS_FILE, "utf-8"));
  } catch {
    return [];
  }
}

function savePlaylists(list) {
  writeJsonAtomic(PLAYLISTS_FILE, list);
}

// ------------------------------------------------------------
// 動画ライブラリ (videos.json) の読み書き
// 上演中に流す動画を演目に割り当てるための素材一覧。tracks.jsonと同じ
// TRACKS_DIR に保存し、/tracks-file で(音源と同様に)配信する。
// ------------------------------------------------------------
function loadVideos() {
  if (!fs.existsSync(VIDEOS_FILE)) return [];
  try {
    return JSON.parse(fs.readFileSync(VIDEOS_FILE, "utf-8"));
  } catch {
    return [];
  }
}

function saveVideos(list) {
  writeJsonAtomic(VIDEOS_FILE, list);
}

// ------------------------------------------------------------
// last.fm track.search (作者検索補助・任意)
// APIキーは https://www.lastfm.jp/api/account/create で無料取得できる
// (Premium等の契約は不要)。
// ------------------------------------------------------------
async function searchLastfm(query) {
  if (!LASTFM_API_KEY) return [];
  const url =
    "https://ws.audioscrobbler.com/2.0/?method=track.search" +
    `&track=${encodeURIComponent(query)}&api_key=${LASTFM_API_KEY}&format=json&limit=5`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`last.fm検索失敗: ${res.status}`);
  const data = await res.json();
  const matches = data.results?.trackmatches?.track || [];
  const list = Array.isArray(matches) ? matches : [matches];
  return list.map((t) => ({ title: t.name, artist: t.artist, album: "" }));
}

// ------------------------------------------------------------
// YouTubeからのダウンロード (yt-dlp + ffmpeg が必要)
// ------------------------------------------------------------
function isYoutubeUrl(raw) {
  try {
    const u = new URL(raw);
    return /(^|\.)youtube\.com$/.test(u.hostname) || u.hostname === "youtu.be";
  } catch {
    return false;
  }
}

async function fetchYoutubeInfo(url) {
  const { stdout } = await execFileAsync(
    YT_DLP_CMD,
    ["--dump-json", "--no-download", "--no-playlist", url],
    { maxBuffer: 10 * 1024 * 1024, timeout: 20000 }
  );
  const info = JSON.parse(stdout);
  return { title: info.title || "", author: info.uploader || info.channel || "", duration: info.duration || null };
}

async function downloadYoutubeAudio(url, id) {
  await execFileAsync(
    YT_DLP_CMD,
    [
      "-x", "--audio-format", "mp3", "--audio-quality", "0",
      "--no-playlist",
      "-o", `${id}.%(ext)s`,
      url,
    ],
    { cwd: TRACKS_DIR, maxBuffer: 10 * 1024 * 1024, timeout: 5 * 60 * 1000 }
  );
  const filename = `${id}.mp3`;
  if (!fs.existsSync(path.join(TRACKS_DIR, filename))) {
    throw new Error("ダウンロードは完了しましたが、mp3ファイルが見つかりません");
  }
  return filename;
}

// 音声のみの downloadYoutubeAudio と違い、映像+音声をmp4にまとめてダウンロードする。
async function downloadYoutubeVideo(url, id) {
  await execFileAsync(
    YT_DLP_CMD,
    [
      "-f", "bv*+ba/b",
      "--merge-output-format", "mp4",
      "--no-playlist",
      "-o", `${id}.%(ext)s`,
      url,
    ],
    { cwd: TRACKS_DIR, maxBuffer: 10 * 1024 * 1024, timeout: 20 * 60 * 1000 }
  );
  const filename = `${id}.mp4`;
  if (!fs.existsSync(path.join(TRACKS_DIR, filename))) {
    throw new Error("ダウンロードは完了しましたが、mp4ファイルが見つかりません");
  }
  return filename;
}

// ------------------------------------------------------------
// 非同期ジョブ管理 (ボーカル除去は数十秒〜数分かかるため、
// レスポンスを待たせずジョブIDを返し、進捗をポーリングで取得できるようにする)
// ------------------------------------------------------------
const jobs = new Map(); // jobId -> {status: "running"|"done"|"error", progress, trackId?, error?, result?}

// 完了したジョブを保持しておく時間。画面のポーリングが結果を拾えるだけの猶予を
// 持たせつつ、いつまでもMapに残り続けない(=リークしない)ようにする。
const JOB_RETENTION_MS = 60 * 60 * 1000;

function createJob(trackId) {
  const id = uuidv4();
  jobs.set(id, { status: "running", progress: 0, trackId });
  return id;
}

function finishJob(jobId, data) {
  jobs.set(jobId, { ...data, finishedAt: Date.now() });
  const now = Date.now();
  for (const [id, job] of jobs) {
    if (job.status !== "running" && job.finishedAt && now - job.finishedAt > JOB_RETENTION_MS) {
      jobs.delete(id);
    }
  }
}

// 中断された(プロセスが強制終了された等)ボーカル除去の作業ディレクトリを掃除する。
// removeVocals() の finally は正常系でしか動かないため、起動時にも消しておく。
// Demucsの中間ファイル(分離済みwav)は元音源より大きく、放置するとディスクを圧迫する。
function cleanupDemucsWorkDirs() {
  let removed = 0;
  try {
    for (const name of fs.readdirSync(TRACKS_DIR)) {
      if (!name.startsWith(".demucs-")) continue;
      try {
        fs.rmSync(path.join(TRACKS_DIR, name), { recursive: true, force: true });
        removed += 1;
      } catch {}
    }
  } catch {}
  if (removed) {
    console.log(`[bgm-library] 中断されたボーカル除去の作業ディレクトリを${removed}件削除しました`);
  }
}

// ------------------------------------------------------------
// AIボーカル除去 (Demucs, デフォルト htdemucs_ft モデル)
// 実行デバイスは起動時に自動検出したもの (demucsDevice: cuda/mps/cpu) を使う。
// CPU実行時はマルチプロセス並列化(-j)も試したが、ワーカーごとにモデルを
// 再ロードするオーバーヘッドが上回り単一トラックではかえって遅くなったため
// 採用していない。代わりにDemucsの進捗バー(標準エラー出力)を正規表現でパース
// してジョブの進捗(0-99%)に反映している。
//
// htdemucs_ft は4モデルのアンサンブル(bag of models)で、Demucsはモデルごとに
// 0%→100%の進捗バーを別々に出す。そのため単純に最後に見つけたパーセントだけを
// 使うと「100%まで行ってはまた0%に戻る」を4回繰り返すように見える(連打などの
// せいではない)。ここでは大きく数値が下がったら次のモデルに進んだとみなして
// パス数で割り、常に単調増加する全体進捗に変換している。
//
// タイムアウトは合計時間の固定上限ではなく「無操作(標準エラー出力が一定時間
// 止まった)」方式にしている。長い曲やCPUが遅い環境でも、進捗が出続けている
// 限り処理を継続でき、本当にハングした場合だけ中断する。
// ------------------------------------------------------------
const DEMUCS_INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000;

// モデルごとの内部パス数 (htdemucs_ft は4モデルのbag、それ以外は単一モデル)
const DEMUCS_MODEL_PASSES = { htdemucs_ft: 4 };
function demucsPassCount(model) {
  return DEMUCS_MODEL_PASSES[model] || 1;
}

function runDemucs(sourcePath, workDir, onProgress) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      PYTHON_CMD,
      [
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        DEMUCS_MODEL,
        "--device",
        demucsDevice,
        "-o",
        workDir,
        sourcePath,
      ],
      { windowsHide: true }
    );

    let stderrTail = "";
    let timer;
    const resetTimer = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        child.kill();
        reject(new Error(`ボーカル除去が${DEMUCS_INACTIVITY_TIMEOUT_MS / 60000}分間応答がないため中断しました`));
      }, DEMUCS_INACTIVITY_TIMEOUT_MS);
    };
    resetTimer();

    const totalPasses = demucsPassCount(DEMUCS_MODEL);
    let passIndex = 0;
    let lastPercent = 0;

    child.stderr.on("data", (chunk) => {
      resetTimer();
      stderrTail += chunk.toString();
      if (stderrTail.length > 4000) stderrTail = stderrTail.slice(-4000);
      const matches = [...stderrTail.matchAll(/(\d+(?:\.\d+)?)%\|/g)];
      if (matches.length) {
        const percent = parseFloat(matches[matches.length - 1][1]);
        // 大きく数値が下がったら次のモデルのパスに入ったとみなす
        if (percent < lastPercent - 20 && passIndex < totalPasses - 1) {
          passIndex += 1;
        }
        lastPercent = percent;
        const overall = (passIndex * 100 + percent) / totalPasses;
        onProgress(Math.min(99, Math.round(overall)));
      }
    });

    child.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`demucsがエラー終了しました (code ${code}): ${stderrTail.slice(-500)}`));
    });
  });
}

async function removeVocals(sourcePath, newId, onProgress) {
  const workDir = path.join(TRACKS_DIR, `.demucs-${newId}`);
  fs.mkdirSync(workDir, { recursive: true });
  try {
    await runDemucs(sourcePath, workDir, onProgress);
    onProgress(99);

    const baseName = path.parse(sourcePath).name;
    const wavPath = path.join(workDir, DEMUCS_MODEL, baseName, "no_vocals.wav");
    if (!fs.existsSync(wavPath)) {
      throw new Error("ボーカル除去は完了しましたが、出力ファイルが見つかりません");
    }

    const filename = `${newId}.mp3`;
    await execFileAsync(
      FFMPEG_CMD,
      ["-y", "-i", wavPath, "-codec:a", "libmp3lame", "-qscale:a", "2", path.join(TRACKS_DIR, filename)],
      { maxBuffer: 20 * 1024 * 1024, timeout: 60 * 1000 }
    );
    return filename;
  } finally {
    fs.rm(workDir, { recursive: true, force: true }, () => {});
  }
}

// ------------------------------------------------------------
// Express アプリ
// ------------------------------------------------------------
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));
app.use("/tracks-file", express.static(TRACKS_DIR)); // 試聴用に音源を配信

// express 4 は async ハンドラが返すPromiseの reject をキャッチしない。
// Node 18以降は未処理のPromise拒否がプロセス終了になるため、保存に失敗した程度で
// bgm-library ごと落ちてしまう (同期ハンドラなら express が拾って500を返していた)。
// 全ての async ハンドラをこれで包み、下のエラーミドルウェアに渡す。
function asyncRoute(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

const ALLOWED_EXT = new Set([".mp3", ".wav", ".ogg"]);

const upload = multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => cb(null, TRACKS_DIR),
    filename: (req, file, cb) => {
      const ext = path.extname(file.originalname).toLowerCase();
      cb(null, `${uuidv4()}${ALLOWED_EXT.has(ext) ? ext : ".mp3"}`);
    },
  }),
  fileFilter: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    cb(null, ALLOWED_EXT.has(ext));
  },
  limits: { fileSize: 50 * 1024 * 1024 },
});

const ALLOWED_VIDEO_EXT = new Set([".mp4", ".webm", ".mov", ".mkv"]);

const uploadVideo = multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => cb(null, TRACKS_DIR),
    filename: (req, file, cb) => {
      const ext = path.extname(file.originalname).toLowerCase();
      cb(null, `${uuidv4()}${ALLOWED_VIDEO_EXT.has(ext) ? ext : ".mp4"}`);
    },
  }),
  fileFilter: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    cb(null, ALLOWED_VIDEO_EXT.has(ext));
  },
  limits: { fileSize: 500 * 1024 * 1024 },
});

// 曲一覧
app.get("/api/tracks", (req, res) => {
  res.json(loadLibrary());
});

// 曲アップロード
app.post("/api/tracks", upload.single("file"), asyncRoute(async (req, res) => {
  if (!req.file) {
    res.status(400).json({ error: "音源ファイル(mp3/wav/ogg)を指定してください" });
    return;
  }
  const title = (req.body.title || "").trim();
  if (!title) {
    fs.unlink(req.file.path, () => {});
    res.status(400).json({ error: "曲名を入力してください" });
    return;
  }

  const entry = {
    id: path.parse(req.file.filename).name,
    filename: req.file.filename,
    title,
    displayTitle: (req.body.displayTitle || "").trim() || title,
    author: (req.body.author || "").trim(),
    arranged: req.body.arranged === "true" || req.body.arranged === true,
    note: (req.body.note || "").trim(),
    durationSec: await probeDurationSec(req.file.path),
    createdAt: new Date().toISOString(),
  };

  await withLock(() => {
    const library = loadLibrary();
    library.push(entry);
    saveLibrary(library);
  });
  res.status(201).json(entry);
}));

// YouTube動画の情報(タイトル・投稿者)を取得 (ダウンロードはしない、フォーム補完用)
app.get("/api/youtube-info", asyncRoute(async (req, res) => {
  const url = (req.query.url || "").trim();
  if (!url) {
    res.status(400).json({ error: "urlを指定してください" });
    return;
  }
  if (!isYoutubeUrl(url)) {
    res.status(400).json({ error: "YouTubeのURLではありません" });
    return;
  }
  try {
    const info = await fetchYoutubeInfo(url);
    res.json(info);
  } catch (e) {
    res.status(502).json({ error: `情報取得に失敗しました: ${String(e.message || e)}` });
  }
}));

// YouTube動画から音声をダウンロードしてライブラリに登録
app.post("/api/tracks/from-youtube", asyncRoute(async (req, res) => {
  const url = (req.body.url || "").trim();
  const title = (req.body.title || "").trim();
  if (!url || !isYoutubeUrl(url)) {
    res.status(400).json({ error: "有効なYouTubeのURLを指定してください" });
    return;
  }
  if (!title) {
    res.status(400).json({ error: "曲名を入力してください" });
    return;
  }

  const id = uuidv4();
  try {
    const filename = await downloadYoutubeAudio(url, id);
    const entry = {
      id,
      filename,
      title,
      displayTitle: (req.body.displayTitle || "").trim() || title,
      author: (req.body.author || "").trim(),
      arranged: req.body.arranged === true || req.body.arranged === "true",
      note: (req.body.note || "").trim(),
      sourceUrl: url,
      durationSec: await probeDurationSec(path.join(TRACKS_DIR, filename)),
      createdAt: new Date().toISOString(),
    };
    await withLock(() => {
      const library = loadLibrary();
      library.push(entry);
      saveLibrary(library);
    });
    res.status(201).json(entry);
  } catch (e) {
    res.status(502).json({ error: `ダウンロードに失敗しました: ${String(e.message || e)}` });
  }
}));

// 動画一覧
app.get("/api/videos", (req, res) => {
  res.json(loadVideos());
});

// 動画アップロード
app.post("/api/videos", uploadVideo.single("file"), asyncRoute(async (req, res) => {
  if (!req.file) {
    res.status(400).json({ error: "動画ファイル(mp4/webm/mov/mkv)を指定してください" });
    return;
  }
  const title = (req.body.title || "").trim();
  if (!title) {
    fs.unlink(req.file.path, () => {});
    res.status(400).json({ error: "動画名を入力してください" });
    return;
  }

  const entry = {
    id: path.parse(req.file.filename).name,
    filename: req.file.filename,
    title,
    displayTitle: (req.body.displayTitle || "").trim() || title,
    createdAt: new Date().toISOString(),
  };

  await withLock(() => {
    const videos = loadVideos();
    videos.push(entry);
    saveVideos(videos);
  });
  res.status(201).json(entry);
}));

// YouTube動画から映像+音声をダウンロードして動画ライブラリに登録
app.post("/api/videos/from-youtube", asyncRoute(async (req, res) => {
  const url = (req.body.url || "").trim();
  const title = (req.body.title || "").trim();
  if (!url || !isYoutubeUrl(url)) {
    res.status(400).json({ error: "有効なYouTubeのURLを指定してください" });
    return;
  }
  if (!title) {
    res.status(400).json({ error: "動画名を入力してください" });
    return;
  }

  const id = uuidv4();
  try {
    const filename = await downloadYoutubeVideo(url, id);
    const entry = {
      id,
      filename,
      title,
      displayTitle: (req.body.displayTitle || "").trim() || title,
      sourceUrl: url,
      createdAt: new Date().toISOString(),
    };
    await withLock(() => {
      const videos = loadVideos();
      videos.push(entry);
      saveVideos(videos);
    });
    res.status(201).json(entry);
  } catch (e) {
    res.status(502).json({ error: `ダウンロードに失敗しました: ${String(e.message || e)}` });
  }
}));

// 動画削除 (ファイルごと削除。行事の次第から参照されている場合は警告だけ返して削除は実行する)
app.delete("/api/videos/:id", asyncRoute(async (req, res) => {
  const entry = await withLock(() => {
    const videos = loadVideos();
    const idx = videos.findIndex((v) => v.id === req.params.id);
    if (idx === -1) return null;
    const [removed] = videos.splice(idx, 1);
    saveVideos(videos);
    return removed;
  });
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  fs.unlink(path.join(TRACKS_DIR, entry.filename), () => {});

  const program = loadProgram();
  const stillUsed = program.some((item) => item.performingVideoId === entry.id);
  res.json({ deleted: entry.id, warning: stillUsed ? "行事の次第から参照されたままです" : null });
}));

// ボーカル除去 (Demucs) を実行し、インストゥルメンタル版を新しい曲として登録する
app.post("/api/tracks/:id/remove-vocals", (req, res) => {
  const library = loadLibrary();
  const entry = library.find((t) => t.id === req.params.id);
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  const sourcePath = path.join(TRACKS_DIR, entry.filename);
  if (!fs.existsSync(sourcePath)) {
    res.status(404).json({ error: "元の音源ファイルが見つかりません" });
    return;
  }

  const newId = uuidv4();
  const jobId = createJob(entry.id);
  res.status(202).json({ jobId });

  removeVocals(sourcePath, newId, (progress) => {
    const job = jobs.get(jobId);
    if (job) job.progress = progress;
  })
    .then(async (filename) => {
      const newEntry = {
        id: newId,
        filename,
        title: `${entry.title} (Instrumental)`,
        // displayTitle(配信画面での表示名)は原曲と揃えたままにする。
        // 舞台上では「ボーカル除去済みに差し替えた」と観客に分からせたくないため。
        displayTitle: entry.displayTitle,
        author: entry.author,
        arranged: true,
        note: ["AIボーカル除去 (Demucs)", entry.note].filter(Boolean).join(" / "),
        sourceTrackId: entry.id,
        durationSec: await probeDurationSec(path.join(TRACKS_DIR, filename)),
        createdAt: new Date().toISOString(),
      };
      await withLock(() => {
        const current = loadLibrary();
        current.push(newEntry);
        saveLibrary(current);
      });
      finishJob(jobId, { status: "done", progress: 100, result: newEntry });
    })
    .catch((e) => {
      finishJob(jobId, { status: "error", progress: 0, error: `ボーカル除去に失敗しました: ${String(e.message || e)}` });
    });
});

// 実行中のジョブ一覧 (ページ再読み込み後、進捗表示を復元するために使う)
app.get("/api/jobs", (req, res) => {
  const running = [...jobs.entries()]
    .filter(([, job]) => job.status === "running")
    .map(([jobId, job]) => ({ jobId, trackId: job.trackId, status: job.status, progress: job.progress }));
  res.json(running);
});

// ジョブの進捗確認 (ボーカル除去・将来の非同期処理で共用)
app.get("/api/jobs/:id", (req, res) => {
  const job = jobs.get(req.params.id);
  if (!job) {
    res.status(404).json({ error: "not found" });
    return;
  }
  res.json(job);
});

// 曲メタデータ編集 (曲名/表示用曲名/作者/注記/BGM化フラグ)
app.patch("/api/tracks/:id", asyncRoute(async (req, res) => {
  const entry = await withLock(() => {
    const library = loadLibrary();
    const found = library.find((t) => t.id === req.params.id);
    if (!found) return null;
    for (const key of ["title", "displayTitle", "author", "note"]) {
      if (typeof req.body[key] === "string") found[key] = req.body[key].trim();
    }
    if (typeof req.body.arranged === "boolean") found.arranged = req.body.arranged;
    saveLibrary(library);
    return found;
  });
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  res.json(entry);
}));

// 曲削除 (ファイルごと削除。行事の次第から参照されている場合は警告だけ返して削除は実行する)
app.delete("/api/tracks/:id", asyncRoute(async (req, res) => {
  const entry = await withLock(() => {
    const library = loadLibrary();
    const idx = library.findIndex((t) => t.id === req.params.id);
    if (idx === -1) return null;
    const [removed] = library.splice(idx, 1);
    saveLibrary(library);
    return removed;
  });
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  fs.unlink(path.join(TRACKS_DIR, entry.filename), () => {});

  const program = loadProgram();
  const stillUsed = program.some((item) =>
    item.bgm === entry.id || item.trackId === entry.id || item.performingTrackId === entry.id
  );
  res.json({ deleted: entry.id, warning: stillUsed ? "行事の次第から参照されたままです" : null });
}));

// last.fmで作者候補を検索 (未設定なら空配列を返す)
app.get("/api/artist-search", asyncRoute(async (req, res) => {
  const q = (req.query.q || "").trim();
  if (!q) {
    res.json({ enabled: Boolean(LASTFM_API_KEY), results: [] });
    return;
  }
  if (!LASTFM_API_KEY) {
    res.json({ enabled: false, results: [] });
    return;
  }
  try {
    const results = await searchLastfm(q);
    res.json({ enabled: true, results });
  } catch (e) {
    res.status(502).json({ enabled: true, results: [], error: String(e.message || e) });
  }
}));

// 行事の次第 (program.json) の取得/保存
app.get("/api/program", (req, res) => {
  res.json(loadProgram());
});

app.put("/api/program", asyncRoute(async (req, res) => {
  if (!Array.isArray(req.body)) {
    res.status(400).json({ error: "配列で送ってください" });
    return;
  }
  for (const item of req.body) {
    if (typeof item.name !== "string" || !item.name.trim()) {
      res.status(400).json({ error: "各演目に name が必要です" });
      return;
    }
  }
  await withLock(() => saveProgram(req.body));
  res.json(req.body);
}));

// 転換用プレイリスト一覧
app.get("/api/playlists", (req, res) => {
  res.json(loadPlaylists());
});

// プレイリスト作成
app.post("/api/playlists", asyncRoute(async (req, res) => {
  const name = (req.body.name || "").trim();
  if (!name) {
    res.status(400).json({ error: "プレイリスト名を入力してください" });
    return;
  }
  const entry = {
    id: uuidv4(),
    name,
    trackIds: Array.isArray(req.body.trackIds) ? req.body.trackIds : [],
    note: (req.body.note || "").trim(),
    loop: req.body.loop !== false, // 既定でループする(従来通り)。false明示時だけOFF
  };
  await withLock(() => {
    const playlists = loadPlaylists();
    playlists.push(entry);
    savePlaylists(playlists);
  });
  res.status(201).json(entry);
}));

// プレイリスト編集 (名前/曲リスト/注記)
app.patch("/api/playlists/:id", asyncRoute(async (req, res) => {
  const entry = await withLock(() => {
    const playlists = loadPlaylists();
    const found = playlists.find((p) => p.id === req.params.id);
    if (!found) return null;
    if (typeof req.body.name === "string" && req.body.name.trim()) found.name = req.body.name.trim();
    if (typeof req.body.note === "string") found.note = req.body.note.trim();
    if (Array.isArray(req.body.trackIds)) found.trackIds = req.body.trackIds;
    if (typeof req.body.loop === "boolean") found.loop = req.body.loop;
    // 結合プレイリスト(他のプレイリストを順番につなげたもの)。
    // 空配列[]は「結合モードだが中身が空」、nullは「結合モード自体を解除して
    // 通常のプレイリスト(trackIds使用)に戻す」で明確に区別する。
    if (Array.isArray(req.body.sourcePlaylistIds)) found.sourcePlaylistIds = req.body.sourcePlaylistIds;
    else if (req.body.sourcePlaylistIds === null) delete found.sourcePlaylistIds;
    savePlaylists(playlists);
    return found;
  });
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  res.json(entry);
}));

// プレイリスト削除 (行事の次第から参照中なら警告だけ返して削除は実行する)
app.delete("/api/playlists/:id", asyncRoute(async (req, res) => {
  const ok = await withLock(() => {
    const playlists = loadPlaylists();
    const idx = playlists.findIndex((p) => p.id === req.params.id);
    if (idx === -1) return false;
    playlists.splice(idx, 1);
    savePlaylists(playlists);
    return true;
  });
  if (!ok) {
    res.status(404).json({ error: "not found" });
    return;
  }

  const program = loadProgram();
  const stillUsed = program.some((item) =>
    item.playlistId === req.params.id || item.performingPlaylistId === req.params.id
  );
  res.json({ deleted: req.params.id, warning: stillUsed ? "行事の次第から参照されたままです" : null });
}));

// 実際に到達できるURLを列挙する (LAN内の別端末から開くときのため)。
function listenUrls() {
  if (HOST !== "0.0.0.0" && HOST !== "::") return [`http://${HOST}:${PORT}`];
  const urls = [`http://127.0.0.1:${PORT}`];
  for (const list of Object.values(os.networkInterfaces())) {
    for (const ni of list || []) {
      if (ni.family === "IPv4" && !ni.internal) urls.push(`http://${ni.address}:${PORT}`);
    }
  }
  return urls;
}

// 全ルートの後に置くエラーハンドラ。画面側は res.json() を期待しているのでJSONで返す。
// (multerのファイルサイズ超過などもここに来る)
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error("[bgm-library] リクエスト処理でエラー:", err);
  if (res.headersSent) return;
  res.status(500).json({ error: `サーバー内部エラー: ${String(err.message || err)}` });
});

// 最後の保険。本番中に取りこぼしで落ちるより、ログを残して動き続けるほうがよい。
process.on("unhandledRejection", (reason) => {
  console.error("[bgm-library] 未処理のPromise拒否:", reason);
});

async function onServerReady() {
  cleanupDemucsWorkDirs();
  console.log(`[bgm-library] 起動しました (bind: ${HOST}:${PORT})`);
  for (const url of listenUrls()) console.log(`[bgm-library]   ${url}`);
  if (HOST === "0.0.0.0" || HOST === "::") {
    console.log(
      "[bgm-library] 認証はありません。同じLAN上の誰でも曲の追加/削除ができます " +
        "(.env で HOST=127.0.0.1 にするとローカル限定になります)"
    );
  }
  console.log(`[bgm-library] 音源保存先: ${TRACKS_DIR}`);
  console.log(`[bgm-library] 次第ファイル: ${PROGRAM_FILE}`);
  if (!LASTFM_API_KEY) {
    console.log("[bgm-library] 作者検索(last.fm)は無効です (.env に LASTFM_API_KEY を設定すると使えます)");
  }
  try {
    await execFileAsync(YT_DLP_CMD, ["--version"], { timeout: 5000 });
  } catch {
    console.log(`[bgm-library] YouTubeダウンロードは無効です (yt-dlp が見つかりません: '${YT_DLP_CMD}'。pip install yt-dlp とffmpegの導入が必要です)`);
  }
  try {
    await execFileAsync(PYTHON_CMD, ["-m", "demucs", "--help"], { timeout: 15000 });
    demucsDevice = await detectDemucsDevice();
    console.log(
      `[bgm-library] ボーカル除去: モデル=${DEMUCS_MODEL} デバイス=${demucsDevice}${DEMUCS_DEVICE_OVERRIDE ? " (固定)" : " (自動検出)"}`
    );
  } catch {
    console.log(`[bgm-library] ボーカル除去は無効です ('${PYTHON_CMD} -m demucs' が実行できません。pip install demucs を確認してください)`);
  }
  try {
    await execFileAsync(FFPROBE_CMD, ["-version"], { timeout: 5000 });
    backfillDurations(); // 起動をブロックしないよう待たずに投げる
  } catch {
    console.log(`[bgm-library] 曲の長さ(プレイリスト合計時間)取得は無効です (ffprobe が見つかりません: '${FFPROBE_CMD}')`);
  }
}

// 既存曲でdurationSecが未設定のものをバックグラウンドで一括取得する
// (この機能を追加する前にアップロード済みの曲を遡って埋めるため)。
async function backfillDurations() {
  const library = loadLibrary();
  const targets = library.filter((t) => t.durationSec == null && fs.existsSync(path.join(TRACKS_DIR, t.filename)));
  if (!targets.length) return;
  console.log(`[bgm-library] 曲の長さ未取得が${targets.length}件あります。バックグラウンドで取得します...`);
  for (const t of targets) {
    t.durationSec = await probeDurationSec(path.join(TRACKS_DIR, t.filename));
  }
  await withLock(() => {
    saveLibrary(
      loadLibrary().map((t) => {
        const updated = targets.find((u) => u.id === t.id);
        return updated ? { ...t, durationSec: updated.durationSec } : t;
      })
    );
  });
  console.log(`[bgm-library] 曲の長さの取得が完了しました`);
}

// ポート衝突(前回のプロセスが終了しきれず残っている等)からの自動復旧。
// そのポートをLISTENしているプロセスを探して強制終了し、1回だけ再試行する。
function killProcessOnPort(port) {
  try {
    if (process.platform === "win32") {
      const out = execFileSync("netstat", ["-ano"], { encoding: "utf-8" });
      const pids = new Set();
      for (const line of out.split("\n")) {
        const m = line.match(/^\s*TCP\s+\S*:(\d+)\s+\S+\s+LISTENING\s+(\d+)/i);
        if (m && Number(m[1]) === port) pids.add(m[2]);
      }
      if (!pids.size) return false;
      for (const pid of pids) {
        try {
          execFileSync("taskkill", ["/PID", pid, "/F"]);
          console.log(`[bgm-library] ポート${port}を使用していたプロセス (PID ${pid}) を終了しました`);
        } catch {}
      }
      return true;
    }
    const out = execFileSync("lsof", ["-ti", `tcp:${port}`], { encoding: "utf-8" });
    const pids = out.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!pids.length) return false;
    for (const pid of pids) {
      try {
        execFileSync("kill", ["-9", pid]);
        console.log(`[bgm-library] ポート${port}を使用していたプロセス (PID ${pid}) を終了しました`);
      } catch {}
    }
    return true;
  } catch {
    return false;
  }
}

function startServer(allowRetry) {
  const server = app.listen(PORT, HOST, onServerReady);
  server.on("error", (err) => {
    if (err.code === "EADDRINUSE" && allowRetry) {
      console.log(`[bgm-library] ポート${PORT}が使用中です。既存プロセスを終了して再試行します`);
      if (killProcessOnPort(PORT)) {
        setTimeout(() => startServer(false), 500);
      } else {
        console.error(`[bgm-library] ポート${PORT}を解放できませんでした。手動で確認してください`);
        process.exit(1);
      }
    } else {
      console.error(`[bgm-library] 起動エラー: ${err}`);
      process.exit(1);
    }
  });
}

startServer(true);
