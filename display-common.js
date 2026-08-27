// 2枚のモニター(サイズ・解像度が違っていてもOK)を横につなげて
// 1つの巨大パネルとして使うための共通ロジック。
// MONITOR = 1 (左) or 2 (右) を各HTMLで指定してから読み込むこと。
//
// 位置調整はこのページ自身では行わない(OBSのBrowser Source内では操作できないため)。
// 代わりに control.html から main.py のローカルAPI(/calib)経由で設定された
// 「画面の高さ(cm)」「縦オフセット(px)」をポーリングで受け取り、それに従って
// 表示だけを行う。物理的に同じ文字サイズ・同じ継ぎ目位置になるよう、
// cm単位で計算してから各モニター自身のpxPerCmでpx換算する。

// main.py がこのページ自身も配信しているため、相対パスで同じオリジンを指す
// (127.0.0.1固定だとLAN上の他端末から開いたときにその端末自身を見てしまうため)
const NOW_PLAYING_URL = "/now-playing";
const CALIB_URL = "/calib";
const POLL_MS = 700;
const DEFAULT_PX_PER_CM = 37.8; // 96dpi相当のフォールバック値(未キャリブレート時)

const appEl = document.getElementById("app");
const stageEl = document.getElementById("stage");
const statusBadge = document.getElementById("status-badge");
const trackName = document.getElementById("track-name");
const trackSub = document.getElementById("track-sub");
const connError = document.getElementById("conn-error");

// 作者・「当方でBGM化(編集・二次利用)した音源」の注意書き・自由記述の注記を、
// 曲名の下に小さく表示するための1行にまとめる (著作権表示・出典明記のため)。
function formatTrackSub(track) {
  if (!track) return "";
  const parts = [];
  if (track.author) parts.push(track.author);
  if (track.arranged) parts.push("編集音源");
  if (track.note) parts.push(track.note);
  return parts.join(" / ");
}

// 上演中は主題が演目名になり曲名自体が画面から消えるため、説明欄には
// 曲名を先頭に足して「曲名 / 作者 / 編集音源 / 注記」の形にする。
function formatBgmLine(track) {
  if (!track) return "";
  return [track.title, formatTrackSub(track)].filter(Boolean).join(" / ");
}

const OTHER = MONITOR === 1 ? 2 : 1;
const state = {
  1: { heightCm: null, yOffsetPx: 0, innerHeightPx: window.innerHeight, innerWidthPx: window.innerWidth },
  2: { heightCm: null, yOffsetPx: 0, innerHeightPx: window.innerHeight, innerWidthPx: window.innerWidth },
};

function pxPerCm(monitorState) {
  // heightCm が未設定・0・不正値 (文字列やnull) だと NaN / Infinity になり、
  // そのままフォントサイズ計算に伝播して "NaNpx" となり画面から文字が消える。
  // 過去に不正値が calib_state.json へ保存されていた場合の保険でもある。
  const value = monitorState.innerHeightPx / monitorState.heightCm;
  if (Number.isFinite(value) && value > 0) return value;
  return DEFAULT_PX_PER_CM;
}

let lastData = null;

