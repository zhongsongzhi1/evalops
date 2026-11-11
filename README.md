# EvalOps

**Agent 评测、回归对比与 CI 门禁平台。**

前两个 Agent 项目回答"能不能做"，EvalOps 回答"改完之后有没有变差"：
用例集版本化 → 轨迹执行与录制 → 多级断言 → baseline / candidate 对比 → 门禁阻断。

当前进度：**阶段 3 后端（重复运行与 Flaky、指标趋势、轨迹回放与 diff 已就绪）**；
可视化前端尚未开始。

完整设计与实施计划见 [docs/project-plan.md](docs/project-plan.md)。

## 零密钥快速开始

不需要任何模型 API Key：内置 `fake` adapter 读取录制好的轨迹，完整跑通
`run → compare → gate` 三段链路。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"

# 1. 跑一次评测，并标记为基线
evalops run --project examples/projects/fake.yaml --cases examples/cases --baseline

# 2. 把 examples/fixtures/refund_policy.json 里输出中的 "7天" 改掉后，再跑一次
evalops run --project examples/projects/fake.yaml --cases examples/cases

# 3. 对比与门禁：退化用例会被定位到 case_key
evalops compare --candidate latest
evalops gate --candidate latest
# 门禁阻断时退出码为 2，可直接用于 CI
```

数据库默认是当前目录的 `evalops.db`（SQLite），可用 `--db <path|url>` 或
`EVALOPS_DB_URL` 覆盖；PostgreSQL 使用 `EVALOPS_DB_URL=postgresql+asyncpg://...`
并安装 `pip install -e ".[postgres]"`。

## 常用命令

| 命令 | 作用 | 退出码 |
|---|---|---|
| `evalops run` | 执行用例集，落库轨迹、断言结果、成本 | 有用例错误时 1 |
| `evalops compare` | 按 `case_key` 对齐，输出 New Failures / 修复 / 新增 / 移除 | 0 |
| `evalops gate` | 按门禁规则给出 `pass` / `warn` / `block` | `block` 时 2 |
| `evalops label` | 导入人工评分，绑定到具体某次 Judge 输出 | 全部跳过时 1 |
| `evalops kappa` | 计算 Judge 与人工评分的 Cohen's κ | 低于阈值时 1 |
| `evalops flaky` | 按运行组或指定运行统计 Flaky Rate 与不稳定断言 | 数据不足时 1 |
| `evalops metrics` | 输出最近 N 次运行的 Pass Rate / 断言通过率 / p95 / 单例成本 | 0 |
| `evalops diff` | 对比两次运行的轨迹与断言差异，`--case` 展开逐步 diff | 0 |
| `evalops trace` | 回放单用例的完整轨迹与全部断言结果 | 0 |

运行状态机为 `queued → running → succeeded / failed / cancelled`：执行异常或
Ctrl+C 也会写入终态（`finished_at` 不为空），不会永久停在 `running`；只有
`succeeded` 的运行才能被标记为基线。断言失败属于评测结果（Pass Rate），
不改变执行状态，沿用门禁判定。

常用参数：

```bash
# 只跑带有 regression 标签的用例
evalops run -p examples/projects/fake.yaml -c examples/cases --tag regression

# 指定基线与候选运行
evalops gate --baseline <run_id> --candidate <run_id> --gate examples/gates/default.yaml

# 机器可读输出
evalops gate --candidate latest --json

# 同用例重复 3 次，直接给出 Flaky Rate（并与第一次运行一起标记为同组）
evalops run -p examples/projects/fake.yaml -c examples/cases --repeat 3
evalops flaky --group <group_id>

# 指标趋势与轨迹回放 / 对比
evalops metrics --project demo-agent --limit 20
evalops trace --run latest --case refund_policy
evalops diff --case refund_policy
```

## 真实 adapter

除 `fake`（录制回放）外，阶段 1 提供了两类真实接入方式：

```yaml
# projects/agent.yaml
name: my-agent
adapter: http                 # fake | http | langgraph | openai_agents
config:
  endpoint: "http://127.0.0.1:9000/run"
  headers: { Authorization: "Bearer demo-token" }
  timeout_s: 30
```

- `http`：平台把 `{case_key, name, input, tags}` POST 给 `config.endpoint`，
  服务返回与 fixture 相同的标准轨迹 JSON（`output / steps / tokens_in /
  tokens_out / latency_ms`）。
- `langgraph` / `openai_agents`：声明 `entrypoint: "module:function"`，函数签名
  `run(case, config)`，可同步或异步，返回标准轨迹 dict 或 `Trajectory`。
  框架相关的状态拼装留在被测项目侧（≤50 行），平台只做执行与归一化。
  模块必须位于项目目录内，加载时会校验路径；当前为进程内执行，只接入受信项目。

## 语义断言

```yaml
assertions:
  - type: semantic
    reference: "超过7天通常不支持无理由退款，需人工审核"
    threshold: 0.85
```

embedding 来源由项目配置显式声明，缺省是零密钥的离线 `mock`：

