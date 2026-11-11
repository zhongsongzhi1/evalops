/* 页面共享的格式化与请求工具，保持零构建、零依赖。 */

const STATUS = {
  passed: ["pass", "通过"],
  failed: ["fail", "失败"],
  error: ["warn", "异常"],
  succeeded: ["pass", "成功"],
  running: ["run", "运行中"],
  queued: ["run", "排队中"],
  cancelled: ["warn", "已取消"],
};

const VERDICT = {
  pass: ["pass", "门禁 PASS"],
  warn: ["warn", "门禁 WARN"],
  block: ["block", "门禁 BLOCK"],
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function badge(status) {
  const [cls, label] = STATUS[status] || ["", status];
  return `<span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
}

const pct = (x) => `${(Number(x || 0) * 100).toFixed(1)}%`;
const usd = (x) => `$${Number(x || 0).toFixed(6)}`;
const ms = (x) => (x == null ? "-" : `${Math.round(x)} ms`);
const shortId = (id) => (id ? String(id).slice(0, 10) : "-");
const signed = (x) => `${x >= 0 ? "+" : ""}${(Number(x || 0) * 100).toFixed(1)}pp`;

function time(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function shortDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function pretty(value) {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const error = new Error((data && data.detail) || `请求失败 ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
}
