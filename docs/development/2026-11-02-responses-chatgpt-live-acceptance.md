---
create-date: 2026-11-02
lifecycle: active
status: verified
description: 默认温度配置 P1 已修复，现有隔离授权下无覆盖参数的模型、工具循环及追问验收通过。
---

# Responses 与 ChatGPT 真实账号验收

## 结论

温度 profile P1 已修复。本次使用原有隔离授权，不传 `--temperature`，也不传 `--model-name`，默认模型调用、Dayu 两步依赖工具循环及新 CLI 进程中的同 label 追问全部通过。3 个新增实际 run 均为 `succeeded`，生效温度为 `null`。

本次请求的默认入口验收已通过。此前首次真实验收的 4 个 run 和失败证据保留在下文作为历史记录，完整财报研究和真实 token 轮换仍不在本次通过范围内。全局 CLI、原账号凭据及 shell 配置均未改变。

## P1 修复与默认入口重验

### 代码与集成回归

`resolve_scene_temperature` 按当前 Runner 协议处理温度适用性，Responses 在读取 profile 或显式覆盖之前返回 None。Service 的接受结果与展示 DTO 允许 None，写作模型序列化返回类型同步容纳 None，Host 两条 scene 路径复用同一解析方法。没有给 init 生成的模型补造温度 profile，也没有改变原 API provider 的温度优先级和缺失 profile 校验。

新增 `tests/application/test_responses_initialization.py` 从完整 init 开始，重新加载生成的配置与 manifest，经 `SceneExecutionAcceptancePreparer`、公开的 `DefaultScenePreparer.prepare` 和 Host Runner 装配发出真实本地 HTTP 请求。只模拟浏览器授权边界、模型目录和用户输入，不替换温度解析、配置提交、Service、Host 或 Runner。

该回归先在修复前产生 **2 failed**，分别暴露无覆盖时启动失败和显式覆盖不能正确归一为空。修复后无覆盖、显式覆盖两种情况均通过，断言展示 DTO、接受契约、序列化结果及 AgentCreateArgs 的温度为 None、HTTP 没有 temperature，并验证 Host 自有压缩 scene 同样不需要 profile。

定向回归 **49 passed**。扩大回归 **2004 passed, 1 skipped**，覆盖 CLI、application、architecture、services，以及 Agent、取消、上下文预算、OpenAI/Responses Runner、SSE、写作流水线和 CLI 运行配置。补充 conversation memory、interactive coverage 与 scene replay 回归 **102 passed**，两组共 **2106 passed, 1 skipped**。测试 fixture 的环境恢复补全后，新增集成回归再次 **2 passed**。

pyright 使用项目解释器运行，命令为 `.venv/bin/pyright --pythonpath .venv/bin/python`，结果仍为原有 **17 errors**，与原始基线逐条一致，无新增错误。新增测试的 Black 检查和 `git diff --check` 通过。

覆盖率采集显示 Service 契约为 100%，scene 接受为 99%，写作模型序列化所在模块为 95%。原有 `execution/options.py` 整文件为 **71%**，仍低于项目 80% 目标，不能声称所有修改文件都达到目标。本次温度解析函数为 **15/18（83%）**，新增协议分支均被执行，剩余未覆盖的是原有 API provider 的三个异常分支。本轮未扩写无关执行选项测试来抬高整文件数字。

自动化原始证据为项目 `workspace/tmp/responses-temperature-before.log`、`responses-temperature-focused.log`、`responses-temperature-broad.log`、`responses-temperature-additional.log`、`responses-temperature-integration-final.log`、`responses-temperature-pyright.log` 和 `responses-temperature-coverage.log`。

### 真实账号结果

沿用验收根目录 `workspace/tmp/responses-live-20261102-183840/`、原授权与 init 生成的配置，无需再次登录。新 label 为 `responses-default-e2e-20261102`，session 为 `cli_conv_6326079283e845929e0a90d3515c1635`。三次请求分别在新的 CLI 进程执行，均未传 temperature 或模型选择参数。