```yaml
config:
  embedding:
    provider: mock            # 字符 n-gram 哈希向量，仅用于演示与 CI
    # provider: openai        # 真实路径：OpenAI 兼容 /embeddings 接口
    # model: text-embedding-3-small
    # base_url: https://api.openai.com/v1
    # api_key_env: OPENAI_API_KEY
```

`mock` 衡量的是字面重合度，不代表真实语义，README 与指标中均按「演示模式」标注；
需要真实语义时切到 `openai`（或兼容网关）并显式配置 Key。

## LLM Judge

`llm_judge` 按独立 rubric 文件打分，默认采样 3 次取中位数：

```yaml
assertions:
  - type: llm_judge
    rubric: ../rubrics/refund_accuracy.md
    pass_score: 4
    samples: 3
```

rubric 是 Markdown，可带 YAML front matter。`version` 会写入断言结果与人工标注，
用于追溯"这条结论由哪版标准产出"：

```markdown
---
version: refund_accuracy_v1
reference: "超过7天退款期后无法直接退款，需要人工审核。"
---

# 退款答复准确性
- 5：结论准确，给出下一步
- 1：承诺无条件退款
```

评分来源由项目配置声明，缺省为零密钥的 `mock`：

```yaml
config:
  judge:
    provider: mock             # 用输出与 reference 的字面相似度模拟评分，仅演示 / CI
    # provider: openai         # OpenAI 兼容 /chat/completions
    # model: gpt-4o-mini
    # base_url: https://api.openai.com/v1
    # api_key_env: OPENAI_API_KEY
    # temperature: 0
```

`mock` 不代表真实评审能力；需要可信的 Judge 时必须切换真实模型，并完成下面的校准。

## Judge 校准

人工标注 CSV 格式为 `case_key,human_score,notes`（分数 1–5），标注会绑定到本次运行
的具体输出，而不是用例：

```bash
# 1. 导入人工评分（重复导入同一 case_result 会更新而不是新增）
evalops label --run latest --csv labels.csv --labeled-by alice

# 2. 计算 Cohen's κ（未加权 + 二次加权）
evalops kappa --project demo-agent --threshold 0.6
```

流程要求：人工标注 ≥ 50 条 → κ ≥ 0.6 才允许 Judge 进入门禁。样本不足时 `kappa`
会明确提示「结论仅供参考」，不伪造达标。

门禁可以要求 κ 达标：

```yaml
rules:
  require_kappa: 0.6   # 无标注或 κ 低于阈值时直接 block
```

## GitHub PR 自动评测

服务端暴露 `POST /webhooks/github`：校验 `X-Hub-Signature-256`、按
`X-GitHub-Delivery` 幂等、忽略 fork PR，然后执行候选评测、判门禁并把 New Failures
回评到 PR。

```bash
export EVALOPS_GITHUB_SECRET=...            # 必填，Webhook secret
export EVALOPS_GITHUB_TOKEN=...             # 可选，缺失时只评测不评论
export EVALOPS_PROJECT=examples/projects/fake.yaml
export EVALOPS_CASES=examples/cases
export EVALOPS_GATE_FILE=examples/gates/default.yaml
export EVALOPS_BASELINE=baseline            # 基线标签 / 运行 ID
evalops serve --port 8000
```

首次接入还没有基线时，webhook 只记录运行、不回评阻断；先跑一次 `evalops run --baseline`。
也可以在 GitHub Actions 中直接跑 CLI：`evalops run` → `evalops gate`，`block` 时
退出码 2，工作流按失败处理。

## 可视化控制台

不想敲命令时，可以启动内置控制台查看运行记录、用例结果与门禁判定：

```bash
pip install -e ".[web]"
evalops serve --port 8000
# 打开 http://127.0.0.1:8000
```

页面只读为主：运行列表、运行详情（指标 + 失败断言定位）、一键标记基线并查看
门禁结果；也可以从页面触发一次评测。配置仍由 YAML / CLI 管理。
需要鉴权或多用户协作时，应在此之上替换为正式 Web 服务。

阶段 3 新增的查询接口：

| 接口 | 作用 |
|---|---|
| `GET /api/projects` | 项目列表（运行次数、最近运行时间） |
| `GET /api/projects/{id}/metrics?limit=20` | 项目最近 N 次运行的指标趋势 |
| `GET /api/runs/{id}/cases/{case_key}` | 单用例完整轨迹与全部断言 |
| `GET /api/runs/{id}` | 运行详情，新增 `assertion_pass_rate` / `p95_latency_ms` / `cost_per_case` |

## 用例 DSL

用例是声明式数据，不是代码：

```yaml
name: 退款政策咨询
key: refund_policy          # 默认取 name；跨版本稳定，回归按它对齐
input:
  messages:
    - role: user
      content: "我买的课程能退款吗？已经过了7天"
expected:
  answer_contains: ["退款", "7天"]
  answer_not_contains: ["保证退款"]
  tool_calls:
    - name: query_order
    - name: check_refund_policy
      args_contains: { order_id: "*" }   # "*" 表示只要求参数存在
assertions:
  - type: max_latency_ms
    value: 5000
tags: [regression, refund]
```

