# EvalOps：Agent 评测与回归平台 —— 项目实施与面试准备方案

> 定位：面向 AI 应用开发岗位的第三个项目，补齐现有简历中"评测 / 回归 / 成本治理"的空白。
> 目标面试时间：次年 3–4 月。
> 项目形态：独立新仓库，从零实现，开源。

---

## 1. 为什么做这个项目

现有两个项目（贷款文案生成、智能信贷助手）已经覆盖 LangGraph、RAG、MCP、记忆、可观测，但存在三个缺口：

1. 两个项目高度同质，第三个同类 Agent 边际收益低；
2. 只有一处量化（Tool Registry 70%/20%），缺少"改完之后有没有变差"的闭环；
3. 没有公开产物（GitHub / demo / 文章）。

EvalOps 的价值主张一句话：

> **前两个项目解决"能不能做"，这个项目解决"做完怎么证明它没变差"。**

面试官关心的三个问题，它都能直接回答：效果怎么衡量、回归怎么拦截、成本怎么控制。

---

## 2. 范围

### 做

- 用例集管理与版本化
- Agent 轨迹执行与记录（工具调用、token、耗时、成本）
- 多级断言（精确 / 语义 / 工具序列 / LLM Judge）
- baseline vs candidate 回归对比与 CI 门禁
- 稳定性指标（flaky 率）与成本指标
- 轨迹回放与两次运行 diff
- 最小前端（用例列表、运行详情、对比、趋势）

### 不做（明确写进 README 的非目标）

- 不做 Agent 框架，不重写 LangGraph
- 不做通用 LLM 网关
- 不做复杂可视化画布
- 不做多租户计费、K8s、微服务
- 不支持 10 种以上框架适配（只做 2 个薄适配）

---

## 3. 核心概念与数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `project` | id, name, adapter, config | 被测 Agent 的接入配置 |
| `case_set` | id, project_id, version, content_hash, created_at | 用例集版本（不可变，导入即快照） |
| `case` | id, case_set_id, case_key, name, input(jsonb), expected(jsonb), tags, enabled | 单条用例；`case_key` 跨版本稳定，唯一约束 `(case_set_id, case_key)` |
| `run` | id, project_id, case_set_id, git_sha, model, prompt_version, trigger(manual/ci), config_snapshot(jsonb), status, started_at, finished_at, cost_usd | 一次完整评测 |
| `case_result` | id, run_id, case_id, status, score, latency_ms, tokens_in/out, cost_usd, trajectory(jsonb), error | 单用例结果 |
| `assertion_result` | id, case_result_id, seq, type, passed, score, detail(jsonb) | 每条断言的结果，`seq` 对应 DSL 中的断言序号 |
| `baseline` | id, project_id, run_id, label | 被认可的基线运行 |
| `regression` | id, baseline_run_id, candidate_run_id, summary(jsonb), verdict | 回归结论，verdict ∈ pass / block / warn |
| `gate` | id, project_id, name, scope_tags(jsonb), rules(jsonb), enabled | 门禁规则：最大通过率跌幅、最大 New Failures、成本上限、是否要求 κ 达标 |
| `judge_label` | id, case_id, case_result_id, human_score, judge_score, rubric_version, labeled_by, notes | Judge 校准用的人工标注，必须绑定到具体某次输出 |
| `tool_recording` | id, case_result_id, step, tool, request_digest, response(jsonb), truncated, source(fixture/live) | 工具响应录制，支撑离线回放 |
| `webhook_event` | delivery_id, event, payload_digest, status, run_id | GitHub 重投递幂等去重 |

轨迹统一存 JSONB：`[{step, type: llm|tool, name, input, output, tokens, latency_ms}]`。

---

## 4. 用例 DSL

