# Jev 模拟器

[![Offline tests](https://github.com/ueagohbbcd/jev-simulator/actions/workflows/test.yml/badge.svg)](https://github.com/ueagohbbcd/jev-simulator/actions/workflows/test.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Chat Completions + logprobs](https://img.shields.io/badge/backend-Chat_Completions_%2B_logprobs-475569)

**用普通 Chat API，提供 Jev 兼容的决策端点。**

把支持首 token `logprobs` 的 Chat Completions API 封装成类型化决策服务。业务侧传入状态和问题，获得判断、分类或评分；服务侧通过一份 TOML 配置提示词和概率处理。

[快速启动](#启动) · [配置指南](docs/configuration.md) · [API 兼容性](docs/api.md) · [概率与诊断](docs/probabilities.md)

```text
状态 + 问题 → Jev 模拟器 → Chat Completions API
                  ↑                 ↓
               TOML 配置       首 token 概率
                  ↓                 ↓
           Noul / Choice / Score ← 解析与聚合
```

| 能力 | 用途 |
| --- | --- |
| Noul / Choice / Score | 是非判断、候选分类、分档评分 |
| 文本提示词模板 | 直接编辑措辞、布局和证据位置 |
| 双循环与自定义标签 | 比较候选的两种顺序，或替换默认字母标签 |
| 温度缩放 | 调整输出概率的尖锐程度 |
| 概率质量诊断 | 查看归一化前质量、缺失标签和质量上下界 |
| stdin 热重载 | 运行中切换完整配置，在途请求保持原配置 |

无需 GPU、SGLang、完整词表或选定-token 查询接口。每个比较分支只请求一个输出 token，从返回分布读取目标标签。安装后可独立运行，无前端构建步骤。

兼容的是公开的请求／响应结构，不是官方模型的判断质量、内部评分公式或全部容量限制。具体边界见 [接口与兼容性](docs/api.md)。

## 启动

需要 Python 3.11 或更新版本。

```sh
git clone https://github.com/ueagohbbcd/jev-simulator.git
cd jev-simulator
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 用 .venv\Scripts\Activate.ps1
python -m pip install -e .
```

把上游密钥放进环境变量。PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
jev-simulator check --config config.toml
jev-simulator serve --config config.toml
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
system = "Answer the question using the evidence. Reply with one label, without whitespace."
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

提示词是带四个占位符的普通文本，可以直接修改措辞、顺序和重复次数。变量值只替换一次，证据中的占位符不会再次展开。

配置无继承、无预设覆盖顺序。省略项使用代码默认值，未知配置项在加载前报错。运行中的服务可通过 stdin 重载整份配置；每次响应携带该请求所用配置的摘要 `x-jev-config-id`，方便关联实验。配置文件只引用密钥环境变量名，不保存密钥值。

独立示例：[温度缩放](examples/calibrated.toml)、[呼号](examples/callsigns.toml)、[双循环](examples/round-robin.toml)。每份都可直接通过 `--config` 使用。字段说明见 [配置指南](docs/configuration.md)。

## 运行中切换配置

启动 `serve` 后，在同一终端逐行输入：

```text
status
reload
reload examples/calibrated.toml
reload "配置文件/我的配置.toml"
```

`status` 查看当前配置；`reload` 重新读取当前文件；`reload PATH` 切换到另一份完整配置，成功后它成为后续 `reload` 的默认文件。相对路径始终以**进程启动时的工作目录**为基准，回执显示绝对路径。

每条命令在 stdout 输出一行 JSON 回执；运行日志和告警仍写 stderr。程序也可以持有子进程的 stdin/stdout 管道完成控制，无需 HTTP debug 端点。控制输入采用 UTF-8，一条命令一行，空行忽略；stdin 关闭只停止接收命令，HTTP 服务继续运行，退出服务使用 Ctrl+C 或进程管理器。

新配置先验证再整体切换，失败继续使用旧配置和旧路径。在途 HTTP 请求保留收到请求时的配置，新请求使用新配置。重载只做本地检查，不访问模型。`[server]` 下的监听、鉴权和容量设置都要求重启；如果它们有变化，本次重载整份拒绝，不会只应用其中一部分。

## 命令行调试

```sh
# 离线检查，不需要密钥，不发送请求
jev-simulator check --config config.toml

# 展开全部实际提示词和映射，查看双循环会发多少请求
jev-simulator preview --config config.toml --request examples/request.json

# 真实调用；stdout 是 Jev 响应，可选保存完整诊断
jev-simulator evaluate --config config.toml --request examples/request.json \
  --diagnostics diagnostics/example.json
```

预览、命令行运行和 HTTP 服务共用同一个计划、解析和聚合实现。完整诊断包含提示词、原始概率及映射，可能含输入隐私；只在明确指定路径时写入，本仓库默认忽略 `diagnostics/`。

HTTP 进程把结构化事件写到 **stderr**，附请求 ID、配置 ID、概率质量、缺失标签数量和告警，不写输入正文、完整提示词或 API key。正常归一化本身不是告警。默认不保存用户请求、不自动重试，也不因告警额外调用模型。

## 概率语义

原始目标质量高，说明模型愿意按要求输出标签，不代表答对概率高。缺失 top-k 标签按 0 近似，诊断始终保留缺失标记和归一化前质量上下界；如果一个目标标签都读不到，返回错误，不能制造均匀分布。

温度在**每次请求的目标标签归一化时**应用。单次推理不改变选项排序；双循环先分别缩放，再按语义对齐、合并两种顺序，最后归一化期望胜分。因此双循环下的最终分布是锦标赛评分近似，不是一次模型调用的联合概率。

温度默认为 1，可用自己的标注数据校准。公式、告警与重放方法见 [概率与诊断](docs/probabilities.md)。

## 开发

```sh
python -m pip install -e ".[test]"
python -m pytest
```

测试使用本地模拟上游，无需 API 密钥，不产生模型调用费用。

项目参考 [Jev HTTP API](https://docs.typesafe.ai/api) 和 [openjev-sglang](https://github.com/ekzhang/openjev-sglang) 的接口思路，独立实现普通 Chat API 后端。后者能直接查询选定 token，本项目用提示词约束与可观测的 top-k 近似替代该能力，不声称两种读数完全等价。
