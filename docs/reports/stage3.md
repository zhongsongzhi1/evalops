# 阶段 3 评测报告：回归门禁、Flaky 与成本

> 数据集：`examples/` 合成用例（4 条，11 条断言）；被测系统：`fake` adapter（录制回放），
> 另有一组 `http` adapter 抖动实验。所有数字均由本仓库 CLI 实测产出，命令见文末附录 B。
> **Judge / 语义打分使用 `mock` provider，只验证链路，不代表真实评审能力。**

## 1. 结论摘要

| 指标 | 基线 | 退化候选 | 修复后候选 |
|---|---|---|---|
| Pass Rate | 100.0%（4/4） | **25.0%（1/4）** | 100.0%（4/4） |
| 断言通过率 | 100.0%（11/11） | 72.7%（8/11） | 100.0%（11/11） |
| New Failures | — | **3** | 0 |
| 单次运行成本 | $0.000849 | $0.000849 | $0.000849 |
| Cost per Case | $0.000212 | $0.000212 | $0.000212 |
| p95 延迟 | 1240 ms | 1240 ms | 1240 ms |
| 门禁判定 | — | **BLOCK（退出码 2）** | PASS（退出码 0） |
| 本地运行耗时 | 113 ms | 66 ms | 70 ms |

抖动实验中：同一用例集重跑 3 次，**Flaky Rate = 50.0%（2/4）**，两次运行 Pass Rate 在
100% 与 50% 之间摆动，但门禁侧只把它记为 flaky，不混入 New Failures。

## 2. 数据集与判定口径

| 用例 `case_key` | 断言数 | 覆盖能力 |
|---|---|---|
| `refund_policy` | 3 | 文本包含 / 排除、工具调用及参数、延迟上限 |
| `order_lookup` | 3 | 文本包含、正则、工具调用参数 |
| `out_of_scope` | 1 | 拒答话术包含 |
| `judge_refund_quality` | 1 | LLM Judge（rubric + 3 次采样取中位数） |