```yaml
# cases/refund_flow.yaml
name: 退款政策咨询
input:
  messages:
    - role: user
      content: "我买的课程能退款吗？已经过了7天"
expected:
  answer_contains: ["退款", "7天"]
  answer_not_contains: ["保证退款"]
  tool_calls:
    - name: query_order
      must_call: true
    - name: check_refund_policy
      must_call: true
      args_contains: { order_id: "*" }
  json_schema: null
assertions:
  - type: semantic
    threshold: 0.85
    reference: "超过7天通常不支持无理由退款，需人工审核"
  - type: llm_judge
    rubric: rubrics/refund_accuracy.md
    pass_score: 4
tags: [regression, refund]
```

设计要点：

- 用例是**声明式**的，不写代码；
- 工具断言只看"调了什么、参数是否匹配"，不依赖内部实现；
- Judge 的 rubric 独立成文件，便于版本化与校准。

---

## 5. 系统架构

```
┌──────────────┐     ┌──────────────────┐     ┌───────────────┐
│  CLI / CI    │────▶│  FastAPI 控制面   │────▶│  PostgreSQL    │
└──────────────┘     │  cases/runs/gates │     │  cases/runs/   │
                     └────────┬─────────┘     │  results       │
                              │               └───────────────┘
                     ┌────────▼─────────┐
                     │  Runner（并发池） │
                     │  adapter:        │
                     │  - LangGraph     │
                     │  - OpenAI Agents │
                     └────────┬─────────┘
                              │
                     ┌────────▼─────────┐
                     │  Assertion 引擎   │
                     │  exact/semantic/ │
                     │  tool-seq/judge  │
                     └────────┬─────────┘
                              │
                     ┌────────▼─────────┐
                     │  Reporter         │
                     │  compare / gate   │
                     └──────────────────┘
```

技术栈：Python 3.11、FastAPI、SQLAlchemy(async)、PostgreSQL、Alembic、pytest、Docker Compose、GitHub Actions、Next.js（最小页面）。

Adapter 接口保持极薄：

```python
class AgentAdapter(Protocol):
    name: str
    async def run(self, case: Case, config: dict) -> Trajectory: ...
```

---

