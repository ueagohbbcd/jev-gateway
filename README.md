# Jev Gateway

把支持首 token `logprobs` 的 Chat Completions API 封装成 Jev 风格的类型化决策服务。业务侧传 `state` 和 `questions`，得到 Noul、Choice、Score；服务侧用一份 TOML 配置模型、提示词、双循环、呼号和温度缩放。

无需 GPU、SGLang、完整词表或选定-token 查询接口。服务不生成解释，只请求一个输出 token，从返回分布读取目标标签。这个仓库独立于早期实验室，没有前端构建步骤，也不依赖父目录。

兼容的是公开的请求／响应结构，不是官方模型的判断质量、内部评分公式或全部容量限制。具体边界见 [接口与兼容性](docs/api.md)。

## 启动

需要 Python 3.11 或更新版本。

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 用 .venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

把上游密钥放进环境变量。PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
jev-gateway check --config config.toml
jev-gateway serve --config config.toml
```

macOS / Linux 用 `export DEEPSEEK_API_KEY='你的密钥'`。示例默认连接 DeepSeek，并显式禁用思考；换供应商时修改 `upstream`，删除对方不支持的 `extra_body` 字段。模型必须支持首输出 token 的 logprobs，且单个输出 token 足以完成读数。

服务默认监听 `127.0.0.1:8080`。打开 `/docs` 可以交互查看 HTTP 接口；它是自动生成的接口文档，不是另一套执行逻辑。

```sh
curl http://127.0.0.1:8080/v1/systemone \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request.json
```

PowerShell 可用 `curl.exe`，把命令写在一行。业务客户端的 base URL 指向本服务，model 使用 `jev-latest` 或配置中的上游模型名。一个服务实例使用一份配置，不根据请求动态切换上游账户。

## 一份配置，一套执行逻辑

[config.toml](config.toml) 是可以直接修改的完整起点。影响推理的核心配置只有：

```toml
[adapter]
temperature = 1.0
double_round_robin = false
callsigns = []

[prompt]
system = "Evaluate the evidence. Reply with exactly one allowed token."
user = """
Evidence:
{{state}}

Question:
{{instructions}}

Options:
{{options}}

{{output}}
"""
```

`temperature` 是概率后处理温度，不是 API 采样温度。空呼号列表使用 A–Z；非空列表替换这些字母。Noul 始终直接读取 `Yes`／`No`，不参与呼号映射或双循环。双循环用于 Choice 和 Score，每对候选交换位置各调用一次。

提示词不需要学习 JSON 模板、表达式或函数语言。只有四个文本占位符；重复放置就是复读，调整顺序就是布局变更。变量值只替换一次，证据中的占位符不会再次执行。

配置无继承、无预设覆盖顺序、无热更新。省略项使用代码默认值，未知配置项在启动前报错。修改后重启服务；每次响应携带配置摘要 `x-jev-config-id`，方便关联实验。配置文件只引用密钥环境变量名，不保存密钥值。

独立示例：[温度缩放](examples/calibrated.toml)、[呼号](examples/callsigns.toml)、[双循环](examples/round-robin.toml)。每份都可直接通过 `--config` 使用。字段说明见 [配置指南](docs/configuration.md)。

## 不启动 WebUI 也能调试

```sh
# 离线检查，不需要密钥，不发送请求
jev-gateway check --config config.toml

# 展开全部实际提示词和映射，查看双循环会发多少请求
jev-gateway preview --config config.toml --request examples/request.json

# 真实调用；stdout 是 Jev 响应，可选保存完整诊断
jev-gateway evaluate --config config.toml --request examples/request.json \
  --diagnostics diagnostics/example.json
```

预览、命令行运行和 HTTP 服务共用同一个计划、解析和聚合实现。完整诊断包含提示词、原始概率及映射，可能含输入隐私；只在明确指定路径时写入，本仓库默认忽略 `diagnostics/`。

HTTP 进程把结构化事件写到 **stderr**，附请求 ID、配置 ID、概率质量、缺失标签数量和告警，不写输入正文、完整提示词或 API key。正常归一化本身不是告警。默认不保存用户请求、不自动重试，也不因告警额外调用模型。

## 概率语义

原始目标质量高，说明模型愿意按要求输出标签，不代表答对概率高。缺失 top-k 标签按 0 近似，诊断始终保留缺失标记和归一化前质量上下界；如果一个目标标签都读不到，返回错误，不能制造均匀分布。

温度在**每次请求的目标标签归一化时**应用。单次推理不改变选项排序；双循环先分别缩放，再按语义对齐、合并两种顺序，最后归一化期望胜分。因此双循环下的最终分布是锦标赛评分近似，不是一次模型调用的联合概率。

旧实验完整分布子集的分组校准温度约为 2.8，但使用的是旧提示词和字母协议；本项目默认仍为 1。不要把该经验值当作新模型或新提示词的通用校准常数。公式、告警与重放边界见 [概率与诊断](docs/probabilities.md)。

## 开发与验证

```sh
python -m pytest
```

测试使用本地模拟上游，不花模型费用。代码按配置、公开类型、纯推理核心、异步转发服务、HTTP、CLI 分层；不提供通用插件系统或配置文件内代码执行。

首次交付通过 60 项离线测试，并完成三个短问题的真实链路检查。具体范围、用量和限制见 [验证记录](docs/verification.md)。

项目参考 [Jev HTTP API](https://docs.typesafe.ai/api) 和 [openjev-sglang](https://github.com/ekzhang/openjev-sglang) 的接口思路，独立实现普通 Chat API 后端。后者能直接查询选定 token，本项目用提示词约束与可观测的 top-k 近似替代该能力，不声称两种读数完全等价。
