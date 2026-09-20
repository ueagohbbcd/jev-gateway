# 接口与兼容性

核对依据：2026-09-20 的 [TypeSafe/Jev API 文档](https://docs.typesafe.ai/api) 和用户指定的 [openjev-sglang](https://github.com/ekzhang/openjev-sglang)。本项目独立实现，没有部署或依赖 SGLang。

## 核心端点

`POST /v1/systemone` 接收 `model`、`state`、`questions`。`state` 和每个问题的 `instructions` 接受字符串、对象或数组。问题 ID 原样对应响应 ID，不参与提示词推理。

| 问题 | criteria | 响应 |
| --- | --- | --- |
| Noul | 可省略；true/false 描述，各可为字符串、对象或数组 | `{type: "noul", noul: P(Yes)}` |
| Choice | 选项键到描述的对象；null 描述回退到键 | `type`、`choice`、`probabilities`、`confidence` |
| Score | 有序描述数组，2–10 个等级 | `type`、期望分 `score`、完整 `legend`、`probabilities`、`confidence` |

响应顶层是 `model`、`answers`、`usage`。`model` 保留调用方使用的受支持名称。`usage.input_tokens`、`usage.output_tokens` 汇总真实上游用量，包括多问题和双循环；输入 token 包含供应商报告的缓存输入。这里没有 SGLang 预热调用，不人为增加 N+1 的用量。

Score 等级从 0 开始，结果可以是小数。结构化等级描述在 legend 中序列化为 JSON 字符串。Choice 和 Score 的 confidence 使用 `1 - H(p)/log(n)`，反映分布集中程度。官方未公开相同的精确公式，因此只保证字段结构和取值范围，不宣称 confidence 数值等价。

每个 HTTP 响应通过 `x-typesafe-request-id` 关联日志，`x-jev-config-id` 标识有效配置。诊断不混入标准 answers。

## 辅助端点

| 路由 | 用途 |
| --- | --- |
| `/v1/models` | 支持 `jev-latest` 和配置模型名；同时提供 TypeSafe 与 OpenAI 风格列表 |
| `/v1/limits` | 本实例真实的题数、候选数、字节数、调用数和并发上限 |
| `/health/live` | 进程存活 |
| `/health` | 本地配置及密钥是否就绪；不发推理请求，也不保证上游账户或网络正常 |
| `/docs`、`/openapi.json` | 交互文档和接口结构 |
| `/` | 服务信息 |

无通用聊天透传端点、无模型管理端点、无前端专用的另一套推理行为。

## 错误

请求字段不合法、未知模型、候选数或展开后的调用数超限，都在推理前返回 422。过大的请求体返回 413；本服务鉴权失败返回 401；批次容量已满返回 529；上游限流返回 429；超时返回 504；上游认证、无有效概率等失败返回网关错误。不会把上游的密钥、响应正文或用户输入塞进错误消息。

成功必须包含全部问题的结果。批次中途失败不会返回看似完整的部分答案，也不会自动重试已经计费的调用。调用方如果整体重试，可能重复产生费用。

## 与官方服务、SGLang 版本的差异

1. **概率获取方式**：本项目只能看到 top-k 及实际采样 token。缺失目标按 0 近似；目标质量上下界和缺失列表留在诊断。目标全缺失时失败。选定-token API 不需要这项近似。
2. **候选容量**：默认 A–Z，最多 26 个候选；非空 callsigns 列表决定实际容量，最多 255。Score 始终最多 10 级。高于 top-k 的候选数尤其容易缺分布；使用自定义标签不能消除这个限制。`/v1/limits` 公布实际容量，不声称默认支持官方 255 选项。
3. **证据布局**：聊天记录完整序列化为证据，而不是把记录中的 system/tool 角色提升成上游真实指令。模型看到的消息布局由模板决定。
4. **数值语义**：模型、提示词与读取机制不同，不保证相同答案；双循环分布是归一化锦标赛胜分，温度也会改变数值。Choice 的平局按输入选项顺序稳定选取。
5. **token 预算**：没有供应商 tokenizer，不能精确预检上下文 token 数；本地按请求字节和展开调用数限制，供应商仍可能拒绝超上下文请求。不会虚构 tokenizer 限制字段。

配置和纯核心均可单独使用。若以后恢复一个 playground，只需编辑同样的 TOML、调用同一个执行服务；不需要改变本协议。