## 6. API 设计

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/projects` | 注册被测项目与 adapter |
| POST | `/projects/{id}/case-sets` | 导入 YAML 用例集（带版本） |
| GET | `/projects/{id}/cases` | 用例列表与筛选 |
| POST | `/projects/{id}/runs` | 触发评测（可指定 case 标签） |
| GET | `/runs/{id}` | 运行详情（含逐用例轨迹） |
| POST | `/runs/{id}/baseline` | 设为基线 |
| GET | `/compare?baseline=&candidate=` | 回归对比 |
| POST | `/gates` | 创建门禁规则（通过率下降阈值等） |
| GET | `/projects/{id}/metrics` | 趋势指标 |
| POST | `/webhooks/github` | PR 触发评测并回评 |

---

## 7. 指标定义（简历数字都从这里来）

| 指标 | 定义 | 用途 |
|---|---|---|
| Pass Rate | 全部断言通过的用例数 / 用例数；断言通过率 = 通过断言数 / 断言总数（单列） | 主指标 |
| Regression Delta | candidate 通过率 − baseline 通过率 | 门禁判定 |
| New Failures | 基线上通过、候选失败的用例数 | 最有说服力的数字 |
| Flaky Rate | 参与重跑（N ≥ 3）的用例中，任一断言结果不一致的用例数 / 参与重跑用例数 | 稳定性 |
| Judge-Human Kappa | Judge 与人工评分的一致性（Cohen's κ） | Judge 可信度 |
| Cost per Case | Σ(input_token×输入单价 + output_token×输出单价) / 用例数 | 成本 |
| p95 Latency | 单用例耗时 95 分位 | 性能 |
| Gate Time | CI 中一次回归评测耗时 | 工程效率 |

Judge 校准流程：人工标注 ≥ 50 条 → 计算 κ → κ < 0.6 则修 rubric 或换模型 → 达标后才允许进入门禁。

---

## 8. 里程碑（按面试时间倒排）

### 阶段 1（11 月）：最小闭环

- 设计文档：数据模型、DSL、指标定义
- FastAPI 骨架 + Alembic 首个 revision（一次建全表）
- 单用例执行：YAML → adapter → trajectory → 结果落库
- 精确断言 + 语义断言

**验收**：CLI 一条命令跑 5 条用例，输出通过率与耗时。

### 阶段 2（12 月）：回归与门禁

- baseline/candidate 对比、New Failures 清单
- 工具轨迹断言 + LLM Judge + rubric 版本化
- GitHub Actions 接入，PR 自动评论
- Judge 人工标注 50 条并计算 κ

**验收**：故意改坏一个 Prompt，CI 能拦住并在 PR 中列出退化用例。

### 阶段 3（1 月）：稳定性、成本与可视化

- 同用例 N 次运行与 flaky 判定
- token/成本统计、p95
- 轨迹回放与两次运行 diff
- 最小前端：用例列表、运行详情、对比、趋势
- 开源：README、ADR ×3、release v0.1

**验收**：一份带数字的评测报告 + 可访问 demo。

### 阶段 4（2 月）：展示与面试准备

- Docker Compose 一键启动、英文 README
- demo 录屏（3 分钟：改 Prompt → 跑回归 → 拦截）
- 两篇技术文章（Judge 校准、flaky 归因）
- 系统设计与八股复习

**验收**：陌生人按 README 能在 10 分钟内跑起来。

### 阶段 5（3–4 月）

- 投递与面试；只修 bug，不加功能。

---

## 9. 简历写法

> **EvalOps：Agent 评测与回归平台｜个人开源项目**
> - 从零设计用例 DSL 与执行器，支持精确/语义/工具轨迹/LLM-as-Judge 四级断言；Judge 与人工标注一致率 κ=?（填实测），覆盖 N 条用例。
> - 实现 baseline/candidate 回归对比与 GitHub Actions 门禁，PR 自动输出退化用例；累计拦截 Z 次 Prompt/模型变更导致的回归，New Failures 定位到用例级。
> - 建立 flaky 率、p95 延迟、单用例成本指标；定位并归因 K 条不稳定用例（温度/工具超时/上下文截断），单次回归耗时从 X 降至 Y。

> 与前两个项目的关系一句话：生成与合规 → 检索与推荐 → **评测与回归**，第三个补齐"效果可证明"这一环。

---

## 10. 面试素材

### 三个可深挖故事

1. **Judge 怎么可信**：人工标注 → κ 计算 → rubric 迭代 → 门禁阈值设定；回答"为什么不用 GPT 打分就完事"。
2. **flaky 怎么归因**：固定温度/种子、工具响应录制回放、按失败阶段聚类，区分模型随机性与外部依赖抖动。
3. **回归门禁怎么不误伤**：阈值 + New Failures 双条件、基线版本化、抽样重跑。

### 高频追问准备

| 问题 | 回答要点 |
|---|---|
| 和 Langfuse/LangSmith 有什么区别？ | 它们是观测与追踪，EvalOps 是"可执行的门禁"：用例、断言、基线、回归判定、CI 拦截。 |
| 为什么自己做而不是用 promptfoo？ | 先说明调研过；差异在工具轨迹断言、Judge 校准流程、与内部 Agent 的 adapter。 |
| 怎么保证评测稳定？ | 录制回放 + 温度 0 + 多次运行统计 + flaky 单列，不把不稳定当失败。 |
| 成本怎么算？ | token × 单价 × 用例数，按模型分档；门禁支持成本上限。 |

---

## 11. 风险与砍刀清单

**风险**

1. 范围失控（最可能）：严格按"不做清单"执行。
2. 适配两个框架耗时：只做 LangGraph + 一个薄适配，够讲抽象即可。
3. Judge 不稳定：先校准再上门禁。
4. 前端陷阱：只做 4 个页面，不做设计系统。

**时间不够时的砍刀顺序**

前端美化 > 第二个 adapter > Docker > 成本模块 > 英文文档。

**不能砍**：用例 DSL、回归对比、CI 门禁、Judge 校准、指标报告。

---

## 12. 开始前的三件事

1. 新建独立仓库，**不复制 InsightFlowX 代码**；只借鉴"断点续跑/幂等"的设计经验。
2. 先写 1 页设计文档（本文可裁剪），再写代码。
3. 第一个 commit 就带 CI（lint + pytest），保证从第一天起有绿色徽章。

---

## 13. 被测系统（SUT）接入设计

### 两个 SUT，一套平台

```
EvalOps（开源主体）
├── SUT-A: demo-loan-copy-agent（开源 / 合成数据 / 可一键启动）
└── SUT-B: 公司贷款文案 Agent（私有 / 内网 / 仅本地接入，不进仓库）
```

- **SUT-A** 用于让陌生人能跑起来、让 CI 有对象、让 demo 可录制。数据全部合成：假产品名、假利率、假额度。
- **SUT-B** 用于产生真实业务结论（合规违规率、数值准确率），只在本地/内网接入。仓库中只保留 adapter 接口，不保留任何 Prompt、业务词表、真实数据。

### Adapter 契约

```python
class AgentAdapter(Protocol):
    name: str
    async def run(self, case: Case, config: dict) -> Trajectory: ...
