// 2枚のモニターを横につなげて1つの巨大画面として使うための共通ロジック。
// MONITOR = 1 (左) or 2 (右) を各HTMLで指定してから読み込むこと。
// 両モニターの解像度が同じである前提で、テキストを「2画面ぶんの仮想キャンバス」に
// 一度だけ描画し、モニターごとに表示位置をずらして片側だけを見せることで
// あたかも1枚の巨大パネルであるかのように文字がつながって見える。

const API_URL = "http://127.0.0.1:8787/now-playing";
const POLL_MS = 700;

const appEl = document.getElementById("app");
const stageEl = document.getElementById("stage");
const statusBadge = document.getElementById("status-badge");
const trackName = document.getElementById("track-name");
const connError = document.getElementById("conn-error");

function layoutStage() {
  const screenW = window.innerWidth;
  const screenH = window.innerHeight;
  stageEl.style.width = (screenW * 2) + "px";
  stageEl.style.height = screenH + "px";
  // MONITOR 1 は左半分(オフセットなし)、MONITOR 2 は右半分(1画面ぶん左にずらす)
  stageEl.style.left = (MONITOR === 2 ? -screenW : 0) + "px";
}

function fitTrackName() {
  const maxWidth = stageEl.clientWidth * 0.94;
  trackName.style.fontSize = "";
  let size = parseFloat(getComputedStyle(trackName).fontSize);
  while (trackName.scrollWidth > maxWidth && size > 12) {
    size -= 4;
    trackName.style.fontSize = size + "px";
  }
}

async function poll() {
  try {
    const res = await fetch(API_URL, { cache: "no-store" });
    if (!res.ok) throw new Error("bad status");
    const data = await res.json();
    connError.classList.remove("show");

    trackName.textContent = data.track.replace(/\.(mp3|wav|ogg)$/i, "");
    statusBadge.textContent = data.playing ? "NOW PLAYING" : "PAUSED";
    appEl.classList.toggle("paused", !data.playing);

    layoutStage();
    fitTrackName();
  } catch (e) {
    connError.classList.add("show");
  }
}

window.addEventListener("resize", () => { layoutStage(); fitTrackName(); });

poll();
setInterval(poll, POLL_MS);
