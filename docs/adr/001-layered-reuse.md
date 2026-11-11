# ADR-001：分层复用，自研编排与门禁

- 状态：已采纳（阶段 1）
- 关联：[项目方案 §16](../project-plan.md#16-替代方案对比与-adr-001)

## 背景

需要在私有环境里对内网业务 Agent 做日常回归：合规词、数值一致性、模板完整性、工具调用序列都要能判定，
并且要回答"通过率降多少算退化、谁能合并、成本超限怎么拦"。数据不能出域，评测结论要可追溯到具体用例与断言。

## 备选方案

| 方案 | 能力覆盖 | 主要问题 |
|---|---|---|
| promptfoo | YAML 断言、CI、红队 | 无工具轨迹级断言、无业务硬规则插件模型、不做基线决策 |
| DeepEval / Ragas | G-Eval、幻觉、RAG 指标 | 指标强，缺 baseline/门禁/成本阈值与轨迹级判定 |
| LangSmith / Langfuse | 追踪、数据集、在线评测 | 观测为主，决策层与私有化适配不足 |
| 全自研 | 完全可控 | 重复实现指标库，工程价值低且难以自证 |
| **分层复用 + 自研编排** | 指标复用、编排自研 | 需维护 adapter 与断言层 |

## 决策

1. **指标与打分复用**：semantic / Judge 等打分能力按需接入成熟实现，平台不重复造指标库；
   当前默认 `mock` provider 仅用于零密钥演示与 CI，可在 `project.yaml` 中切换到 OpenAI 兼容接口。
2. **自研四件事**：执行编排（adapter → trajectory）、轨迹录制回放、硬规则断言、Judge 校准与 CI 门禁。
3. **被测系统只暴露一个薄契约**：`run(case, config) -> Trajectory`，框架差异（LangGraph / OpenAI Agents / HTTP）
   全部收敛在 adapter 内，业务 Prompt 与状态拼装留在被测项目侧（≤50 行）。

## 后果

正面：

- 门禁结论可解释：每条 block 都能落到"哪个 case、哪条断言、期望与实得"；
- 失败可复现：fixture 回放让回归结论不依赖外部模型与网络；
- 换框架不改核心：新增接入方式只新增 adapter，不动 runner / assertion / regression。

负面与约束：

- 需要长期维护 adapter 与断言层，`json_schema` 等能力目前仍未支持；
- `mock` provider 的分数不能代表真实语义或真实评审质量，指标里必须按"演示模式"标注；
- 不承诺跨项目比较 Pass Rate，只做同项目跨 run 对比。
