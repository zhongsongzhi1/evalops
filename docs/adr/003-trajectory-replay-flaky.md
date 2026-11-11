# ADR-003：轨迹录制回放与 flaky 归因

- 状态：已采纳（阶段 3）
- 关联：[项目方案 §10 面试素材](../project-plan.md#10-面试素材)、[阶段 3 评测报告](../reports/stage3.md)

## 背景

Agent 评测有三个互相纠缠的不确定性来源：

1. 模型采样随机（温度、并行、上下文顺序）；
2. 外部工具抖动（超时、限流、返回字段缺失）；
3. 平台自身不可复现（环境变量、时区、依赖版本）。

如果每次失败都直接算"退化"，门禁必然误伤；如果一律重试到通过，又会把真实回归掩盖成"偶发"。
需要一套机制把"可复现的部分"和"不可复现的部分"分开。

## 备选方案

| 方案 | 说明 | 问题 |
|---|---|---|
| 失败自动重试 | 失败后再跑，直到通过或次数用尽 | 掩盖真实退化，回归结论不可信 |
| 只固定温度 / seed | 靠采样参数降低随机性 | 无法消除工具抖动，且模型侧不保证确定性 |
| 全链路网络录放（如 VCR） | 录制 HTTP 层流量 | 只能覆盖 HTTP，进程内工具调用、Agent 内部步骤录不到 |
| **轨迹级录制 + 显式重跑统计** | 按 step 录制工具响应，重跑用于统计而非掩盖 | 需要被测方按契约产出轨迹 |

## 决策

### 1. 录制：绑定到具体用例结果的轨迹层

- 只录制 `type=tool` 的步骤：`tool`、`step`、`request_digest`（对入参做规范化 JSON 的 SHA-256）、
  `response`、`truncated`、`source`；
- 单个响应序列化超过 **8 KB** 时只存 `{"truncated": true, "preview": ...}`（前 4000 字符），
  避免大响应把库撑爆，同时保留定位所需的信息；
- `source` 由 adapter 决定：`fake` 记为 `fixture`，其余记为 `live`，用于区分"回放产生"与"真实调用产生"；
- 录制与用例结果同事务落库，保证不会出现"有结果无轨迹"。

### 2. 回放：`fake` adapter 读 fixture

`<fixtures_dir>/<case_key>.json` 提供 `output / steps / tokens_in / tokens_out / latency_ms`，
评测算出的通过率、断言结果、成本与延迟全部来自同一份轨迹，因此**离线、零密钥、可进 CI**。
`case_key` 跨用例集版本稳定，fixture 因此可以像金样例一样被 review。

### 3. flaky：重跑是显式操作，统计而不是重试

- `evalops run --repeat N` 生成同一 `repeat_group` 的 N 次运行，`repeat_index` 记录次序；
- N ≥ 3 才计算 Flaky Rate：按 `case_key` 对齐 N 次结果，逐条断言以 `(seq, type)` 为键收集布尔序列，
  任一序列出现不一致、或状态在 `passed` 与非 `passed` 之间摆动，该用例即判为 flaky；
- `Flaky Rate = flaky 用例数 / 参与重跑用例数`，不足 3 次的用例进 `skipped`，不进分母；
- 断言失败不触发自动重试，避免把 flaky 洗成"偶发通过"。

### 4. 归因：先定位到断言，再定位到步骤

不稳定用例给出 `#seq + type + [true, false, ...]`；再用 `evalops diff --case <key>` 对比两次运行，
输出到步骤级的 `output` 变化。归因顺序固定为：

1. **断言类型**：`tool_calls` 不一致 → 外部工具/编排；`contains`/`semantic` 不一致 → 模型输出；
2. **步骤 diff**：确认差在 `plan`、某个 tool，还是最终 `answer`；
3. **外部依赖**：结合 `tool_recording.source` 与 `request_digest` 判断是同一请求返回不同响应，还是参数本身就变了。

## 后果

正面：

- 回归结论可复现：基线、候选、重跑三组数据都能离线重放；
- flaky 与退化被分成两个数字（Flaky Rate vs New Failures），门禁阈值可以分开调；
- 归因从"猜"变成"看某条断言、某个 step 的差异"。

负面与约束：

- 录制的是**被测方产出的归一化轨迹**，不是真实网络流量；若被测方改写轨迹，回放同样会失真；
- 平台不做种子/温度控制，这类参数由 `project.yaml` 声明并进入 `config_snapshot`，只能保证"记录了什么"；
- 超时类失败目前只体现为 `error` 文本，还没有独立超时分类；
- `truncated` 响应无法支撑逐字段 diff，只能看 preview。