```

接入配置（`projects/sut-b.yaml`，此文件不入库，提供 `example` 版本）：

```yaml
name: loan-copy-agent
adapter: langgraph
entrypoint: "app.agents.copy_agent:build_graph"
config:
  model: deepseek-chat
  temperature: 0
  retrieve_top_k: 5
  token_budget: 6000
secrets:
  api_key: ${SUT_API_KEY}
```

约束：

1. adapter 不感知业务，只负责"跑一次并返回标准轨迹"；
2. 轨迹必须包含 `step / type / name / input / output / tokens / latency_ms`；
3. 工具返回统一走录制层，保证可回放（录制模型见 §19.3）。

---

## 14. 合规断言定义（EvalOps 的差异化能力）

通用评测工具擅长打分，不擅长业务硬规则。以下六类断言是自研的核心价值（四类硬规则可进 CI 门禁，两类软指标只做趋势）：

| 断言类型 | 输入 | 判定 | 是否可进 CI 门禁 |
|---|---|---|---|
| `forbidden_terms` | 文案 + 词表文件 | 命中即 fail（零容忍） | 是 |
| `numeric_consistency` | 文案 + 业务数据源 | 提取数字与 DB 实际值逐项比对 | 是 |
| `template_completeness` | 文案模板 + 填充结果 | 占位符集合与填充键集合完全一致 | 是 |
| `tag_hit` | 用户标签 + 召回文案标签 | 命中率 ≥ 阈值 | 是 |
| `semantic` | 文案 + 参考表述 | 余弦相似度 ≥ 阈值 | 否（软指标） |
| `llm_judge` | 文案 + rubric | 分数 ≥ 阈值，需 κ 校准 | 否（软指标） |

用例示例：

```yaml
name: 高额度产品文案-合规校验
input:
  user_tags: [高净值, 短期周转]
  product_id: P-1001
expected:
  forbidden_terms: compliance/banned_terms.txt
  numbers:
    source: db:products/P-1001        # 期望值与业务库一致
    fields: [rate, quota, term]
  template_placeholders: [rate, quota, term, product_name]
assertions:
  - type: numeric_consistency
  - type: template_completeness
  - type: forbidden_terms
  - type: llm_judge
    rubric: rubrics/copy_persuasiveness.md
    pass_score: 4
