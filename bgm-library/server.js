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
 *   このUIから編集し、各項目にライブラリの曲を割り当てられる。
 * - YouTubeのURLを渡すと yt-dlp + ffmpeg で音声を抽出してライブラリに登録できる
 *   (要 yt-dlp / ffmpeg のインストール)。ダウンロードした音源の著作権・利用規約は
 *   利用者側の責任で確認すること。
 * - 登録済みの曲から「ボーカル除去」でDemucs (要 pip install demucs、main.py と
 *   同じvenv) を実行し、インストゥルメンタル版を新しい曲として追加できる。
 *   CPU実行のため曲の長さと同程度〜数倍の処理時間がかかる。
 */

require("dotenv").config();
const fs = require("fs");
const path = require("path");
const express = require("express");
const multer = require("multer");
const { v4: uuidv4 } = require("uuid");
const { execFile } = require("child_process");
const { promisify } = require("util");
const execFileAsync = promisify(execFile);

const PORT = Number(process.env.PORT || 4000);
const TRACKS_DIR = path.resolve(__dirname, process.env.TRACKS_DIR || "../tracks");
const PROGRAM_FILE = path.resolve(__dirname, process.env.PROGRAM_FILE || "../program.json");
const LIBRARY_FILE = path.join(TRACKS_DIR, "tracks.json");

const LASTFM_API_KEY = process.env.LASTFM_API_KEY || "";
const YT_DLP_CMD = process.env.YT_DLP_CMD || "yt-dlp";
const FFMPEG_CMD = process.env.FFMPEG_CMD || "ffmpeg";

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

fs.mkdirSync(TRACKS_DIR, { recursive: true });

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
  fs.writeFileSync(LIBRARY_FILE, JSON.stringify(list, null, 2), "utf-8");
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
  fs.writeFileSync(PROGRAM_FILE, JSON.stringify(items, null, 2), "utf-8");
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

// ------------------------------------------------------------
// AIボーカル除去 (Demucs, htdemucsモデル)
// ------------------------------------------------------------
async function removeVocals(sourcePath, newId) {
  const workDir = path.join(TRACKS_DIR, `.demucs-${newId}`);
  fs.mkdirSync(workDir, { recursive: true });
  try {
    await execFileAsync(
      PYTHON_CMD,
      ["-m", "demucs", "--two-stems=vocals", "-o", workDir, sourcePath],
      { maxBuffer: 20 * 1024 * 1024, timeout: 10 * 60 * 1000 }
    );

    const baseName = path.parse(sourcePath).name;
    const wavPath = path.join(workDir, "htdemucs", baseName, "no_vocals.wav");
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

// 曲一覧
app.get("/api/tracks", (req, res) => {
  res.json(loadLibrary());
});

// 曲アップロード
app.post("/api/tracks", upload.single("file"), (req, res) => {
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
    createdAt: new Date().toISOString(),
  };

  const library = loadLibrary();
  library.push(entry);
  saveLibrary(library);
  res.status(201).json(entry);
});

// YouTube動画の情報(タイトル・投稿者)を取得 (ダウンロードはしない、フォーム補完用)
app.get("/api/youtube-info", async (req, res) => {
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
});

// YouTube動画から音声をダウンロードしてライブラリに登録
app.post("/api/tracks/from-youtube", async (req, res) => {
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
      createdAt: new Date().toISOString(),
    };
    const library = loadLibrary();
    library.push(entry);
    saveLibrary(library);
    res.status(201).json(entry);
  } catch (e) {
    res.status(502).json({ error: `ダウンロードに失敗しました: ${String(e.message || e)}` });
  }
});

// ボーカル除去 (Demucs) を実行し、インストゥルメンタル版を新しい曲として登録する
app.post("/api/tracks/:id/remove-vocals", async (req, res) => {
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
  try {
    const filename = await removeVocals(sourcePath, newId);
    const newEntry = {
      id: newId,
      filename,
      title: `${entry.title} (Instrumental)`,
      displayTitle: `${entry.displayTitle} (Instrumental)`,
      author: entry.author,
      arranged: true,
      note: ["AIボーカル除去 (Demucs)", entry.note].filter(Boolean).join(" / "),
      sourceTrackId: entry.id,
      createdAt: new Date().toISOString(),
    };
    const current = loadLibrary();
    current.push(newEntry);
    saveLibrary(current);
    res.status(201).json(newEntry);
  } catch (e) {
    res.status(502).json({ error: `ボーカル除去に失敗しました: ${String(e.message || e)}` });
  }
});

// 曲メタデータ編集 (曲名/表示用曲名/作者/注記/BGM化フラグ)
app.patch("/api/tracks/:id", (req, res) => {
  const library = loadLibrary();
  const entry = library.find((t) => t.id === req.params.id);
  if (!entry) {
    res.status(404).json({ error: "not found" });
    return;
  }
  for (const key of ["title", "displayTitle", "author", "note"]) {
    if (typeof req.body[key] === "string") entry[key] = req.body[key].trim();
  }
  if (typeof req.body.arranged === "boolean") entry.arranged = req.body.arranged;
  saveLibrary(library);
  res.json(entry);
});

// 曲削除 (ファイルごと削除。行事の次第から参照されている場合は警告だけ返して削除は実行する)
app.delete("/api/tracks/:id", (req, res) => {
  const library = loadLibrary();
  const idx = library.findIndex((t) => t.id === req.params.id);
  if (idx === -1) {
    res.status(404).json({ error: "not found" });
    return;
  }
  const [entry] = library.splice(idx, 1);
  saveLibrary(library);
  fs.unlink(path.join(TRACKS_DIR, entry.filename), () => {});

  const program = loadProgram();
  const stillUsed = program.some((item) => item.bgm === entry.id);
  res.json({ deleted: entry.id, warning: stillUsed ? "行事の次第から参照されたままです" : null });
});

// last.fmで作者候補を検索 (未設定なら空配列を返す)
app.get("/api/artist-search", async (req, res) => {
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
});

// 行事の次第 (program.json) の取得/保存
app.get("/api/program", (req, res) => {
  res.json(loadProgram());
});

app.put("/api/program", (req, res) => {
  if (!Array.isArray(req.body)) {
    res.status(400).json({ error: "配列で送ってください" });
    return;
  }
  for (const item of req.body) {
    if (typeof item.name !== "string" || !item.name.trim()) {
      res.status(400).json({ error: "各項目に name が必要です" });
      return;
    }
  }
  saveProgram(req.body);
  res.json(req.body);
});

app.listen(PORT, async () => {
  console.log(`[bgm-library] http://localhost:${PORT} で起動しました`);
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
  } catch {
    console.log(`[bgm-library] ボーカル除去は無効です ('${PYTHON_CMD} -m demucs' が実行できません。pip install demucs を確認してください)`);
  }
});
