# 接口与兼容性

接口依据：2026-09-20 的 [TypeSafe/Jev API 文档](https://docs.typesafe.ai/api) 和 [openjev-sglang](https://github.com/ekzhang/openjev-sglang)。本项目使用 Chat Completions API 作为推理后端。

## 核心端点

`POST /v1/systemone` 接收 `model`、`state`、`questions`。`state` 和每个问题的 `instructions` 接受字符串、对象或数组。问题 ID 原样对应响应 ID，不参与提示词推理。

| 问题 | criteria | 响应 |
| --- | --- | --- |
| Noul | 可省略；true/false 描述，各可为字符串、对象或数组 | `{type: "noul", noul: P(Yes)}` |
| Choice | 选项键到描述的对象；null 描述回退到键 | `type`、`choice`、`probabilities`、`confidence` |
| Score | 有序描述数组，2–10 个等级 | `type`、期望分 `score`、完整 `legend`、`probabilities`、`confidence` |

响应顶层是 `model`、`answers`、`usage`。`model` 保留调用方使用的受支持名称。`usage.input_tokens`、`usage.output_tokens` 汇总实际上游用量，包括多问题和双循环；输入 token 包含供应商报告的缓存输入。

Score 等级从 0 开始，结果可以是小数。结构化等级描述在 legend 中序列化为 JSON 字符串。Choice 和 Score 的 confidence 使用 `1 - H(p)/log(n)`，反映分布集中程度，取值为 0–1。这是本实现采用的计算公式。

每个 HTTP 响应通过 `x-typesafe-request-id` 关联日志，`x-jev-config-id` 标识有效配置。诊断不混入标准 answers。

## 辅助端点

| 路由 | 用途 |
| --- | --- |
| `/v1/models` | 支持 `jev-latest` 和配置模型名；同时提供 TypeSafe 与 OpenAI 风格列表 |
| `/v1/limits` | 本实例真实的题数、候选数、字节数、调用数和并发上限 |
| `/health/live` | 进程存活 |
| `/health` | 检查本地配置及密钥是否就绪；上游连通性可通过 `evaluate` 验证 |
| `/docs`、`/openapi.json` | 交互文档和接口结构 |
| `/` | 服务信息 |

## 错误

请求字段不合法、未知模型、候选数或展开后的调用数超限，都在推理前返回 422。过大的请求体返回 413；本服务鉴权失败返回 401；批次容量已满返回 529；上游限流返回 429；超时返回 504；上游认证、无有效概率等失败返回网关错误。错误消息提供状态与原因摘要。

成功响应包含全部问题的结果。任一问题失败时，整个批次返回错误，剩余任务取消。重试由调用方发起，并作为新请求计费。

## 与官方服务、SGLang 版本的差异

1. **概率获取方式**：读取 top-k 及实际采样 token。缺失目标按 0 近似；诊断提供目标质量上下界和缺失列表。目标全缺失时返回错误。
2. **候选容量**：默认 A–Z，最多 26 个候选；非空 callsigns 列表决定实际容量，最多 255。Score 最多 10 级。候选数较多时，需结合缺失标签诊断评估 top-k 的覆盖情况。`/v1/limits` 公布本实例容量。
3. **证据布局**：聊天记录完整序列化为证据，消息角色与布局由提示词模板决定。
4. **数值语义**：结果取决于上游模型、提示词和温度；双循环输出归一化锦标赛胜分。Choice 平局时按输入选项顺序选取。
5. **输入预算**：本地限制请求字节和展开调用数；上下文 token 上限由上游服务检查。

配置加载与推理核心也可作为 Python 模块使用。