async function pollCalib() {
  try {
    const res = await fetch(CALIB_URL, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (data["1"]) { state[1].heightCm = data["1"].heightCm; state[1].yOffsetPx = data["1"].yOffsetPx || 0; }
    if (data["2"]) { state[2].heightCm = data["2"].heightCm; state[2].yOffsetPx = data["2"].yOffsetPx || 0; }
    render(lastData);
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
  trackSub.style.fontSize = (canonicalFontCm * myPxPerCm * 0.35) + "px";

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

// main.py --program 利用時は {mode, current_item/next_item, bgm} 形式、
// 未使用時は {track: {title, author, arranged}, playing, ...} 形式で返る。
function extractNowPlaying(data) {
  // 未開始 (Nをまだ一度も押していない)。開演前で客席からスクリーンが見えている
  // 時間帯なので、最初の演目名を出しておく。この分岐が無いと下の返り値に落ちて
  // title が undefined になり、曲名が空欄のままバッジだけ "PAUSED" と出る。
  if (data.mode === "ready") {
    return { title: data.next_item, statusText: "まもなく開始", playing: false, sub: "" };
  }
  if (data.mode === "performing") {
    return { title: data.current_item, statusText: "上演中", playing: false, sub: formatBgmLine(data.bgm) };
  }
  const track = (data.track && typeof data.track === "object") ? data.track : null;
  return {
    title: track ? track.title : data.track,
    statusText: data.playing ? "NOW PLAYING" : "PAUSED",
    playing: Boolean(data.playing),
    sub: formatTrackSub(track),
  };
}

// 転換中は2画面を1枚として繋げず、モニターごとに独立した内容を表示する。
// 1枚目(左): 大きく「転換中 Next: 次の演目名」
// 2枚目(右): 小さく「再生中: 曲名/作者」
function layoutTransition(data) {
  state[MONITOR].innerHeightPx = window.innerHeight;
  state[MONITOR].innerWidthPx = window.innerWidth;

  stageEl.style.width = window.innerWidth + "px";
  stageEl.style.height = window.innerHeight + "px";
  stageEl.style.left = "0px";
  stageEl.style.top = (state[MONITOR].yOffsetPx || 0) + "px";

  const myPxPerCm = pxPerCm(state[MONITOR]);
  const heightCm = state[MONITOR].heightCm
    ? window.innerHeight / myPxPerCm
    : (window.innerHeight / DEFAULT_PX_PER_CM);

  if (MONITOR === 1) {
    trackName.textContent = `Next: ${data.next_item}`;
    trackName.style.fontSize = (heightCm * 0.28 * myPxPerCm) + "px";
    trackName.style.color = "";
    trackSub.textContent = "";
    statusBadge.style.visibility = "visible";
  } else if (data.bgm) {
    trackName.textContent = `再生中：${data.bgm.title}`;
    trackName.style.fontSize = (heightCm * 0.08 * myPxPerCm) + "px";
    trackName.style.color = "";
    trackSub.textContent = formatTrackSub(data.bgm);
    trackSub.style.fontSize = (heightCm * 0.045 * myPxPerCm) + "px";
    statusBadge.style.visibility = "visible";
  } else {
    // 無音転換: 流す曲が無く画面がほぼ空になってしまうので、状態が一目で
    // 分かるよう「転換中」自体を(バッジと同じ水色で)大きく表示する。
    // 上のバッジも同じ文字なので二重に見えないよう隠す。
    trackName.textContent = "転換中";
    trackName.style.fontSize = (heightCm * 0.28 * myPxPerCm) + "px";
    trackName.style.color = "#7de1ff";
    trackSub.textContent = "";
    statusBadge.style.visibility = "hidden";
  }
  statusBadge.textContent = "転換中";
  statusBadge.style.fontSize = (heightCm * 0.03 * myPxPerCm) + "px";

  appEl.classList.remove("paused");
  fitTrackName(window.innerWidth);
}

function render(data) {
  if (!data) return;
  if (data.mode === "transition") {
    layoutTransition(data);
    return;
  }
  const info = extractNowPlaying(data);
  trackName.textContent = info.title ?? "";
  trackSub.textContent = info.sub || "";
  statusBadge.textContent = info.statusText;
  statusBadge.style.visibility = "visible";
  appEl.classList.toggle("paused", !info.playing);
  layoutStage();
}

// --- 再生中の曲情報ポーリング ---
async function pollNowPlaying() {
  try {
    const res = await fetch(NOW_PLAYING_URL, { cache: "no-store" });
    if (!res.ok) throw new Error("bad status");
    const data = await res.json();
    connError.classList.remove("show");
    lastData = data;
    render(data);
  } catch (e) {
    connError.classList.add("show");
  }
}

window.addEventListener("resize", () => { if (lastData) render(lastData); else layoutStage(); });

layoutStage();
pollNowPlaying();
pollCalib();
setInterval(pollNowPlaying, POLL_MS);
setInterval(pollCalib, POLL_MS);
