// Inkframe placeholder UI: polls /api/status and fills the status card.
// An ES module with no dependencies; the full interface replaces it later.

const POLL_MS = 5000;
// middle dot and multiplication sign, spelled out to keep this file ASCII
const DOT = String.fromCharCode(0xb7);
const TIMES = String.fromCharCode(0xd7);

const field = (name) => document.querySelector(`[data-field="${name}"]`);

function setText(name, text) {
  const el = field(name);
  if (el) el.textContent = text;
}

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function formatWhen(iso) {
  if (!iso) return "never";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  return t.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// uptime ticks locally between polls
let uptimeBase = null;

function render(status) {
  const { display, panel, rotation, system } = status;
  setText("panel", panel.state);
  field("panel-dot").dataset.state = panel.state;
  setText("display", `${display.kind} ${DOT} ${display.width}${TIMES}${display.height}`);
  const source = panel.last_source ? ` ${DOT} ${panel.last_source.label}` : "";
  setText("last-refresh", formatWhen(panel.last_refresh_at) + source);
  setText("rotation", rotation.paused ? "paused" : "running");
  setText("host", system.hostname);
  setText("version", `Inkframe ${status.version}`);
  uptimeBase = { seconds: system.uptime_seconds, at: performance.now() };
  tickUptime();
}

function tickUptime() {
  if (!uptimeBase) return;
  const elapsed = (performance.now() - uptimeBase.at) / 1000;
  setText("uptime", formatDuration(uptimeBase.seconds + elapsed));
}

function setConnection(state, text) {
  const el = field("conn");
  el.dataset.state = state;
  el.textContent = text;
}

async function poll() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
    setConnection("online", "live");
  } catch (err) {
    setConnection("offline", "offline");
    console.warn("status poll failed:", err);
  }
}

async function loop() {
  if (document.visibilityState === "visible") await poll();
  setTimeout(loop, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") poll();
});
setInterval(tickUptime, 1000);
loop();
