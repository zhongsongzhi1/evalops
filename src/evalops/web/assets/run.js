const runId = new URLSearchParams(location.search).get("id");

const STATUS = {
  passed: ["pass", "通过"],
  failed: ["fail", "失败"],
  error: ["warn", "异常"],
  succeeded: ["pass", "成功"],
  running: ["run", "运行中"],
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

function renderMetrics(run, report) {
  const drops = report ? report.delta : null;
  const costRatio = report ? report.cost_delta_ratio : null;
  document.getElementById("metrics").innerHTML = `
    <div class="metric">
      <div class="k">Pass Rate</div>
      <div class="v ${run.pass_rate < 1 ? "bad" : ""}">${pct(run.pass_rate)}</div>
      <div class="d">${drops == null ? "暂无对比" : `${drops >= 0 ? "+" : ""}${(drops * 100).toFixed(1)}pp vs 基线`}</div>
    </div>
    <div class="metric">
      <div class="k">用例通过</div>
      <div class="v">${esc(run.passed)} / ${esc(run.total)}</div>
      <div class="d">失败 ${esc(run.failed)} · 异常 ${esc(run.errored)}</div>
    </div>
    <div class="metric">
      <div class="k">本次成本</div>
      <div class="v">${usd(run.cost_usd)}</div>
      <div class="d">${costRatio == null ? "暂无对比" : `较基线 ${(costRatio * 100).toFixed(1)}%`}</div>
    </div>
    <div class="metric">
      <div class="k">用例数</div>
      <div class="v">${esc(run.total)}</div>
      <div class="d">${run.finished_at ? "已完成" : "进行中"}</div>
    </div>`;
}

function renderBanner(report) {
  const box = document.getElementById("banner");
  if (!report) {
    box.innerHTML = `<div class="banner warn"><div><strong>还没有基线运行</strong>
      <p>先点右上角「标记为基线」，再改动用例或执行一次新运行后重新对比。</p></div></div>`;
    return;
  }
  const [cls, label] = VERDICT[report.verdict] || ["warn", report.verdict];
  const reasons = (report.reasons || []).join(" · ") || "所有门禁规则通过";
  const failures = report.new_failures && report.new_failures.length
    ? `新增失败：${report.new_failures.join("、")}`
    : "";
  box.innerHTML = `<div class="banner ${cls}"><div>
    <strong>${esc(label)}</strong>
    <p>${esc(reasons)}${failures ? ` · ${esc(failures)}` : ""}</p>
  </div></div>`;
}

function renderCases(run) {
  const box = document.getElementById("cases");
  if (!run.cases.length) {
    box.innerHTML = '<div class="empty">这次运行没有用例结果。</div>';
    return;
  }
  box.innerHTML = `<table>
    <thead><tr><th>用例</th><th>状态</th><th>耗时</th><th>成本</th><th>失败断言 / 错误</th></tr></thead>
    <tbody>${run.cases.map((item) => {
      const details = (item.failed_assertions || []).map((a) =>
        `#${esc(a.seq)} ${esc(a.type)} ${esc(a.detail)}`).join("<br>");
      const reason = item.error ? `<b>错误：</b>${esc(item.error)}` : details || "—";
      return `<tr>
        <td><span class="mono">${esc(item.case_key)}</span><div class="detail">${esc(item.name)}</div></td>
        <td>${badge(item.status)}</td>
        <td class="muted">${ms(item.latency_ms)}</td>
        <td class="mono">${usd(item.cost_usd)}</td>
        <td class="detail">${reason}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

async function load() {
  if (!runId) {
    document.getElementById("banner").innerHTML =
      '<div class="banner block"><div><strong>缺少运行 ID</strong><p>请从运行记录页进入。</p></div></div>';
    return;
  }
  document.getElementById("run-crumb").textContent = runId;

  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    document.getElementById("run-meta").textContent =
      `${run.project} · ${run.model || "-"} · ${run.trigger} · ${run.status}`;
    let report = null;
    try {
      report = await api(`/api/compare?baseline=baseline&candidate=${encodeURIComponent(runId)}`);
    } catch (err) {
      if (err.status !== 409) throw err;
    }
    renderBanner(report);
    renderMetrics(run, report);
    renderCases(run);
  } catch (err) {
    document.getElementById("banner").innerHTML =
      `<div class="banner block"><div><strong>加载失败</strong><p>${esc(err.message)}</p></div></div>`;
  }
}

document.getElementById("mark-baseline").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/baseline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "latest" }),
    });
    location.reload();
  } catch (err) {
    button.disabled = false;
    alert(err.message);
  }
});

load();
