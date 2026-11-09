const STATUS = {
  passed: ["pass", "通过"],
  failed: ["fail", "失败"],
  error: ["warn", "异常"],
  succeeded: ["pass", "成功"],
  running: ["run", "运行中"],
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
const time = (iso) => (iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "-");

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) || `请求失败 ${res.status}`);
  return data;
}

async function loadRuns() {
  const box = document.getElementById("runs");
  try {
    const runs = await api("/api/runs");
    document.getElementById("run-count").textContent = `共 ${runs.length} 次`;
    if (!runs.length) {
      box.innerHTML = '<div class="empty">还没有运行记录，先在上方发起一次评测。</div>';
      return;
    }
    box.innerHTML = `<table>
      <thead><tr><th>运行</th><th>项目</th><th>状态</th><th>通过率</th><th>用例</th><th>成本</th><th>触发</th><th>开始时间</th></tr></thead>
      <tbody>${runs.map((r) => `<tr>
        <td class="mono"><a href="/run.html?id=${encodeURIComponent(r.id)}">${esc(r.id.slice(0, 10))}</a></td>
        <td>${esc(r.project)}</td>
        <td>${badge(r.status)}</td>
        <td>${pct(r.pass_rate)}</td>
        <td>${esc(r.passed)}/${esc(r.total)}</td>
        <td class="mono">${usd(r.cost_usd)}</td>
        <td class="muted">${esc(r.trigger)}</td>
        <td class="muted">${esc(time(r.started_at))}</td>
      </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

document.getElementById("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = document.getElementById("run-submit");
  const message = document.getElementById("form-msg");
  const tags = form.tags.value.split(",").map((t) => t.trim()).filter(Boolean);

  button.disabled = true;
  message.textContent = "运行中…";
  try {
    const outcome = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project: form.project.value.trim(),
        cases: form.cases.value.trim(),
        tags: tags.length ? tags : null,
        baseline: form.baseline.checked,
      }),
    });
    location.href = `/run.html?id=${encodeURIComponent(outcome.run_id)}`;
  } catch (err) {
    message.textContent = err.message;
    button.disabled = false;
  }
});

loadRuns();