tags: [compliance, regression]
```

由这些断言直接产出的指标：**合规违规率、数值准确率、占位符完整率、标签命中率、κ**，全部可写进简历，且都能追溯到具体用例。

---

## 15. 知识产权与合规边界

必须遵守的红线：

1. 公开仓库**不出现**公司源码、Prompt、业务词表、真实利率/额度数据、内部系统地址。
2. 公司相关配置一律用 `${ENV}` 占位，`.env` 进 `.gitignore`。
3. 面试只讲**方法与指标**，不展示业务数据与代码。
4. 若使用公司系统跑出的数字写进简历，必须确认不违反保密协议；不确定就用相对值（如"违规率从 X 降至 0"）或脱敏表述。
5. 开源部分只依赖公开可获取的模型与合成数据。

---

## 16. 替代方案对比与 ADR-001

### 替代方案对比（同时作为 README 一节）

| 类别 | 代表项目 | 覆盖能力 | 本项目补的缺口 |
|---|---|---|---|
| CLI 断言 | promptfoo | YAML 配置、CI、红队 | 工具轨迹断言、业务硬规则、回放 |
| 指标库 | DeepEval、Ragas | G-Eval、幻觉、RAG 指标 | 基线/门禁/成本阈值、轨迹级判定 |
| 平台型 | LangSmith、Langfuse | 追踪、数据集、在线评测 | 私有化部署、CI 决策层、框架无关 adapter |
| 基准框架 | Inspect、τ-bench | Agent 任务基准 | 日常回归、业务规则校验 |

**分层复用原则**：打分与指标复用现成库（可选依赖 DeepEval），自研只做编排、轨迹录制回放、硬规则断言、Judge 校准与 CI 门禁。

### ADR-001：为什么自研而不是直接用 promptfoo

- **背景**：需要在私有环境对业务 Agent 做回归评测，硬约束包括合规词、数值一致性、模板完整性。
- **备选**：promptfoo（CLI 断言）、DeepEval（指标库）、LangSmith/Langfuse（平台）、直接自研。
- **决策**：采用"分层复用"——指标与打分优先复用，编排/回放/门禁自研。
- **理由**：① 私有化与数据不出域；② 内部 Agent 轨迹格式需要 adapter；③ 现成工具不覆盖"降多少算退化、谁能合并、成本超限怎么拦"这套决策流程。
- **后果**：需要维护 adapter 与断言层；换来可解释的门禁与可复现的失败归因。
- **不选纯自研的理由**：重复实现指标库没有工程价值，简历上也无法自证。

---

## 17. 通用性设计与扩展点

### 17.1 设计立场

**评测流程通用，业务语义不通用。** 平台不"理解业务"，只负责把业务判断下沉成可声明的三类输入：真值从哪来、用哪种断言、阈值多少。

一句话：不追求"一个平台测所有业务"，追求"新增一个业务只加配置和插件，不动核心"。

### 17.2 通用性的四个层次

| 层次 | 是否通用 | 说明 |
|---|---|---|
| 执行与轨迹 | 通用 | adapter 只有 `run(case) -> trajectory`；LangGraph / OpenAI Agents / HTTP / 录制回放四种实现 |
| 断言引擎 | 通用框架 + 可插拔 | 内置 exact / contains / json_schema / regex / tool_seq / latency / cost；业务规则走插件 |
| 判定与门禁 | 通用 | New Failures + Regression Delta + 成本上限 + 基线版本化，与业务无关 |
| 业务语义 | 不通用，也不假装通用 | 词表、rubric、真值来源、多轮用户模拟，全部由使用者声明 |

### 17.3 三个扩展点（不再开放第四个）

1. **`projects/<name>.yaml`**：接入方式与运行参数

```yaml
name: loan-copy-agent
adapter: langgraph            # langgraph | openai_agents | http | fake
entrypoint: "app.agents.copy_agent:build_graph"
config:
  model: deepseek-chat
  temperature: 0
  retrieve_top_k: 5
  token_budget: 6000
secrets:
  api_key: ${SUT_API_KEY}
