const params = new URLSearchParams(location.search);
const baselineSelect = document.getElementById("baseline-select");
const candidateSelect = document.getElementById("candidate-select");

const CHANGE = {
  same: ["muted", "未变化"],
  new_failure: ["fail", "新增失败"],
  fixed: ["pass", "已修复"],
  still_failing: ["warn", "仍失败"],
  added: ["muted", "新增"],
  removed: ["warn", "移除"],
};

const STEP_STATUS = {
  same: ["muted", "未变化"],
  changed: ["warn", "有变化"],
  added: ["pass", "新增步骤"],
  removed: ["fail", "移除步骤"],
};

function option(value, label) {
  return `<option value="${esc(value)}">${esc(label)}</option>`;
}

function setSelect(select, value) {
  const exists = Array.from(select.options).some((item) => item.value === value);
  select.value = exists ? value : select.options[0].value;
}

function fillRuns(runs) {
  const options = [option("baseline", "最近基线"), option("latest", "最近运行")]
    .concat(
      runs.map((run) =>
        option(run.id, `${shortId(run.id)} · ${run.project} · ${pct(run.pass_rate)} · ${time(run.started_at)}`)
      )
    )
    .join("");
  baselineSelect.innerHTML = options;
  candidateSelect.innerHTML = options;
  setSelect(baselineSelect, params.get("baseline") || "baseline");
  setSelect(candidateSelect, params.get("candidate") || "latest");
}

function renderBanner(report) {
  const [cls, label] = VERDICT[report.verdict] || ["warn", report.verdict];
  const reasons = (report.reasons || []).join(" · ") || "所有门禁规则通过";
  const changed = report.cases.filter((item) => item.change !== "same").length;
  document.getElementById("banner").innerHTML = `<div class="banner ${cls}"><div>
    <strong>${esc(label)}</strong>
    <p>${esc(reasons)} · ${esc(changed)} 条用例有变化</p>
  </div></div>`;
}

function renderMetrics(report) {
  const cost = report.cost_delta_ratio;
  const costText = cost === 0
    ? "持平"
    : `${cost > 0 ? "+" : ""}${(cost * 100).toFixed(1)}%`;
  document.getElementById("metrics").innerHTML = `
    <div class="metric">
      <div class="k">候选 Pass Rate</div>
      <div class="v ${report.delta < 0 ? "bad" : ""}">${pct(report.candidate_pass_rate)}</div>
      <div class="d">${signed(report.delta)} · 基线 ${pct(report.baseline_pass_rate)}</div>
    </div>
    <div class="metric">
      <div class="k">断言通过率</div>
      <div class="v">${pct(report.candidate_assertion_pass_rate)}</div>
      <div class="d">基线 ${pct(report.baseline_assertion_pass_rate)}</div>
    </div>
    <div class="metric">
      <div class="k">成本变化</div>
      <div class="v ${cost > 0 ? "bad" : ""}">${costText}</div>
      <div class="d">候选相对基线</div>
    </div>
    <div class="metric">
      <div class="k">New Failures</div>
      <div class="v ${report.new_failures.length ? "bad" : ""}">${esc(report.new_failures.length)}</div>
      <div class="d">修复 ${esc(report.fixed.length)} · 新增 ${esc(report.added.length)} · 移除 ${esc(report.removed.length)}</div>
    </div>`;
}

function chipRow(label, keys, cls) {
  const items = keys.length
    ? keys
        .map((key) => `<button class="chip ${cls}" type="button" data-diff-case="${esc(key)}">${esc(key)}</button>`)
        .join("")
    : '<span class="muted">无</span>';
  return `<div class="chip-row"><span class="chip-label">${esc(label)}</span>${items}</div>`;
}

function renderSummary(report) {
  document.getElementById("run-refs").innerHTML =
    `<a href="/run.html?id=${encodeURIComponent(report.baseline_run_id)}">${esc(shortId(report.baseline_run_id))}</a>` +
    ` → <a href="/run.html?id=${encodeURIComponent(report.candidate_run_id)}">${esc(shortId(report.candidate_run_id))}</a>`;
  document.getElementById("summary").innerHTML = `
    ${chipRow("New Failures", report.new_failures, "fail")}
    ${chipRow("已修复", report.fixed, "pass")}
    ${chipRow("新增用例", report.added, "muted")}
    ${chipRow("移除用例", report.removed, "warn")}`;
}

