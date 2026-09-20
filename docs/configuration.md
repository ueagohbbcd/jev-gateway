# 配置指南

一个进程使用一份完整 TOML，通过 `--config` 选择启动文件，也可以在运行中用 stdin 命令替换。`jev-gateway check --config PATH` 只做本地验证；它不检查账户余额、模型权限或线上 tokenizer。

## 选择与重载

在运行 `jev-gateway serve --config config.toml` 的终端输入 `status`、`reload` 或 `reload PATH`。路径有空格时可以用一对引号包围；Windows 反斜杠不作为转义符。相对路径基于启动工作目录，不基于上一份配置所在目录。

`reload` 先加载和校验整份文件，检查上游密钥环境变量是否存在，再切换当前配置快照。文件不存在、TOML 语法错误、提示词缺占位符或需要重启的设置发生变化，都返回失败回执，原配置和路径保持不变。检查不会发起上游请求，因此成功不代表模型权限、网络或余额已经验证。

可热换 `[upstream]`、`[adapter]`、`[prompt]`、`[diagnostics]`；`[server]` 的所有字段固定于进程启动时，变更需重启。切换配置文件时要带上相同的非默认 server 设置，省略字段表示使用默认值，不表示继承旧值。

每次 HTTP 请求在进入服务时捕获一个快照，包括最终响应头和失败日志的配置 ID。重载不影响已开始的请求，也不重置全进程并发额度。stdout 是逐行 JSON 命令回执，stderr 是服务日志；stdin EOF 不会关闭 HTTP 服务。

其他程序可以控制它启动的服务子进程。例如，下面只查询状态和切配置，不运行推理；密钥环境变量由子进程继承：

```python
import json
import subprocess

process = subprocess.Popen(
    ["jev-gateway", "serve", "--config", "config.toml"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
)
try:
    for command in ["status", "reload examples/calibrated.toml"]:
        process.stdin.write(command + "\n")
        process.stdin.flush()
        print(json.loads(process.stdout.readline()))
finally:
    process.terminate()
    process.wait()
```

这段示例结束时主动停止子进程。长期控制程序可以持续持有管道；stderr 默认继承到终端，不混入 JSON 回执。控制通道属于启动该进程的终端或父进程，不提供从另一个终端附着到任意已运行进程的命令。

## upstream：模型连接

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `base_url` | `https://api.deepseek.com` | Chat API 前缀，程序追加 `/chat/completions`；OpenAI 风格地址通常包含 `/v1` |
| `model` | `deepseek-flash` | 发给上游的实际模型名 |
| `api_key_env` | `DEEPSEEK_API_KEY` | 从哪个环境变量读取上游密钥 |
| `timeout` | `60.0` | 单次上游 HTTP 调用超时秒数 |
| `top_logprobs` | `20` | 请求返回的候选数，1–20；供应商需要支持所选值 |
| `extra_body` | `{}` | 供应商扩展，例如 `thinking = { type = "disabled" }` |

生成参数由适配器固定：一个输出 token、单条非流式 completion、开启 logprobs、采样温度 1。模型答案由概率分布决定，不使用实际采样出的文字作为硬答案。`extra_body` 不能覆盖这些协议字段，也不接受通过工具调用或 JSON 输出格式改变任务。换供应商时通常只需换连接配置并删去 `thinking`。

只支持普通文本 Chat Completions。不是透明转发任意聊天请求的代理，也不会把本服务客户端的 Bearer token 转交上游。

## adapter：三个执行特性和校准

`temperature` 为正有限数，默认 1。公式是 `softmax(label_logprobs / temperature)`，详见概率文档。它不更改上游采样参数。

`double_round_robin` 默认 false。开启后 Choice/Score 的 n 个选项需要 `n(n-1)` 次调用；Noul 仍为 1 次。三选一是 6 次，十选一是 90 次。完整批次超过调用上限会在任何上游调用前拒绝。

`callsigns` 默认 `[]`，使用 A–Z。非空列表按顺序绑定候选，例如：

```toml
[adapter]
callsigns = ["alpha", "fox", "delta", "echo"]
```

单次四选一会使用这四个词；双循环每场只使用前两个词，两次交换候选含义。列表长度同时限制 Choice/Score 的最大候选数。字符串大小写敏感，不能含空白、重复或为空。Noul 固定 Yes/No，与这份列表无关。

部署者须确认呼号在上游 tokenizer 的首输出位置是单 token。普通 Chat API 没有通用 tokenizer 查询方法，所以本地配置校验不能代替这项验证。示例中的候选曾在公开 DeepSeek tokenizer 上验证，但托管模型别名可能变化。`charlie` 等看起来简单的词也可能被切成多个 token。

## prompt：直接可读的文本

`system` 和 `user` 都是普通字符串，可用 TOML 多行字符串。两个字段合起来必须包含全部四个占位符：

| 占位符 | 渲染内容 |
| --- | --- |
| `{{state}}` | 原始字符串，或保留全部字段的 JSON |
| `{{instructions}}` | 当前问题的说明；结构化说明也转为 JSON |
| `{{options}}` | 当前调用的标签及描述；双循环只含本场两个候选 |
| `{{output}}` | 本场允许输出的精确标签要求 |

自定义问题 ID 不发给模型；Choice 的语义键也不发给模型，除非描述是 null，此时用键作为描述。Noul 的 true/false 标准渲染在 Yes/No 后。

没有隐式复读、布局预设或优先级。想复读问题，可再放一次 `{{instructions}}`；想复读全部，直接再写一遍对应段落。字面 JSON 的单大括号不受影响。`{{...}}` 只用于已知占位符，未知或不闭合的写法会报错。证据里的同名字样保留为证据，不再次渲染。

## server：运行边界

| 字段 | 默认 |
| --- | --- |
| `host` / `port` | `127.0.0.1` / `8080` |
| `api_key_env` | 省略，即本服务不要求鉴权 |
| `max_questions` | 64 |
| `max_body_bytes` | 2097152 |
| `max_calls_per_request` | 256 |
| `max_concurrent_requests` | 16 |
| `max_concurrent_calls` | 8 |
| `request_timeout` | 120.0 秒，整个批次 |

例如，配置 `api_key_env = "JEV_GATEWAY_API_KEY"`，再设置对应环境变量，就为 `/v1/*` 加上 Bearer 鉴权。这与上游 `DEEPSEEK_API_KEY` 分开。配置了密钥变量却没有设置值时，服务拒绝启动。

并发限制是单进程的。超过批次并发限制返回 529；上游 429 保留为限流信号。一次批次失败或超时会取消尚未完成的任务，但无法保证供应商立即停止已经收到的调用或不计费。

## diagnostics：告警门槛

`low_mass_threshold` 默认 0.99。比较的是温度缩放与目标归一化**之前**的概率质量。它只控制诊断，不改变答案、不触发额外请求、不将低质量调用静默丢弃。详细判定见 [概率与诊断](probabilities.md)。
