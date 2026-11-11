const state = { projects: [], projectId: null };

function projectName() {
  const project = state.projects.find((item) => item.id === state.projectId);
  return project ? project.name : "";
}

function renderTrend(runs) {
  const box = document.getElementById("trend-chart");
  if (!runs.length) {
    box.innerHTML = '<div class="empty">还没有运行数据，先在上方发起一次评测。</div>';
    document.getElementById("trend-summary").innerHTML = "";
    return;
  }

  const W = 720;
  const H = 220;
  const P = { t: 18, r: 18, b: 30, l: 42 };
  const n = runs.length;
  const x = (i) =>
    n === 1 ? P.l + (W - P.l - P.r) / 2 : P.l + (i * (W - P.l - P.r)) / (n - 1);
  const y = (v) => P.t + (1 - Math.max(0, Math.min(1, Number(v || 0)))) * (H - P.t - P.b);
  const line = (key) =>
    runs.map((run, i) => `${x(i).toFixed(1)},${y(run[key]).toFixed(1)}`).join(" ");
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((v) => `<line class="grid" x1="${P.l}" y1="${y(v)}" x2="${W - P.r}" y2="${y(v)}"/>` +
      `<text class="axis" x="${P.l - 8}" y="${y(v) + 4}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`)
    .join("");
  const step = Math.max(1, Math.ceil(n / 6));
  const labels = runs
    .map((run, i) =>
      i % step === 0 || i === n - 1
        ? `<text class="axis" x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(shortDate(run.started_at))}</text>`
        : "")
    .join("");
  const dots = (key, cls) =>
    runs
      .map((run, i) => `<circle class="dot-${cls}" cx="${x(i)}" cy="${y(run[key])}" r="3">` +
        `<title>${esc(shortId(run.run_id))} · ${pct(run[key])} · ${esc(time(run.started_at))}</title></circle>`)
      .join("");

  box.innerHTML = `
    <div class="legend">
      <span><i class="swatch pass"></i>Pass Rate</span>
      <span><i class="swatch assert"></i>断言通过率</span>
    </div>
    <svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="通过率趋势">
      ${grid}
      <polyline class="series pass" points="${line("pass_rate")}"/>
      <polyline class="series assert" points="${line("assertion_pass_rate")}"/>
      ${dots("pass_rate", "pass")}
      ${dots("assertion_pass_rate", "assert")}
      ${labels}
    </svg>`;

  const latest = runs[runs.length - 1];
  document.getElementById("trend-summary").innerHTML = `
    <div class="metrics">
      <div class="metric">
        <div class="k">最近 Pass Rate</div>
        <div class="v ${latest.pass_rate < 1 ? "bad" : ""}">${pct(latest.pass_rate)}</div>
        <div class="d">${esc(shortId(latest.run_id))}</div>
      </div>
      <div class="metric">
        <div class="k">断言通过率</div>
        <div class="v">${pct(latest.assertion_pass_rate)}</div>
        <div class="d">${esc(latest.assertion_passed)} / ${esc(latest.assertion_total)} 条断言</div>
      </div>
      <div class="metric">
        <div class="k">p95 延迟</div>
        <div class="v">${ms(latest.p95_latency_ms)}</div>
        <div class="d">单用例</div>
      </div>
      <div class="metric">
        <div class="k">单例成本</div>
        <div class="v">${usd(latest.cost_per_case)}</div>
        <div class="d">共 ${esc(runs.length)} 次运行</div>
      </div>
    </div>`;
}

async function loadTrend() {
  const box = document.getElementById("trend-chart");
  if (!state.projectId) {
    box.innerHTML = '<div class="empty">还没有项目，先运行一次评测。</div>';
    document.getElementById("trend-summary").innerHTML = "";
    return;
  }
  box.innerHTML = '<div class="empty">加载趋势…</div>';
  try {
    const data = await api(
      `/api/projects/${encodeURIComponent(state.projectId)}/metrics?limit=20`
    );
    renderTrend(data.runs || []);
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    document.getElementById("trend-summary").innerHTML = "";
  }
}

async function loadRuns() {
  const box = document.getElementById("runs");
  try {
    const runs = await api("/api/runs?limit=100");
    const name = projectName();
    const visible = name ? runs.filter((run) => run.project === name) : runs;
    document.getElementById("run-count").textContent = name
      ? `${name} · 共 ${visible.length} 次`
      : `共 ${visible.length} 次`;
    if (!visible.length) {
      box.innerHTML = '<div class="empty">还没有运行记录，先在上方发起一次评测。</div>';
      return;
    }
    box.innerHTML = `<table>
      <thead><tr><th>运行</th><th>项目</th><th>状态</th><th>通过率</th><th>用例</th><th>成本</th><th>触发</th><th>开始时间</th><th></th></tr></thead>
      <tbody>${visible.map((r) => `<tr>
        <td class="mono"><a href="/run.html?id=${encodeURIComponent(r.id)}">${esc(shortId(r.id))}</a></td>
        <td>${esc(r.project)}</td>
        <td>${badge(r.status)}</td>
        <td>${pct(r.pass_rate)}</td>
        <td>${esc(r.passed)}/${esc(r.total)}</td>
        <td class="mono">${usd(r.cost_usd)}</td>
        <td class="muted">${esc(r.trigger)}</td>
        <td class="muted">${esc(time(r.started_at))}</td>
        <td class="right"><a class="linkbtn" href="/compare.html?candidate=${encodeURIComponent(r.id)}">对比</a></td>
      </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

async function loadProjects() {
  const select = document.getElementById("project-select");
  try {
    state.projects = await api("/api/projects");
    if (!state.projects.length) {
      select.innerHTML = "<option>暂无项目</option>";
      select.disabled = true;
      return;
    }
    select.innerHTML = state.projects
      .map((p) => `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.run_count)} 次</option>`)
      .join("");
    state.projectId = state.projects[0].id;
    select.value = state.projectId;
    select.addEventListener("change", () => {
      state.projectId = select.value;
      loadTrend();
      loadRuns();
    });
  } catch (err) {
    select.innerHTML = `<option>${esc(err.message)}</option>`;
    select.disabled = true;
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

(async () => {
  await loadProjects();
  await Promise.all([loadTrend(), loadRuns()]);
})();