function renderCases(report) {
  const box = document.getElementById("case-list");
  document.getElementById("case-count").textContent = `${report.cases.length} 条用例`;
  if (!report.cases.length) {
    box.innerHTML = '<div class="empty">两次运行都没有用例结果。</div>';
    return;
  }
  box.innerHTML = `<table>
    <thead><tr><th>用例</th><th>变化</th><th>基线</th><th>候选</th><th></th></tr></thead>
    <tbody>${report.cases.map((item) => {
      const [cls, label] = CHANGE[item.change] || ["muted", item.change];
      return `<tr>
        <td class="mono">${esc(item.case_key)}</td>
        <td><span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span></td>
        <td>${item.baseline ? badge(item.baseline) : '<span class="muted">—</span>'}</td>
        <td>${item.candidate ? badge(item.candidate) : '<span class="muted">—</span>'}</td>
        <td class="right">${item.change === "same"
          ? ""
          : `<button class="linkbtn" type="button" data-diff-case="${esc(item.case_key)}">逐步</button>`}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function assertionCell(value) {
  if (!value) return '<span class="muted">—</span>';
  const cls = value.passed ? "pass" : "fail";
  const label = value.passed ? "通过" : "失败";
  const score = value.score == null ? "" : ` · ${value.score}`;
  return `<span class="badge ${cls}"><span class="dot"></span>${label}${esc(score)}</span>`;
}

function renderDiff(entry) {
  const steps = entry.steps || [];
  const stepItems = steps.map((step) => {
    const [cls, label] = STEP_STATUS[step.status] || ["muted", step.status];
    const head = `<span class="step-no">#${esc(step.step)}</span>
      <span class="tag ${esc(step.type || "")}">${esc(step.type || "-")}</span>
      <span class="step-name">${esc(step.name || "-")}</span>
      <span class="grow"></span>
      <span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
    if (step.status === "same") {
      return `<li class="diff-step same">${head}</li>`;
    }
    if (step.status === "added" || step.status === "removed") {
      const where = step.status === "added" ? "候选" : "基线";
      return `<li class="diff-step ${cls}">${head}<div class="detail">该步骤仅存在于${where}运行。</div></li>`;
    }
    const rows = step.changes.map((change) => `<tr>
      <td class="mono">${esc(change.field)}</td>
      <td><pre class="inline">${esc(pretty(change.baseline))}</pre></td>
      <td><pre class="inline">${esc(pretty(change.candidate))}</pre></td>
    </tr>`).join("");
    return `<li class="diff-step ${cls}">${head}
      <table class="mini diff">
        <thead><tr><th>字段</th><th>基线</th><th>候选</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </li>`;
  }).join("");

  const assertionRows = (entry.assertion_changes || []).map((change) => `<tr>
    <td class="mono">${esc(change.seq)}</td>
    <td>${esc(change.type)}</td>
    <td>${assertionCell(change.baseline)}</td>
    <td>${assertionCell(change.candidate)}</td>
  </tr>`).join("");

  return `<div class="trace">
    <div class="trace-meta">
      <span class="muted">基线</span>${entry.baseline_status ? badge(entry.baseline_status) : '<span class="muted">—</span>'}
      <span class="muted">→ 候选</span>${entry.candidate_status ? badge(entry.candidate_status) : '<span class="muted">—</span>'}
      <span class="muted">变化步骤 ${esc(entry.step_changes)}</span>
      <span class="muted">断言变化 ${esc((entry.assertion_changes || []).length)}</span>
      <span class="muted">耗时 ${ms(entry.latency_ms.baseline)} → ${ms(entry.latency_ms.candidate)}</span>
      <span class="muted">成本 ${usd(entry.cost_usd.baseline)} → ${usd(entry.cost_usd.candidate)}</span>
    </div>
    <h3 class="subhead">轨迹（${steps.length} 步）</h3>
    <ol class="steps">${stepItems || '<li class="muted">无轨迹记录</li>'}</ol>
    <h3 class="subhead">断言变化（${(entry.assertion_changes || []).length} 条）</h3>
    ${assertionRows
      ? `<table class="mini"><thead><tr><th>#</th><th>类型</th><th>基线</th><th>候选</th></tr></thead><tbody>${assertionRows}</tbody></table>`
      : '<div class="muted">两次运行的断言结果一致。</div>'}
  </div>`;
}

async function openDiff(caseKey) {
  const panel = document.getElementById("diff-panel");
  panel.hidden = false;
  document.getElementById("diff-case").textContent = caseKey;
  const body = document.getElementById("diff-body");
  body.innerHTML = '<div class="empty">加载 diff…</div>';
  params.set("case", caseKey);
  history.replaceState(null, "", `?${params.toString()}`);
  try {
    const data = await api(
      `/api/diff?baseline=${encodeURIComponent(baselineSelect.value)}` +
      `&candidate=${encodeURIComponent(candidateSelect.value)}` +
      `&case=${encodeURIComponent(caseKey)}`
    );
    body.innerHTML = renderDiff(data.cases[0]);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    body.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function clearDiff() {
  document.getElementById("diff-panel").hidden = true;
  document.getElementById("diff-body").innerHTML = "";
  params.delete("case");
  history.replaceState(null, "", `?${params.toString()}`);
}

async function loadCompare(autoCase) {
  const baseline = baselineSelect.value;
  const candidate = candidateSelect.value;
  params.set("baseline", baseline);
  params.set("candidate", candidate);
  if (!autoCase) params.delete("case");
  history.replaceState(null, "", `?${params.toString()}`);

  document.getElementById("compare-msg").textContent = "加载中…";
  document.getElementById("diff-panel").hidden = true;
  try {
    const report = await api(
      `/api/compare?baseline=${encodeURIComponent(baseline)}` +
      `&candidate=${encodeURIComponent(candidate)}`
    );
    renderBanner(report);
    renderMetrics(report);
    renderSummary(report);
    renderCases(report);
    document.getElementById("compare-msg").textContent = "";
    if (autoCase) await openDiff(autoCase);
  } catch (err) {
    document.getElementById("compare-msg").textContent = "";
    document.getElementById("banner").innerHTML =
      `<div class="banner block"><div><strong>无法对比</strong><p>${esc(err.message)}</p></div></div>`;
    document.getElementById("metrics").innerHTML = "";
    document.getElementById("run-refs").textContent = "";
    document.getElementById("summary").innerHTML = "";
    document.getElementById("case-list").innerHTML =
      '<div class="empty">先运行一次评测并标记基线，再回来对比。</div>';
    document.getElementById("case-count").textContent = "";
    document.getElementById("diff-panel").hidden = true;
  }
}

document.getElementById("compare-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadCompare(null);
});

document.getElementById("diff-close").addEventListener("click", clearDiff);

document.querySelector("main").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-diff-case]");
  if (button) openDiff(button.dataset.diffCase);
});

(async () => {
  const initialCase = params.get("case");
  try {
    const runs = await api("/api/runs?limit=100");
    fillRuns(runs);
  } catch (err) {
    document.getElementById("compare-msg").textContent = err.message;
  }
  await loadCompare(initialCase);
})();