| 验收项 | 实际证据 |
| --- | --- |
| 默认模型调用 | `run_0a99bc0b9433`，1 个 iteration，无工具，准确返回 `DAYU-NO-TEMP-93D7，默认配置模型调用成功。`。输出为 `05-default-model.md`。 |
| 有依赖的工具循环 | `run_cb908ed1f6a3`，第 1 轮 `search_web` 返回 IANA 官方 URL，第 2 轮 `fetch_web_page` 读取该 URL，第 3 轮汇总，两个工具均成功。后续轮次分别携带 1、2 组 continuation 和工具结果。输出为 `06-default-tools.md`。 |
| 新进程同 label 追问 | `run_7d50ea0fbbc9`，1 个 iteration，无工具，正确复述标记、工具依赖、实际 URL 和页面标题。输出为 `07-default-follow-up.md`。 |

三次日志的默认模型均为 `chatgpt-thinking`，底层为 `gpt-6.1-sol`，生效 temperature 均为 null。Host `runs --all` 确认三个 run 均为 succeeded。

本次聚合证据在验收根目录的 `default-retest-evidence.json`，Host 状态在 `default-retest-runs.log`，三个命令日志为 `default-*-retest.log`。输出和 trace 留在同一隔离工作区。原 shell、Dayu 凭据及全局 uv 元数据的前后指纹均一致，凭据仍为 0600，auth 以外的验收产物未发现本次完整 token。未提交、推送、合并或安装覆盖。

## 环境与隔离方式

- 源码为 `feat/responses-chatgpt` 工作区，基线为 `dev@c40664a7cd7e3094e75e107d03b43adf14087c7f`。
- 全部命令使用项目 `.venv/bin/python`，临时启动器最终调用原 `dayu.cli.main.main()`。
- 验收根目录为 `workspace/tmp/responses-live-20261102-183840/`，包含独立 `workspace/`、`auth/`、shell profile 及脱敏证据。
- 启动器仅把 CLI 和 Host 凭据工厂重定向到 `CredentialStore` 已有的 `root` 参数，并把 init 的 shell profile 路径重定向到验收目录。没有替换授权、验签、HTTP、模型响应、工具、Agent 或 Host 行为。
- 完整 init 经真实 provider 菜单进入 ChatGPT 授权。用户在系统浏览器完成登录，回调、ID token 验签及 scope 校验通过。候选授权在模型配置和 14 个 scene manifest 更新后才保存。
- 账号模型目录返回 GPT-6.1-Sol 等 8 个可展示项。本次选择 `gpt-6.1-sol`，目录给出的上下文为 500000 tokens，推理档位选 `medium`，使用配置名 `chatgpt-thinking`。
- 凭据目录实际权限为 0700，凭据文件为 0600。验收目录中除 auth 外的配置、日志、数据库及 trace 未找到本次 access/refresh/ID token 的完整值。
- 验收前后指纹比对确认用户 `.zshrc`、`.zprofile`、原 Dayu 凭据及 host-id、全局 uv 安装元数据未改变。未覆盖全局 CLI，未提交、推送或合并。

## 首次真实验收结果（修复前）

会话 label 为 `responses-e2e-20261102`，session 为 `cli_conv_9f1f65f7dcfe4352862a1d1b2e778acd`。以下请求分别由新的 CLI 进程发起，均复用该 label。

| 验收项 | 证据与结果 |
| --- | --- |
| 纯文本模型调用 | `run_2e6704c84a95`，1 个 iteration，无工具。准确返回 `DAYU-LIVE-7C42，模型调用成功。`，生成 `01-model.md`。 |
| 单工具结果回填 | `run_373bb63b6c11`，2 个 iterations。第 1 轮 `fetch_web_page(https://example.com/)` 成功，第 2 轮根据正文作答。工具正文没有保留 Learn more 链接，模型明确报告无法推导第二个 URL，没有猜测执行，生成 `02-tool-loop.md`。 |
| 两次有依赖的工具调用 | `run_b3a09992a380`，3 个 iterations。第 1 轮 `search_web` 搜索 `site:iana.org example domains`，第 2 轮 `fetch_web_page` 读取搜索结果中的 `https://www.iana.org/news/2024/example-domain-http-methods`，第 3 轮汇总。两次工具 trace 均为 `success`，生成 `03-dependent-tools.md`。 |
| 跨轮 continuation | 两步工具 run 的输入消息数依次为 6、8、10。第 2、3 轮分别带 1、2 组 assistant continuation，工具结果数分别为 1、2，模型实际消费后完成后续步骤。本次输出未包含 encrypted reasoning item，不把本次结果作为密文回传的线上证据，相关行为仍由自动化测试覆盖。 |
| 新 CLI 进程后续追问 | `run_6df99bf72ff4`，1 个 iteration，无工具。仅依赖会话历史，正确复述最初标记、此前未继续抓取的原因、搜索后抓取的依赖顺序和实际 URL/标题，生成 `04-follow-up.md`。 |

