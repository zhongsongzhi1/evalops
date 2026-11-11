const runId = new URLSearchParams(location.search).get("id");

function renderMetrics(run, report) {
  const delta = report ? report.delta : null;
  const costRatio = report ? report.cost_delta_ratio : null;
  const costText =
    costRatio == null
      ? "暂无对比"
      : costRatio === 0
        ? "与基线持平"
        : `${costRatio > 0 ? "+" : ""}${(costRatio * 100).toFixed(1)}% vs 基线`;
  document.getElementById("metrics").innerHTML = `
    <div class="metric">
      <div class="k">Pass Rate</div>
      <div class="v ${run.pass_rate < 1 ? "bad" : ""}">${pct(run.pass_rate)}</div>
      <div class="d">${delta == null ? "暂无对比" : `${signed(delta)} vs 基线`}</div>
    </div>
    <div class="metric">
      <div class="k">断言通过率</div>
      <div class="v">${pct(run.assertion_pass_rate)}</div>
      <div class="d">${esc(run.assertion_passed ?? 0)} / ${esc(run.assertion_total ?? 0)} 条断言</div>
    </div>
    <div class="metric">
      <div class="k">用例通过</div>
      <div class="v">${esc(run.passed)} / ${esc(run.total)}</div>
      <div class="d">失败 ${esc(run.failed)} · 异常 ${esc(run.errored)}</div>
    </div>
    <div class="metric">
      <div class="k">p95 延迟</div>
      <div class="v">${ms(run.p95_latency_ms)}</div>
      <div class="d">tokens ${esc(run.tokens_in ?? 0)} in / ${esc(run.tokens_out ?? 0)} out</div>
    </div>
    <div class="metric">
      <div class="k">本次成本</div>
      <div class="v ${costRatio > 0 ? "bad" : ""}">${usd(run.cost_usd)}</div>
      <div class="d">${costText}</div>
    </div>
    <div class="metric">
      <div class="k">单例成本</div>
      <div class="v">${usd(run.cost_per_case)}</div>
      <div class="d">共 ${esc(run.total)} 条用例</div>
    </div>`;
}

function chipRow(label, keys, cls) {
  const items = keys && keys.length
    ? keys
        .map((key) => `<a class="chip ${cls}" href="/compare.html?baseline=baseline&candidate=${encodeURIComponent(runId)}&case=${encodeURIComponent(key)}">${esc(key)}</a>`)
        .join("")
    : '<span class="muted">无</span>';
  return `<div class="chip-row"><span class="chip-label">${esc(label)}</span>${items}</div>`;
}

function renderCompare(report) {
  const box = document.getElementById("compare-body");
  if (!report) {
    box.innerHTML =
      '<div class="hint">还不存在基线运行。先在任意一次成功的运行上点「标记为基线」，再回到这里查看变化。</div>';
    return;
  }
  const costRatio = report.cost_delta_ratio;
  const costText =
    costRatio === 0 ? "持平" : `${costRatio > 0 ? "+" : ""}${(costRatio * 100).toFixed(1)}%`;
  box.innerHTML = `
    <div class="stat-row">
      <div class="stat">
        <span class="k">Pass Rate</span>
        <span class="v ${report.delta < 0 ? "bad" : "good"}">${signed(report.delta)}</span>
        <span class="muted">${pct(report.baseline_pass_rate)} → ${pct(report.candidate_pass_rate)}</span>
      </div>
      <div class="stat">
        <span class="k">断言通过率</span>
        <span class="v">${pct(report.baseline_assertion_pass_rate)} → ${pct(report.candidate_assertion_pass_rate)}</span>
      </div>
      <div class="stat">
        <span class="k">成本变化</span>
        <span class="v ${costRatio > 0 ? "bad" : ""}">${costText}</span>
      </div>
      <div class="stat">
        <span class="k">基线运行</span>
        <span class="v mono"><a href="/run.html?id=${encodeURIComponent(report.baseline_run_id)}">${esc(shortId(report.baseline_run_id))}</a></span>
      </div>
    </div>
    <div class="chip-rows">
      ${chipRow("New Failures", report.new_failures, "fail")}
      ${chipRow("已修复", report.fixed, "pass")}
      ${chipRow("新增用例", report.added, "muted")}
      ${chipRow("移除用例", report.removed, "warn")}
    </div>`;
}