```

2. **`cases/*.yaml`**：用例与断言声明（格式见 §4），用例是数据不是代码。

3. **断言插件**：只有内置断言覆盖不了时才写，代码如下（新增业务规则的标准形态）：

```python
class NumericConsistency:
    name = "numeric_consistency"

    async def check(self, result: CaseResult, expected: dict) -> Verdict:
        actual = extract_numbers(result.output)
        truth = load_business_values(expected["source"], expected["fields"])
        diffs = [f"{k}: {actual.get(k)} != {truth.get(k)}" for k in truth if actual.get(k) != truth[k]]
        return Verdict(passed=not diffs, detail={"diffs": diffs})
```

**明确不做的第四个扩展点**：允许在 YAML 里写任意脚本。评测结果必须可复现、可审计，用例文件里执行任意代码会同时引入安全和复现性问题；需要复杂逻辑就写插件，插件入库、可 review、可单测。

### 17.4 新增场景的验收标准

写进 README，并作为集成测试：

> 接入一个新业务 Agent ≤ 1 份 `project.yaml` + ≤ 50 行插件，核心目录零改动。

用三个场景自证：

| 场景 | 真值来源 | 主断言 | 产出指标 |
|---|---|---|---|
| 贷款文案（SUT-A/B） | 业务库 + 词表 + 模板 | `forbidden_terms` / `numeric_consistency` / `template_completeness` | 合规违规率、数值准确率、占位符完整率 |
| 信贷助手（SUT-B） | 工具响应录制 + 期望调用序列 | `tool_seq` / `json_schema` | 工具正确率、平均步骤数 |
| RAG 问答（公开示例） | 参考文档 + rubric | `semantic` / `llm_judge` | 引用支撑率、Judge κ |

规则：如果新场景必须改核心代码，说明抽象漏了，先改设计再写功能，不允许把业务分支塞进 runner/assertion 引擎。

### 17.5 明确不承诺的边界

| 能力 | 处理方式 |
|---|---|
| 多轮对话 | 支持 messages 序列与按轮断言；不内置用户模拟器，模拟器作为 adapter 接入 |
| 多 Agent 协作质量 | 轨迹按 step 记录；"协作好不好"无通用定义，只能进 Judge 插件 |
| 长时任务 | 支持超时/取消/断点，但不做调度平台 |
| 业务真值 | 平台只保证"和声明的真值一致"，真值维护归使用方 |
| 跨项目分数 | 不支持跨项目比较 Pass Rate，只做同项目跨 run 对比 |

### 17.6 通用性追问与答法

| 追问 | 回答要点 |
|---|---|
| 每个业务场景都不一样，怎么测？ | 流程通用、语义下沉；真值/断言/阈值三类声明由使用者提供 |
| 那你不还是要写代码？ | 写的是插件不是平台；80% 场景内置断言 + YAML 即可，插件是逃生舱 |
| 不同场景的分数可比吗？ | 不做跨项目比较，只在同项目内做 baseline vs candidate |
| 工具时序和超时怎么测？ | `tool_seq` 支持 must_call / order / args_contains / max_calls；超时作为轨迹字段参与断言 |
| 能测非 Agent 吗？ | 能（实现 adapter 即可），但不是目标场景 |

---

## 18. 与现有开源项目的关系

### 18.1 定位

**不是替代品，是判定层。** 观测与指标继续用现成工具；EvalOps 只补"从用例到合并决策"这一层。

### 18.2 工具分工

| 类别 | 代表 | 覆盖 | 本项目补的缺口 | 使用方式 |
|---|---|---|---|---|
| CLI 断言 | promptfoo | YAML 配置、断言、CI、红队 | 轨迹回放、业务硬规则、基线/门禁判定 | 可作为上游结果来源 |
| 指标库 | DeepEval、Ragas | G-Eval、幻觉、RAG 指标 | 编排、成本阈值、轨迹级判定 | 作为可选依赖复用打分 |
| 观测平台 | LangSmith、Langfuse | trace、数据集、在线评测 | 私有化门禁决策、框架无关 adapter | 开发期继续用其调试能力 |
| 基准框架 | Inspect、τ-bench | 任务基准与 agent 能力评估 | 日常回归、业务规则校验 | 不替代 |

### 18.3 60 秒答辩脚本

> 我不是因为现成工具"不行"才自己做，而是我的问题是评测结果要直接决定"这个 PR 能不能合"，现成工具大多停在"跑分/看板"，没停在这个决策点。
>
> 我遇到三段拼不起来的能力：第一，业务硬规则（禁词零命中、利率与业务库逐项一致、模板占位符不能漏），这不是打分；第二，决策层（New Failures + 通过率跌幅双条件、基线版本化、成本上限），从"失败"到"能不能合"的规则要自己写；第三，稳定性与成本（同用例多次结果不一致时区分模型随机性与工具抖动），靠录制回放解决。
>
> 所以做法是分层复用：打分与指标优先复用 DeepEval，自研只做编排、回放、硬规则断言、Judge 校准和 CI 门禁。优势不是指标更准，而是从用例到 PR 拦截的闭环完整，并且能在私有环境跑。

### 18.4 提 PR 策略（回答"为什么不给 promptfoo 提 PR"）

判断标准：**能作为插件/适配器接进去的提 PR，会改变项目定位的自研。**

| 需求 | 处理 |
|---|---|
| 新增一种断言类型 | 提 PR |
| 读取其导出结果做对比门禁 | 做适配层，不改上游 |
| 轨迹录制回放、Judge 校准流程、决策层 | 自研，不向上游强推 |

同时说明：EvalOps 以"promptfoo 的下游判定层"设计，接入方式是消费其结果，而不是替换。

### 18.5 追问：为什么不直接用 LangSmith / Langfuse

> 它们强在观测，我不会重造。但我要的是**合并决策**：哪几条用例退化、够不够格阻断、成本是否超限。观测回答"发生了什么"，门禁回答"允不允许发生"。
>
> 另外两个现实约束：数据不出域（公司 Agent 的输入输出不能进第三方 SaaS）；框架耦合（被测 Agent 不在同一生态，需要薄 adapter）。Langfuse 自托管能解决第一条，但门禁规则、业务硬断言、Judge 校准仍要自己写，所以这层单独做成可复用的，观测继续用 Langfuse。

保留各自不可替代的能力：Langfuse 自托管（trace/成本看板/数据集）、LangSmith（LangGraph 调试）、EvalOps 只做判定层。

### 18.6 追问：为什么不直接用 DeepEval

> 指标层就是用它的（可选依赖）：G-Eval、幻觉、RAG 指标不自己实现。它缺的是编排与决策：用例版本化、跨 run 对齐、baseline 对比、PR 门禁。这些不是指标，是流程。

分层复用的边界：**打分/指标复用，编排/回放/门禁自研。**

### 18.7 用数字证明（比形容词有力）

项目跑起来后填实测值：

| 断言 | 数字 |
|---|---|
| 改坏一个 Prompt，CI 阻断耗时 | X 分钟 |
| PR 中列出的 New Failures | N 条 |
| Judge 与人工 κ | 0.7x（低于 0.6 的 rubric 不进 CI） |
| 轨迹回放带来的回归耗时下降 | X → Y |
| 单用例成本 | Z 元 |

### 18.8 降级承诺（加分句）

> 如果哪天 promptfoo 或 DeepEval 把门禁层做进去了，我会把 EvalOps 降级成 adapter，删掉自研部分——我维护的是决策规则，不是工具本身。

### 18.9 三种会被扣分的回答

- "现有开源都不好用"（显得没调研，且经不起追问）
- "我自己写一个更好"（没说清好在哪一层）
- "为了简历上有第三个项目"

---

## 19. 设计评审结论与开工前必补项

### 19.1 评审结论

方向、边界、价值主张成立；**工程契约层不足**，直接开工会返工。以下 4 项致命、5 项重要，补完再进阶段 1。

### 19.2 致命项（必须解决）

**（1）用例身份无法跨 run 对齐**

问题：`case` 挂在 `case_set` 下，每次导入 YAML 生成新 case_set → 新 case_id，baseline 与 candidate 的 case_id 全不同，回归无法计算。

解决：
- 用例引入稳定 `case_key`（默认取 `name`，可显式覆盖），唯一约束 `(case_set_id, case_key)`；
- 回归按 `case_key` 对齐，而非 `case_id`；
- 新增用例（只在 candidate）单列"新增"，不计入 New Failures；
- 删除用例单列"移除"，门禁可配置"不允许删除用例"；
- 重命名视为删除 + 新增，需显式 `key` 覆盖或 alias 声明。

**（2）门禁表缺失**

API 有 `POST /gates`，模型里没有 `gate`。已补入 §3：

```yaml
# gate.rules 示例
max_pass_rate_drop: 0.02      # 允许的最大通过率跌幅
max_new_failures: 0          # 任何 New Failure 直接阻断
max_cost_increase: 0.20      # 成本涨幅上限
require_kappa: 0.6           # Judge κ 低于此值不允许进门禁
```

verdict 枚举：`pass / block / warn`。

**（3）陌生人跑不起来**

SUT-A 需要 LLM key，阶段 4 的"10 分钟跑起来"必然失败。

解决：内置 `adapter: fake` + `fixtures/*.json` 录制轨迹 + 3–5 条 golden 用例，零密钥可跑完整链路（run → compare → gate）；真实模型作为可选路径。

**（4）adapter 动态加载 = 远程代码执行**

`entrypoint: "app.agents.copy_agent:build_graph"` 等于任意模块 import。

解决：
- `adapter` 限定白名单：`langgraph | openai_agents | http | fake`；
- `entrypoint` 只允许项目目录内、且必须存在对应函数；
- 高风险场景跑在子进程，限制超时与环境变量；
- API 鉴权：管理端与触发端分离的 key；
- GitHub webhook：校验 `X-Hub-Signature-256`，按 `delivery_id` 幂等（已补 `webhook_event` 表），fork PR 不注入 secrets。

### 19.3 重要项

**（1）指标公式**：已修正（§7）。Pass Rate 改为用例口径、断言通过率单列；Cost per Case 区分 input/output 单价；Flaky Rate 明确分母为参与重跑用例数。

**（2）Judge 校准闭环**：`judge_label` 绑定 `case_result_id`（哪一次输出被标注）；同一用例 Judge 调 3 次取中位并记录 rationale；κ ≥ 0.6 才允许进门禁；rubric 版本化并入库。

**（3）semantic 断言离线可跑**：显式声明 embedding 来源；提供 `mock_embedding`（固定向量/哈希）用于演示与 CI，真实模型作为可选依赖，README 标注"演示模式"。

**（4）执行层设计**：run 状态机 `queued → running → succeeded/failed/cancelled`；并发度、单用例超时、取消、幂等键（重复 POST 不重复跑）；断言失败不自动重试（避免掩盖 flaky），重跑是显式操作。

**（5）录制回放**：新增 `tool_recording` 表（已补 §3）；轨迹单步输出超阈值截断并标记 `truncated`；回放时按 `request_digest` 命中 fixture，未命中则 fail 并提示录制。

### 19.4 本文档已同步修正

- §3 数据模型：补 `case_key`、`gate`、`tool_recording`、`webhook_event`、`run.trigger/config_snapshot`、`judge_label.case_result_id`、`assertion_result.seq`
- §7 指标口径：Pass Rate / Cost per Case / Flaky Rate
- §14 表述：四类 → 六类（四硬两软）
- §13 交叉引用：录制层改指 §19.3

### 19.5 开工顺序（阶段 0，约 3 天）

1. 仓库骨架 + CI（ruff + pytest + Alembic 首版）
2. 完整 DDL（含 §3 全部表）
3. YAML DSL schema + 校验（含 `case_key` 规则）
4. `fake` adapter + 3 条 golden 用例 + 精确断言
5. CLI：`evalops run` / `evalops compare`，零密钥跑通
6. 再进入阶段 1（语义断言、真实 adapter）