口径与 [README](../../README.md#指标口径) 一致：Pass Rate 按用例计，断言通过率单独统计，
成本按 `tokens × 单价` 计算（`price_in_per_1k = 0.0005`、`price_out_per_1k = 0.0015`），
p95 用最近秩法。

## 3. 基线

```text
用例 4 | 通过 4 | 失败 0 | Pass Rate 100.0% | 断言通过率 100.0%
成本 $0.000849 | 耗时 113ms | 状态 succeeded
```

| 用例 | 状态 | 延迟 | 成本 |
|---|---|---|---|
| `judge_refund_quality` | passed | 1100 ms | $0.000248 |
| `order_lookup` | passed | 860 ms | $0.000177 |
| `out_of_scope` | passed | 500 ms | $0.000135 |
| `refund_policy` | passed | 1240 ms | $0.000289 |

## 4. 退化候选（模拟一次 Prompt 改动）

在候选 fixture 中做了两处"看起来无害"的改动：

1. 退款答复删掉了「超过 7 天」这个关键信息；
2. 订单查询把工具从 `query_order` 换成了 `fetch_order`。

结果 3 条退化被逐条定位到断言，而成本、延迟完全没变——**只看成本或延迟会漏掉这次退化**：

| 用例 | 失败断言 | 定位信息 |
|---|---|---|
| `refund_policy` | `#0 contains` | `missing: ["7天"]` |
| `order_lookup` | `#2 tool_calls` | `未调用必需工具 query_order`，实调 `fetch_order` |
| `judge_refund_quality` | `#0 llm_judge` | 中位数 3.38 < 阈值 4.0（mock 相似度 0.594） |

轨迹 diff（`evalops diff --case refund_policy`）：

```text
- refund_policy: passed -> failed | 步骤变化 1 | 断言变化 1
    #4 changed
      output: 您的课程已购买 12 天，超过7天退款期，退款需要人工审核，请通过客服提交申请。
           -> 您的课程已购买 12 天，退款需要人工审核，请通过客服提交申请。
```

## 5. 门禁判定

```text
基线 52a8b5d8  Pass Rate 100.0%  断言通过率 100.0%
候选 3ca35208  Pass Rate 25.0%  断言通过率 72.7%
变化 -75.0% | 成本变化 +0.0%
New Failures (3): judge_refund_quality, order_lookup, refund_policy
门禁 default: BLOCK
  - New Failures 3 条，超过允许的 0 条
  - 通过率跌幅 75.0%，超过允许的 2.0%
```

`gate` 退出码为 2，CI 可直接按失败处理。两条规则独立给出理由，说明判定不是单阈值，
详见 [ADR-002](../adr/002-regression-gate.md)。

## 6. 修复后复检

把两处改动还原后重跑，同样过门禁：

```text
候选 d356e549  Pass Rate 100.0%  断言通过率 100.0%
门禁 default: PASS
  - New Failures、通过率跌幅与成本均在阈值内
```

修复运行的 `git_sha / prompt_version` 分别记为 `demo-fixed / v3-fixed`，与退化运行的
`demo-candidate / v2-regressed` 一起构成可追溯的三段记录。

## 7. Flaky 实验

`fake` adapter 是确定性的，重跑一定得到同一个结果（Flaky Rate 0%），无法说明稳定性能力。
因此另起一个约 40 行的 HTTP 桩，对 `refund_policy` 与 `order_lookup` **交替返回两种结果**，
模拟"外部依赖抖动"，再用 `--repeat 3` 重跑：

```text
运行 3 次 | 参与用例 4 | Flaky 2 | Flaky Rate 50.0%
  - order_lookup: passed -> failed -> passed（#2 tool_calls）
  - refund_policy: passed -> failed -> passed（#0 contains）
```

| 运行 | Pass Rate | 失败用例 |
|---|---|---|
| 第 1 次 | 100.0% | — |
| 第 2 次 | 50.0% | `order_lookup`、`refund_policy` |
| 第 3 次 | 100.0% | — |

关键点：`out_of_scope`、`judge_refund_quality` 同样参与了 3 次重跑且始终通过，被计入
`stable_cases`（2 条），**没有**被 flaky 率牵连。flaky 与 New Failures 是两个独立数字，
前者调阈值、后者拦退化。

## 8. 局限

- **样本量小**：4 条用例、11 条断言，只够验证机制，不能作为效果结论；
- **打分是演示实现**：`mock` judge 用字面相似度模拟评分（3.38 分即 0.594 相似度），
  `mock` embedding 同理，真实能力必须切换 OpenAI 兼容 provider 并完成 κ 校准；
- **延迟是 fixture 固定值**：p95 = 1240 ms 来自录制数据，不代表真实网络与模型耗时；
- **Judge 尚无人工标注**：本次未导入标注，κ 为 `null`，`require_kappa` 一旦开启会直接 block；
- **门禁耗时**：113 ms 是本地 `fake` adapter 的端到端耗时，不等于 CI 中的真实回归耗时。

## 附录 A：Flaky 演示用的 HTTP 桩

```python
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path("examples/fixtures")
LOCK, COUNTERS = threading.Lock(), {}

def respond(case_key: str) -> dict:
    payload = json.loads((FIXTURES / f"{case_key}.json").read_text(encoding="utf-8"))
    with LOCK:
        index = COUNTERS.get(case_key, 0)
        COUNTERS[case_key] = index + 1
    if index % 2 == 1:  # 奇数次请求返回"抖动"版本
        if case_key == "refund_policy":
            payload["output"] = "您的课程已购买 12 天，退款需要人工审核，请通过客服提交申请。"
        elif case_key == "order_lookup":
            payload["steps"] = payload["steps"][1:]  # 丢掉 query_order 工具步骤
    return payload

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps(respond(req["case_key"]), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", 9100), Handler).serve_forever()
```

对应的 `project.yaml`：

```yaml
name: demo-agent-flaky
adapter: http
config:
  endpoint: http://127.0.0.1:9100/run
  timeout_s: 10
  price_in_per_1k: 0.0005
  price_out_per_1k: 0.0015
```

## 附录 B：复现命令

```bash
# 独立数据库，避免污染本地 evalops.db
DB=stage3-report.db

# 1. 基线
evalops run -p examples/projects/fake.yaml -c examples/cases --db $DB --baseline

# 2. 候选：把 fixtures 复制一份，删掉退款答复里的「超过7天退款期」，
#    并把 order_lookup 的 query_order 改成 fetch_order，再用它的 project.yaml 跑
evalops run -p <candidate>/projects/fake.yaml -c examples/cases --db $DB \
  --git-sha demo-candidate --prompt-version v2-regressed

# 3. 对比与门禁（BLOCK 时退出码 2）
evalops compare --candidate <run_id> --db $DB
evalops gate --candidate <run_id> --gate examples/gates/default.yaml --db $DB

# 4. 轨迹 diff 与回放
evalops diff --baseline <baseline_run_id> --candidate <run_id> --case refund_policy --db $DB
evalops trace --run <run_id> --case refund_policy --db $DB

# 5. Flaky：先启动附录 A 的桩，再重跑 3 次
evalops run -p <flaky>/project.yaml -c examples/cases --repeat 3 --db $DB
evalops flaky --group <group_id> --db $DB

# 6. 指标趋势
evalops metrics --project demo-agent --limit 20 --db $DB
```