function renderBanner(report) {
  const box = document.getElementById("banner");
  if (!report) {
    box.innerHTML = `<div class="banner warn"><div><strong>还没有基线运行</strong>
      <p>先点右上角「标记为基线」，再执行一次新的运行后重新对比。</p></div></div>`;
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
    <thead><tr><th>用例</th><th>状态</th><th>耗时</th><th>成本</th><th>失败断言 / 错误</th><th></th></tr></thead>
    <tbody>${run.cases.map((item) => {
      const details = (item.failed_assertions || []).map((a) =>
        `#${esc(a.seq)} ${esc(a.type)} ${esc(pretty(a.detail))}`).join("<br>");
      const reason = item.error ? `<b>错误：</b>${esc(item.error)}` : details || "—";
      return `<tr>
        <td><span class="mono">${esc(item.case_key)}</span><div class="detail">${esc(item.name)}</div></td>
        <td>${badge(item.status)}</td>
        <td class="muted">${ms(item.latency_ms)}</td>
        <td class="mono">${usd(item.cost_usd)}</td>
        <td class="detail">${reason}</td>
        <td class="right"><button class="linkbtn" type="button" data-case="${esc(item.case_key)}">轨迹</button></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function renderTrace(trace) {
  const steps = trace.trajectory || [];
  const assertions = trace.assertions || [];
  const stepList = steps.length
    ? `<ol class="steps">${steps.map((step) => `<li class="step">
        <div class="step-head">
          <span class="step-no">#${esc(step.step)}</span>
          <span class="tag ${esc(step.type)}">${esc(step.type)}</span>
          <span class="step-name">${esc(step.name || "-")}</span>
          <span class="grow"></span>
          <span class="muted">${esc(step.tokens ?? 0)} tok · ${ms(step.latency_ms)}</span>
        </div>
        <div class="io">
          <details><summary>输入</summary><pre>${esc(pretty(step.input))}</pre></details>
          <details><summary>输出</summary><pre>${esc(pretty(step.output))}</pre></details>
        </div>
      </li>`).join("")}</ol>`
    : '<div class="muted">无轨迹记录</div>';
  const assertionTable = assertions.length
    ? `<table class="mini">
        <thead><tr><th>#</th><th>类型</th><th>结果</th><th>分数</th><th>详情</th></tr></thead>
        <tbody>${assertions.map((a) => `<tr>
          <td class="mono">${esc(a.seq)}</td>
          <td>${esc(a.type)}</td>
          <td>${a.passed
            ? '<span class="badge pass"><span class="dot"></span>通过</span>'
            : '<span class="badge fail"><span class="dot"></span>失败</span>'}</td>
          <td class="mono">${a.score == null ? "-" : esc(a.score)}</td>
          <td class="detail">${esc(pretty(a.detail))}</td>
        </tr>`).join("")}</tbody>
      </table>`
    : '<div class="muted">无断言记录</div>';

  return `<div class="trace">
    <div class="trace-meta">
      ${badge(trace.status)}
      <span class="muted">耗时 ${ms(trace.latency_ms)}</span>
      <span class="muted">成本 ${usd(trace.cost_usd)}</span>
      <span class="muted">tokens ${esc(trace.tokens_in)} in / ${esc(trace.tokens_out)} out</span>
      ${trace.error ? `<span class="detail"><b>错误：</b>${esc(trace.error)}</span>` : ""}
    </div>
    <div class="trace-grid">
      <div>
        <h3 class="subhead">轨迹（${steps.length} 步）</h3>
        ${stepList}
      </div>
      <div>
        <h3 class="subhead">断言（${assertions.length} 条）</h3>
        ${assertionTable}
      </div>
    </div>
  </div>`;
}

document.getElementById("cases").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-case]");
  if (!button) return;
  const key = button.dataset.case;
  const row = button.closest("tr");
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("trace-row")) {
    existing.remove();
    button.textContent = "轨迹";
    return;
  }
  document.querySelectorAll(".trace-row").forEach((el) => el.remove());
  document.querySelectorAll("button[data-case]").forEach((el) => {
    el.textContent = "轨迹";
  });
  button.textContent = "收起";

  const traceRow = document.createElement("tr");
  traceRow.className = "trace-row";
  traceRow.innerHTML = '<td colspan="6"><div class="empty">加载轨迹…</div></td>';
  row.after(traceRow);
  try {
    const trace = await api(
      `/api/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(key)}`
    );
    traceRow.firstElementChild.innerHTML = renderTrace(trace);
  } catch (err) {
    traceRow.firstElementChild.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
});

async function load() {
  if (!runId) {
    document.getElementById("banner").innerHTML =
      '<div class="banner block"><div><strong>缺少运行 ID</strong><p>请从运行记录页进入。</p></div></div>';
    return;
  }
  document.getElementById("run-crumb").textContent = runId;
  document.getElementById("diff-link").href =
    `/compare.html?candidate=${encodeURIComponent(runId)}`;

  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    document.getElementById("run-meta").textContent =
      `${run.project} · ${run.model || "-"} · ${run.trigger} · ${run.status}`;
    let report = null;
    try {
      report = await api(
        `/api/compare?baseline=baseline&candidate=${encodeURIComponent(runId)}`
      );
    } catch (err) {
      if (err.status !== 409) throw err;
    }
    renderBanner(report);
    renderMetrics(run, report);
    renderCompare(report);
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