Host `runs --all` 确认上述四次均为 `succeeded`，同一 session 保持 `active`。没有调用其他付费模型配置或切换到 Codex CLI/SDK。

原始本地证据为验收根目录的 `evidence.json`、`runs-status.log`、`default-startup-failure.log`，以及独立工作区 `output/prompt/` 和 `output/tool_call_traces/`。这些产物含本地验收状态，不纳入版本控制，token 只保存在私有 auth 目录，不在本报告中引用其内容。

## 首次发现及修复前复现（P1 已关闭）

**[P1] 默认初始化配置无法通过 scene 温度解析，真实 prompt 尚未发出请求便失败。**

`dayu/cli/chatgpt_setup.py` 生成的模型条目只有 Responses 所需的模型、认证引用、推理档位和上下文。`dayu/services/scene_execution_acceptance.py` 对所有 Runner 无条件调用 `resolve_scene_temperature`，后者在 `dayu/execution/options.py:302` 强制要求 `runtime_hints.temperature_profiles`。因此用户成功完成浏览器登录和模型选择后，默认 prompt 会退出码 2，报 `scene=prompt_mt, model=chatgpt-thinking 缺少 llm_models.runtime_hints`。首次发现后已重复复现。

复现命令采用本次隔离启动器，等价于在已初始化配置上从正式 CLI 发起 prompt：

```bash
.venv/bin/python workspace/tmp/responses-live-20261102-183840/launch.py prompt --base workspace/tmp/responses-live-20261102-183840/workspace --model-name chatgpt-thinking --label responses-e2e-20261102 --output workspace/tmp/responses-live-20261102-183840/workspace/output/prompt/00-default-startup.md "只回复 OK"
```

添加 `--temperature 0.2` 后，上述 4 个真实 run 均成功。根据当前 Runner 实现，这个参数只绕过本地温度解析，不进入 Responses HTTP 请求，不能据此认为 Responses 需要补造温度档位配置。

另以未经过启动器的 `.venv/bin/python -m dayu.cli prompt --base <本次隔离工作区> --label responses-e2e-20261102 --output <隔离输出路径> "只回复 OK"` 复现相同错误，退出码同为 2，且没有显式选择模型。该失败在凭据读取前发生，证明默认入口问题并非验收启动器引入。输出保存在 `direct-default-startup-failure.log`。

最小修复方向是在执行选项/scene 接受边界按模型协议处理温度能力，Responses 不要求 temperature profile，不适用的值以明确的空语义贯穿接受契约。保持已有 API provider 温度解析规则。应增加“ChatGPT init 生成配置 → SceneExecutionAcceptancePreparer 接受 → Host 装配 Runner”的集成回归，覆盖无 CLI 温度覆盖的真实默认入口，并校验 Responses 请求没有 temperature。不要仅给自动生成的配置塞入未使用的温度 profile。

## 证据边界与后续验收

本次验证的是公共 Responses 下的真实账号、Dayu 网页工具循环和会话连续性，没有执行完整上市公司财报下载、解析与研究，也没有等待自然过期或主动触发真实 refresh token 轮换。后两者不能以本次通过替代。现有自动化中的刷新取消、并发、超限、截断与过滤测试仍是这些路径的证据。

上述默认启动 finding 已修复，集成回归和不传 `--temperature` 的重验结果见前文。真实财报研究作为独立业务验收项继续保留。

官方授权与账号模型目录的对照依据为 [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in) 和 [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。实际可用性结论以上述真实调用记录为准。
