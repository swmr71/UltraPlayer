// 2枚のモニター(サイズ・解像度が違っていてもOK)を横につなげて
// 1つの巨大パネルとして使うための共通ロジック。
// MONITOR = 1 (左) or 2 (右) を各HTMLで指定してから読み込むこと。
//
// 位置調整はこのページ自身では行わない(OBSのBrowser Source内では操作できないため)。
// 代わりに control.html から main.py のローカルAPI(/calib)経由で設定された
// 「画面の高さ(cm)」「縦オフセット(px)」をポーリングで受け取り、それに従って
// 表示だけを行う。物理的に同じ文字サイズ・同じ継ぎ目位置になるよう、
// cm単位で計算してから各モニター自身のpxPerCmでpx換算する。

const NOW_PLAYING_URL = "http://127.0.0.1:8787/now-playing";
const CALIB_URL = "http://127.0.0.1:8787/calib";
const POLL_MS = 700;
const DEFAULT_PX_PER_CM = 37.8; // 96dpi相当のフォールバック値(未キャリブレート時)

const appEl = document.getElementById("app");
const stageEl = document.getElementById("stage");
const statusBadge = document.getElementById("status-badge");
const trackName = document.getElementById("track-name");
const connError = document.getElementById("conn-error");

const OTHER = MONITOR === 1 ? 2 : 1;
const state = {
  1: { heightCm: null, yOffsetPx: 0, innerHeightPx: window.innerHeight, innerWidthPx: window.innerWidth },
  2: { heightCm: null, yOffsetPx: 0, innerHeightPx: window.innerHeight, innerWidthPx: window.innerWidth },
};

function pxPerCm(monitorState) {
  if (monitorState.heightCm && monitorState.innerHeightPx) {
    return monitorState.innerHeightPx / monitorState.heightCm;
  }
  return DEFAULT_PX_PER_CM;
}

async function pollCalib() {
  try {
    const res = await fetch(CALIB_URL, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (data["1"]) { state[1].heightCm = data["1"].heightCm; state[1].yOffsetPx = data["1"].yOffsetPx || 0; }
    if (data["2"]) { state[2].heightCm = data["2"].heightCm; state[2].yOffsetPx = data["2"].yOffsetPx || 0; }
    layoutStage();
  } catch (e) {
    // キャリブレーションサーバーに繋がらない場合はデフォルト値のまま表示を続ける
  }
}

// --- レイアウト計算 (cm基準で統一し、自分のpxPerCmで換算) ---
function layoutStage() {
  state[MONITOR].innerHeightPx = window.innerHeight;
  state[MONITOR].innerWidthPx = window.innerWidth;

  const myPxPerCm = pxPerCm(state[MONITOR]);
  const otherPxPerCm = pxPerCm(state[OTHER]);

  const widthCm1 = state[1].innerWidthPx / (MONITOR === 1 ? myPxPerCm : otherPxPerCm);
  const widthCm2 = state[2].innerWidthPx / (MONITOR === 2 ? myPxPerCm : otherPxPerCm);

  const totalWidthCm = widthCm1 + widthCm2;
  const totalWidthPx = totalWidthCm * myPxPerCm;
  const offsetCm = (MONITOR === 2) ? widthCm1 : 0;

  stageEl.style.width = totalWidthPx + "px";
  stageEl.style.height = window.innerHeight + "px";
  stageEl.style.left = (-offsetCm * myPxPerCm) + "px";
  stageEl.style.top = (state[MONITOR].yOffsetPx || 0) + "px";

  // フォントの物理サイズ(cm)は左右で揃える: 双方の画面高さのうち小さい方を基準に、
  // 高さの42%を文字高さにする(はみ出す場合は幅方向で追加縮小)
  const myHeightCm = state[MONITOR].heightCm ? window.innerHeight / myPxPerCm : (window.innerHeight / DEFAULT_PX_PER_CM);
  const otherHeightCm = state[OTHER].heightCm ? (state[OTHER].innerHeightPx / otherPxPerCm) : myHeightCm;
  const canonicalFontCm = Math.min(myHeightCm, otherHeightCm) * 0.42;

  trackName.style.fontSize = (canonicalFontCm * myPxPerCm) + "px";
  statusBadge.style.fontSize = (canonicalFontCm * myPxPerCm * 0.12) + "px";

  fitTrackName(totalWidthPx);
}

function fitTrackName(totalWidthPx) {
  const maxWidth = totalWidthPx * 0.94;
  let size = parseFloat(getComputedStyle(trackName).fontSize);
  while (trackName.scrollWidth > maxWidth && size > 12) {
    size -= 4;
    trackName.style.fontSize = size + "px";
  }
}

// --- 再生中の曲情報ポーリング ---
async function pollNowPlaying() {
  try {
    const res = await fetch(NOW_PLAYING_URL, { cache: "no-store" });
    if (!res.ok) throw new Error("bad status");
    const data = await res.json();
    connError.classList.remove("show");

    trackName.textContent = data.track.replace(/\.(mp3|wav|ogg)$/i, "");
    statusBadge.textContent = data.playing ? "NOW PLAYING" : "PAUSED";
    appEl.classList.toggle("paused", !data.playing);

    layoutStage();
  } catch (e) {
    connError.classList.add("show");
  }
}

window.addEventListener("resize", layoutStage);

layoutStage();
pollNowPlaying();
pollCalib();
setInterval(pollNowPlaying, POLL_MS);
setInterval(pollCalib, POLL_MS);