已实现断言：`contains` / `not_contains` / `equals` / `regex` /
`tool_calls`（含顺序与 `args_contains`）/ `max_latency_ms` / `max_cost_usd` /
`semantic`（余弦相似度）/ `llm_judge`（rubric + 多次采样取中位数）；
`json_schema` 暂不支持。

门禁规则：

```yaml
name: default
rules:
  max_pass_rate_drop: 0.02   # 允许的最大通过率跌幅
  max_new_failures: 0        # 任何 New Failure 直接阻断
  max_cost_increase: 0.20    # 成本涨幅上限
  # require_kappa: 0.6       # 启用后，Judge 与人工一致性不达标直接 block
  allow_removed_cases: true  # 是否允许删除用例
```

## 指标口径

- **Pass Rate** = 全部断言通过的用例数 / 用例数；
- **断言通过率** = 通过断言数 / 断言总数（单独统计，不混进 Pass Rate）；
- **New Failures** = 基线上通过、候选未通过的用例数，按 `case_key` 对齐；
- **Judge-Human Kappa** = Judge 与人工评分的 Cohen's κ（同时给出二次加权值）；
- **单次运行成本** = Σ`tokens_in × 输入单价 + tokens_out × 输出单价`，单价按项目配置；
- **Cost per Case** = 单次运行成本 / 用例数；
- **p95 延迟** = 用例延迟的 95 分位（最近秩法，空集为 0）；
- **Flaky Rate** = 参与重跑（同组 N ≥ 3 次）的用例中，任一断言结果不一致、或状态在
  `passed` 与非 `passed` 之间摆动的占比；不足 3 次的用例计入跳过，不参与分母。

## 目录结构

```
src/evalops/
├── cases/dsl.py        # project.yaml / cases/*.yaml 校验，case_key 规则
├── adapters/           # fake / http / langgraph / openai_agents
├── embeddings.py       # semantic 断言的 embedding 来源（mock / openai）
├── judge.py            # LLM Judge、rubric 加载与版本化（mock / openai）
├── calibration.py      # 人工标注导入与 Cohen's κ
├── assertions.py       # 断言引擎（精确 + 语义 + 工具轨迹 + Judge）
├── runner.py           # 执行、轨迹与工具录制落库、成本计算
├── regression.py       # baseline/candidate 对比与门禁判定
├── metrics.py          # 运行指标（通过率、p95、成本）与项目趋势
├── flaky.py            # 重复运行分组与 Flaky 判定
├── diff.py             # 两次运行的轨迹/断言 diff 与单用例回放
├── github.py           # PR webhook：签名、幂等、fork 防护、回评
├── db/models.py        # 12 张表
├── db/schema.py        # Alembic 升级入口
└── cli.py              # run / compare / gate / label / kappa / flaky / metrics / diff / trace / serve
examples/               # golden 用例、录制轨迹、门禁配置
tests/                  # 单测 + 门禁回归测试
```

## 设计要点

- **稳定身份**：`case_key` 默认取 `name`，唯一约束 `(case_set_id, case_key)`；
  重命名视为删除 + 新增，需要显式 `key`。用例集导入即快照，`content_hash` 相同则复用。
- **adapter 白名单**：只允许 `fake | http | langgraph | openai_agents`，并按
  `entrypoint` 显式加载，避免 YAML 变成任意代码执行入口。
- **录制回放**：`tool_recording` 表 + `fixtures/*.json` 支撑离线、可复现的评测。
- **失败不重试**：断言失败不自动重试，避免把 flaky 掩盖成偶发；重跑是显式操作。
- **Judge 先校准再进门禁**：rubric 版本随结果落库，人工标注绑定具体输出；
  无标注或 κ 不达标时，`require_kappa` 会直接 block，而不是"先信后补"。
- **Webhook 边界**：签名校验、`delivery_id` 幂等、fork PR 跳过、后台执行；
  密钥只从环境变量读取，不进入用例或仓库。

## 非目标

不做 Agent 框架、不做通用 LLM 网关、不做多租户计费与 K8s、不做复杂可视化画布；
只做两个薄 adapter，评测范围限定在"同项目跨 run"的回归。

## 路线图

- **阶段 0**：仓库骨架、12 张表、DSL、fake adapter、精确断言、三条 CLI、CI
- **阶段 1**：语义断言（mock / OpenAI 兼容）、真实 adapter（http + entrypoint）、FastAPI 控制面
- **阶段 2**：LLM Judge、Judge 校准（Cohen's κ）、门禁 require_kappa、GitHub PR 回评
- **阶段 3（当前，后端完成）**：重复运行与 flaky 判定、p95 / 成本趋势、轨迹回放与 diff；最小前端待补
- **阶段 4**：Docker Compose、英文 README、demo 录屏

## 开发

```bash
ruff check .
mypy src
pytest -q
```
