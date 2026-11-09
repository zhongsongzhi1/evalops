# EvalOps

**Agent 评测、回归对比与 CI 门禁平台。**

前两个 Agent 项目回答"能不能做"，EvalOps 回答"改完之后有没有变差"：
用例集版本化 → 轨迹执行与录制 → 多级断言 → baseline / candidate 对比 → 门禁阻断。

当前进度：**阶段 0（零密钥最小闭环）**。

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

## 三条命令

| 命令 | 作用 | 退出码 |
|---|---|---|
| `evalops run` | 执行用例集，落库轨迹、断言结果、成本 | 有用例错误时 1 |
| `evalops compare` | 按 `case_key` 对齐，输出 New Failures / 修复 / 新增 / 移除 | 0 |
| `evalops gate` | 按门禁规则给出 `pass` / `warn` / `block` | `block` 时 2 |

常用参数：

```bash
# 只跑带有 regression 标签的用例
evalops run -p examples/projects/fake.yaml -c examples/cases --tag regression

# 指定基线与候选运行
evalops gate --baseline <run_id> --candidate <run_id> --gate examples/gates/default.yaml

# 机器可读输出
evalops gate --candidate latest --json
```

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

阶段 0 已实现断言：`contains` / `not_contains` / `equals` / `regex` /
`tool_calls`（含顺序与 `args_contains`）/ `max_latency_ms` / `max_cost_usd`。
语义断言与 LLM Judge 在阶段 1 接入，`json_schema` 暂不支持。

门禁规则：

```yaml
name: default
rules:
  max_pass_rate_drop: 0.02   # 允许的最大通过率跌幅
  max_new_failures: 0        # 任何 New Failure 直接阻断
  max_cost_increase: 0.20    # 成本涨幅上限
  allow_removed_cases: true  # 是否允许删除用例
```

## 指标口径

- **Pass Rate** = 全部断言通过的用例数 / 用例数；
- **断言通过率** = 通过断言数 / 断言总数（单独统计，不混进 Pass Rate）；
- **New Failures** = 基线上通过、候选未通过的用例数，按 `case_key` 对齐；
- **Cost per Case** = `tokens_in × 输入单价 + tokens_out × 输出单价`，按项目配置分档；
- **Flaky Rate**（阶段 3）= 参与重跑（N ≥ 3）的用例中结果不一致的占比。

## 目录结构

```
src/evalops/
├── cases/dsl.py        # project.yaml / cases/*.yaml 校验，case_key 规则
├── adapters/           # adapter 白名单：fake / http / langgraph / openai_agents
├── assertions.py       # 断言引擎（精确 + 工具轨迹）
├── runner.py           # 执行、轨迹落库、成本计算
├── regression.py       # baseline/candidate 对比与门禁判定
├── db/models.py        # 12 张表
├── db/schema.py        # Alembic 升级入口
└── cli.py              # run / compare / gate
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

## 非目标

不做 Agent 框架、不做通用 LLM 网关、不做多租户计费与 K8s、不做复杂可视化画布；
只做两个薄 adapter，评测范围限定在"同项目跨 run"的回归。

## 路线图

- **阶段 0（当前）**：仓库骨架、12 张表、DSL、fake adapter、精确断言、三条 CLI、CI
- **阶段 1**：语义断言、真实 adapter、FastAPI 控制面
- **阶段 2**：工具轨迹断言、LLM Judge、Judge 校准（Cohen's κ）、GitHub Actions 门禁
- **阶段 3**：flaky 率、p95、成本趋势、轨迹回放 diff、最小前端
- **阶段 4**：Docker Compose、英文 README、demo 录屏

## 开发

```bash
ruff check .
mypy src
pytest -q
```
