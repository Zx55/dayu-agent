---
create-date: 2026-11-02
lifecycle: active
status: in_review
plan_snapshot: Zx55/dayu-agent dev@c40664a7cd7e3094e75e107d03b43adf14087c7f
archive_snapshot:
description: 新增 Responses Runner 与 ChatGPT 账号登录，保留 Dayu 工具执行循环，并重构 provider 初始化流程。
---

# Responses Runner 与 ChatGPT 登录改造方案

## 审阅状态与依据

用户已认可方案方向，并要求先记录完整方案以便整体审阅。方案方向已获认可，当前 in_review 表示实现待整体审阅，不表示真实账号验收或安装已完成。用户随后授权从本地 dev 创建功能分支实现，测试后整体 review。全局 CLI、实际工作区配置和真实凭据仍不修改，合并和安装须留待 review 之后。

设计真源为仓库 AGENTS.md、dayu/README.md、dayu/engine/README.md、dayu/host/README.md、dayu/config/README.md 与 tests/README.md。实施前重新核验代码基线和官方授权规范。

## 目标与边界

- 新增独立 Responses Runner，沿用 AsyncAgent 的迭代、工具结果回填、上下文治理和最终答案生成。
- Host 继续拥有生命周期、取消、超时、恢复和持久化的控制权。
- 工具由模型选择并生成参数，由 Dayu ToolRegistry 执行。业务工具及权限规则保持原职责。
- ChatGPT 账号通过 Dayu 自己的官方 OAuth 注册授权，使用公共 Responses API，不读取或复用 Codex 私有登录凭据。
- 用户继续使用 provider 菜单、llm_models.json、scene manifest、--model-name 和现有 prompt/interactive/write 入口。
- 不接 Codex CLI/SDK，不恢复已禁用 CLI Runner，不新增独立 dayu-cli auth 命令体系。
- 保留现有 API provider 和无需认证的 Ollama 路径。不做财报业务重构、Web 登录界面、自动切换付费路径或跨进程逐工具断点恢复。

## 已核实的代码与安装状态

- 本地源码为 /Users/bytedance/Documents/workspace/dayu-agent，当前分支 dev，记录时工作区干净。
- origin 为 https://github.com/Zx55/dayu-agent.git，upstream 为 https://github.com/noho/dayu-agent.git。
- 当前源码提交为 c40664a7cd7e3094e75e107d03b43adf14087c7f。
- /Users/bytedance/.local/bin/dayu-cli 指向 uv 工具环境的 Python。安装元数据记录同一 fork 的 dev 与同一提交，属于非 editable 的独立安装。
- 项目 .venv 导入的是本地源码。源码改动不会自动改变全局 dayu-cli。
- 当前 Runner 工厂只创建 AsyncOpenAIRunner。工具批次执行在该 Runner 内，Agent 负责跨轮编排和工具结果回填。
- 现有 AgentMessage 采用 Chat Completions 消息形状，不能完整表示 Responses 的有序输出和 reasoning continuation。
- init 已负责 provider 选择、认证准备、模型条目写入及 scene 默认模型更新，但目前主要围绕 api_key_name 与 main_key_persist_failed 组织。

## 候选方案与选择

1. 恢复旧 CLI Runner。现有实现禁用且不桥接 Dayu 工具请求，不采用。
2. Codex SDK/app-server 接管执行。需要协调两套 agent runtime，不符合保留现有循环的目标。
3. 原生 Responses Runner 加内部 OAuth 凭据模块。协议适配留在 Runner，工具与治理留在 Dayu，采用此方案。

## 用户流程与配置约定

provider 初始化按认证方式分派 API key 型与登录型流程，另保留无需认证的既有路径。API key 型沿用已有使用流程，登录型实现 ChatGPT 授权与重新设置。内部使用小范围强类型认证描述和初始化结果，不继续用是否存在 API key 表达初始化成功。

ChatGPT 登录型流程为选择 provider、复用或准备当前连接的候选授权、校验权限、读取账号模型目录、选择模型和推理档位，最后提交模型配置、scene 默认项及授权。新授权在模型目录和交互阶段不激活。菜单使用“ChatGPT 订阅（账号登录）”。重新设置只执行登录型配置流程，不要求重做搜索 Key 或 HuggingFace 配置。

入口继续归属 init。实现使用 init --provider <方案名> 定向配置已有工作区，不增加 --model-only 或 auth 命令。登录型与 API key 型流程分离不等于取消完整首次初始化所需的公共步骤。

llm_models.json 保存 runner_type、模型、明确来源的上下文上限、reasoning 配置和凭据引用。scene manifest 继续管理 default_name 与 allowed_names。不新增第二套模型目录，不将 token 写进 JSON、环境变量配置、执行快照或 trace。认证方式与传输协议分别建模，避免把 Responses 永久绑定为仅支持 OAuth。

## 凭据生命周期与错误语义

- 默认凭据路径为 ~/.config/dayu/auth/，目录权限 0700、文件权限 0600，原子写入并做日志脱敏。
- 用户重新设置后替换当前连接的有效凭据，不默认引入多账号管理界面。存储仍正确区分账号身份和客户端注册。
- 新授权身份及 scope 校验通过后先保存在内存候选中。提交阶段依次取得工作区 init/reset 锁与连接锁，检查旧凭据 generation 及候选有效期，写模型 JSON 和 scene 默认项后才原子保存新凭据。目录查询、输入或提交异常以及用户中断不激活候选，提交期间异常恢复配置和旧注册。
- 配置文件快照及回滚由 CLI 的 `config_transaction.py` 负责，凭据保存及恢复由 auth 的 `CredentialStore` 负责。上述事务提供进程内异常回滚，不保证断电、SIGKILL 或磁盘不可写时的多文件原子恢复，也不能撤销服务端已完成的授权操作。
- 同账号重新授权复用已签发 client_id 和稳定 host ID。换账号建立匹配的新注册，整组替换，禁止拼接旧 client_id 与新 token。
- 接近 access token 过期时自动刷新。access/refresh token、过期信息和授权 scope 一起原子更新。完整刷新响应返回后，在下一个异步等待（包括 HTTP 客户端关闭）之前同步校验并保存轮换结果，避免关闭阶段取消丢失新 refresh token。
- 刷新和手动重新设置使用同一连接锁与版本检查，避免两个进程争用轮换 refresh token，或旧刷新覆盖新登录。发请求前读取当前凭据，运行中的账号身份不得静默切换。
- 请求认证失败时检查确切错误码、身份及 scope。仅在适用且确认安全时刷新并最多重试一次，不将所有 401 视为过期。
- refresh 的 invalid_grant、明确失效/撤销/复用等终止错误清除不可用 token，保留重新授权所需注册信息，返回需要重新登录。
- invalid_client 作为客户端配置错误。网络失败、429 和临时 5xx 保留凭据并有限退避，先按状态分类再解析 JSON，代理 HTML/空错误页同样适用。永久 4xx 不进入临时 HTTP 错误重试。额度、权限、模型及不支持参数错误独立分类。
- 认证终止错误必须传到 Host，不能被 force_answer 降级吞掉。后台任务不得打开登录浏览器，不自动转用付费 API。
- 保留已完成工具结果，不因认证失败重跑整个研究链路。只有响应完整且通过校验才执行工具，避免断流和请求重试重复执行。

## Step1 验证官方账号路径

- Goal：验证真实账号授权、模型目录与本地函数调用闭环。
- Do：先完成 OAuth，再测试文本响应和固定测试函数。检查 response.completed、工具请求和结果回传。
- Do Not：不修改生产配置、默认模型或全局安装，不以登录成功代替推理验收。
- Planned Files And Purpose：No file changes，必要临时探针仅放 workspace/tmp/。
- Risks：账号已登录但未授予套餐权限、模型不可用或规范变化。
- Tests：真实授权拒绝/成功、一次文本响应与函数请求闭环。
- Acceptance Criteria：确认账号可用、响应正常完成，工具实际在本地执行且结果被模型消费。

## Step2 重构 provider 初始化与内部凭据能力

- Goal：建立 API key 与登录型流程，共用明确的初始化结果和配置提交机制。
- Do：增加认证类型、OAuth/PKCE/state/nonce、ID token 验签、scope 校验、凭据锁、刷新和替换。通过初始化流程处理重新授权。
- Do Not：不引入独立 auth 命令或通用插件框架，不把认证成功等同于模型可用。
- Planned Files And Purpose：修改 dayu/cli/commands/init.py 及必要参数入口，分派初始化和提交配置；新增 dayu/cli/chatgpt_setup.py 编排候选授权与模型选择，dayu/cli/config_transaction.py 负责配置异常回滚；新增 dayu/auth/ 存放流程、存储和刷新；新增 dayu/contracts/model_credentials.py 定义最小凭据提供协议；启动装配层注入该协议。JWT/JWKS 使用成熟库，并同步依赖声明。
- Risks：并发刷新、失败替换、账号注册混用、初始化部分写入。
- Tests：现有 API/Ollama 初始化回归，OAuth 校验、取消、替换、跨进程竞争和脱敏测试。
- Acceptance Criteria：API 流程保持原使用方式，登录流程可重复配置；认证失败不改变默认模型；token 不进入配置或执行快照。
- Decision Points：定向进入登录流程的 CLI 参数名称在编码前确定。

## Step3 复用 Dayu 工具批次执行

- Goal：两个 Runner 共享同一套工具执行行为。
- Do：从 AsyncOpenAIRunner 提取批次执行、ToolExecutionContext、超时、取消、排序和事件生成。
- Do Not：不移动整个 agent loop，不改业务工具、仓储或权限语义。
- Planned Files And Purpose：修改 dayu/engine/async_openai_runner.py；新增 dayu/engine/tool_batch_executor.py；必要强类型公共数据放 contracts。
- Risks：工具事件顺序、取消传播和执行次数发生回归。
- Tests：先记录既有 Runner 工具测试基线，再验证提取后行为、并发超时与取消。
- Acceptance Criteria：调用身份、顺序和事件语义一致，两个 Runner 复用执行模块。

## Step4 实现 Responses Runner 与装配

- Goal：通过公共 HTTP/SSE 接口完成一次模型调用，映射为现有 Engine 事件。
- Do：使用已有 aiohttp，分离 codec、流解析和请求编排；明确完成、失败、不完整与断流状态；统一取消、close、超时和有限重试。
- Do Not：不把现有 Chat Completions parser 改成混合协议，不透传未知 extra payload，不引入 Codex runtime。
- Planned Files And Purpose：新增 dayu/engine/async_responses_runner.py、responses_codec.py、responses_stream_parser.py；修改 contracts/model_config.py、host/agent_builder.py、engine/runner_factory.py、execution/options.py、execution/runtime_config.py 及必要快照契约；startup/config_loader.py 校验新增配置。
- Risks：账号路径参数限制、工具 namespace 映射、分片参数不完整、流开启后的额度错误。
- Tests：文本、单工具、多工具、禁用工具、非法参数、断流、取消和错误映射；既有 Runner 回归。
- Acceptance Criteria：响应完成后才提交完整工具批次；不重复执行；Host 看到准确终态；所有网络等待可取消。

账号路径显式强制 store=false、stream=true，每次发送所需 input 历史，不使用 previous_response_id。system 输入转 developer 或 instructions，函数工具通过 namespace 组织。temperature 等不支持字段不发送。请求策略在协议边界收口，不改变 Dayu 业务工具定义。

上下文超限的供应商错误码归一为 Engine 的 `context_overflow`，由 Agent 按现有策略压缩并重试。已知文本 incomplete 原因 `max_output_tokens` / `max_messages` 映射为 `length` 与 `truncated`，续写时保留原始输出和 reasoning；`content_filter` 映射为过滤终态，不续写。未知 incomplete 原因报错。任何 incomplete 工具请求均不执行，即使参数 JSON 完整。

## Step5 正确保留 Responses continuation

- Goal：保留工具循环所需的有序输出和加密 reasoning 状态。
- Do：定义独立强类型 continuation 契约，附着对应 assistant 步骤；Runner 编解码，Agent 保留并管理生命周期；有原始 items 时避免重复生成文本与函数调用；压缩、裁剪和 replay 成组处理调用、结果与附属状态。
- Do Not：不把加密状态作为 reasoning_content、正文或摘要，不在 Runner 内另建隐藏会话真源。
- Planned Files And Purpose：修改 contracts/agent_types.py、engine/async_agent.py、Responses codec；检查并按需修改实际消息压缩及 replay 调用点，写测试证明边界。
- Risks：悬空 call_id、重复 items、状态串账号、压缩遗漏、将密文长度误当正文 token 预算。
- Tests：连续多轮函数调用、压缩后继续、禁用工具、replay、账号隔离、close 与状态清理。
- Acceptance Criteria：工具循环状态可正确回传，状态不串 run/账号，不进入展示或摘要；无依赖 previous_response_id 的隐藏状态。
- Decision Points：编码前审阅 continuation 类型与压缩边界。首版不改 Host 数据库 schema；若证据表明必须修改，先更新方案，并按 AGENTS.md 纳入 workspace_migrations，禁止静默扩大范围。

## Step6 集成、审阅与安装

- Goal：完整走通现有研究入口，再安装可追溯版本。
- Do：登录后从账号目录选模型，写入 llm_models.json 与 scene manifest；验证 labeled prompt、后续追问、工具执行、Markdown 输出和认证失败终态。
- Do Not：不覆盖现有工作区配置，不自动 reset，不先替换全局 CLI 再开始调试。
- Planned Files And Purpose：修改必要初始化配置写入逻辑，补 tests/cli、tests/engine、tests/application、tests/architecture 或 integration；按职责同步根 README、dayu/README.md、Engine/Host/配置及 tests README。批准后的本计划记录实施决定与验证证据。
- Risks：模型选择与 scene 角色不匹配、全局安装错版本、真实账号验证未覆盖。
- Tests：项目 .venv 中受影响测试、架构检查、既有 Runner 回归、全项目 pyright；新增/修改模块覆盖率目标至少 80%；真实账号端到端验证与安装后 doctor。
- Acceptance Criteria：新路径闭环成功、旧 API 路径不回归，CLI --output 可用，安装来源与验收提交一致。未运行的真实验证必须明确标记，不能宣称已交付。

## 开发与安装路径

无需重新 clone。实施时从现有源码的 dev 基线创建功能分支，避免直接在 dev 混入未验收代码。已创建功能分支 feat/responses-chatgpt。

开发期使用项目 .venv，通过 .venv/bin/python -m dayu.cli 和项目 pytest/pyright 验证，不修改全局 uv 工具安装。审阅 diff、测试与授权行为后，提交并合入 fork dev，再按完整提交 SHA 安装，最后核对安装元数据与 doctor。推送、合并和替换安装在实施验收阶段明确执行，本次均未进行。

安装使用 dayu-installation 技能的既有安装脚本，--ref 传验收提交，普通代码更新使用 --skip-init。新 provider 首次配置单独进入 init 登录流程；不得因安装更新自动 --reset 或 --overwrite。回滚可重新安装记录中的旧提交，凭据及配置是否需要回退须依据实际 schema 另行核查。

## 总体验收与审阅关口

1. 先确认账号授权和本地函数路径真实可用。
2. 审阅认证接口、初始化提交、错误分类和 continuation/压缩设计后再实现相关核心逻辑。
3. 测试不得暴露内部公共接口来迁就测试，不新增或掩盖 pyright 错误。
4. 当前配置真源仍为现有 JSON 和 scene manifest，Host、业务工具与仓储边界保持明确。
5. 认证终止、额度与临时失败可区分，未完成响应不会被误报为成功。
6. 全局安装只在项目验证和整体审阅之后进行，保留原安装提交用于回退。

## 官方来源与待复核项

以下来源在 2026-11-02 讨论中已核对，实施前需复核最新规则。

- 注册、PKCE、身份校验与凭据保存：https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- 注册身份、刷新轮换与账号生命周期：https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- 账号路径模型和推理：https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- 账号路径参数、工具和存储限制：https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- 认证、额度、临时错误与恢复：https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
- 函数调用：https://developers.openai.com/api/docs/guides/function-calling
- reasoning 状态回传：https://developers.openai.com/api/docs/guides/reasoning

## 实施与验证记录

已在功能分支实现 OAuth/凭据、provider 初始化分派、共享工具执行器、Responses 编解码及 Runner、continuation 和 Host 装配。自动化验证与剩余风险将在本节补齐。项目 .venv 增加 JWT 验签依赖，未更改全局工具环境。真实账号 Step1 与端到端研究验收尚未执行，需要用户完成浏览器授权。本次通过模拟服务及真实本地 HTTP 回调推进实现，不把该结果视为真实账号准入证明。未提交、推送、合并或安装覆盖。


### 实现审阅记录

- 分支为 `feat/responses-chatgpt`。本轮代码未提交、未推送、未合并，保留完整工作区 diff 供整体 review。
- provider 入口实际采用 `init --provider <方案名>`。API key 流程提取为独立初始化函数，ChatGPT 登录型流程位于 `dayu/cli/chatgpt_setup.py`。定向入口要求已有配置，不允许 reset/overwrite。
- continuation 使用 assistant message 的强类型字段保存有序原始输出，绑定注册身份及模型。普通与 force-answer 分支均剥离对外事件中的状态，原有供应商请求边界不发送该字段。
- 工具执行抽取到 `ToolBatchExecutor`。任务调度发生在取消观察建立后，取消、超时与后台线程可能继续的事实均保留。
- 新增依赖为 PyJWT[crypto] 与 filelock，仅在项目 .venv 验证。两个依赖在 pyproject.toml 与 requirements.txt 同步声明。

### 首次实现验证结果（修复前）

- 改造前相关测试基线为 88 passed。
- 扩大回归为 **1621 passed, 1 skipped**，覆盖整个 tests/cli、tests/application、tests/architecture，以及 Agent、上下文预算、取消、SSE 与 OpenAI/Responses Runner 的相关测试。
- 最后增补 force-answer 状态不外泄测试，Responses 定向测试为 **9 passed**。
- 覆盖率专测为 **83 passed**。新增认证、登录配置、Responses 与共享执行器模块合计 **91%**，每个新增生产模块至少 **85%**。使用 `python -m coverage run --source=dayu -m pytest ...` 采集，避免按 dotted module source 提前导入包引发第三方初始化错误。
- 全项目 pyright 基线和当前均为 **17 errors**，逐条对比没有新增。既有错误位于未修改的 Docling 导入和 web tools 测试替身，未为了本功能顺手修改或屏蔽。
- CLI `init --help` 已显示定向 provider 参数，`git diff --check` 通过。
- 原全局安装元数据仍为 `Zx55/dayu-agent@dev` 的 `c40664a7cd7e3094e75e107d03b43adf14087c7f`，未安装覆盖，也未修改真实工作区与凭据。
- 原始测试和静态检查输出保存在项目 `workspace/tmp/responses-*.log`。这些是本轮临时验证产物，不纳入版本控制。

### 仍需真实账号验收

自动化已覆盖动态注册、已注册账号重新授权、真实 loopback 回调、JWT 验签、scope、拒绝授权、凭据替换、轮换刷新、临时错误、模型目录、SSE 分片、两轮工具调用、认证错误终止、取消与降级状态脱敏。

尚未进行真实浏览器账号授权、实际套餐模型及推理档位准入、真实 labeled 财报研究和后续追问。review 后需在隔离工作区以项目 `.venv/bin/python -m dayu.cli` 完成，不将模拟测试视为线上准入证明。覆盖全局 CLI、推送或合并仍不属于本轮执行范围。

## 第一轮 findings 修复与第二轮整体复审

用户授权修复首轮 4 项 findings、增加回归、同步本方案并再次整体 review。改动继续留在 `feat/responses-chatgpt` 工作区，未提交、推送、合并或覆盖全局 CLI。

| 首轮 finding | 已实施修复 | 对应回归证据 |
| --- | --- | --- |
| P1 刷新后等待客户端关闭时取消会丢失轮换 token | 将刷新响应校验与原子保存移到 HTTP 客户端上下文内部，位于异步关闭之前 | 模拟真实异步 transport 在关闭处阻塞，取消后验证新 token 已落盘，下一次 acquire 不再刷新且锁已释放 |
| P2 登录先保存账号、后续配置失败却已切换连接 | `authorize` 返回未激活候选；配置与默认项成功后才保存，提交失败恢复配置和注册，提交前检查 generation | 目录失败、非法输入、用户中断、默认项写入后失败、凭据保存后失败、并发刷新冲突以及成功提交 |
| P2 原始超限码及 incomplete 未遵循 Engine 契约 | 归一超限码，区分文本截断、过滤和不允许执行的 incomplete 工具请求，续写保留 continuation | Agent 实际压缩后重试、带 encrypted reasoning 续写、空/非空过滤终态、不完整响应中的完整参数仍不执行 |
| P2 刷新接口先解析 JSON，HTML/空 503 误报协议错误 | 429/5xx 先按 HTTP 错误分类，Runner 仅对临时状态有限重试 | HTML/空 429/502/503 保留凭据及 HTTP 错误类型，刷新 HTML 503 后重试成功并完成 Responses 请求 |

第二轮验证结果为扩大回归 **1642 passed, 1 skipped**。覆盖率专测 **104 passed**，新认证、CLI 配置、Responses 与共享执行器模块合计 **92%**，各非空新增生产模块为 **88%–100%**。全项目 pyright 仍为原有 **17 errors**，与基线逐条一致，无新增错误。Black 检查相关新模块和测试通过，`git diff --check` 通过。验证日志为 `workspace/tmp/responses-fixes-broad.log`、`responses-fixes-coverage.log` 和 `responses-fixes-pyright.log`，不纳入版本控制。

整体复审同时检查代码与架构。首轮 4 项已关闭，未发现新增代码阻断项。Host 仍装配凭据并控制取消和生命周期，Runner 负责单次模型请求与协议映射，共享执行器负责 Dayu 工具执行，Agent 继续拥有跨轮循环、压缩与续写。配置事务不直接读写凭据格式，auth 不反向依赖 CLI 或 Host。

本次验收仅限已核查代码和自动化行为，真实账号准入与 labeled 研究仍未完成，故方案继续保持 `in_review`。多文件异常回滚不等于断电事务，完整响应返回之前的取消也无法证明服务端是否已经轮换 token。详细复审范围、证据和剩余风险见 [整体复审记录](2026-11-02-responses-chatgpt-review.md)。

## 首次真实账号验收（温度 P1 修复前）

用户随后授权使用项目 `.venv` 和隔离工作区完成真实浏览器登录、模型调用、工具循环及后续追问，继续不覆盖全局 CLI。本次完整 init 已完成，账号目录的 `gpt-6.1-sol`（500000 tokens、medium 推理档位）真实调用成功。同一 label 下 4 个 run 均由 Host 记录为 `succeeded`，包括纯文本、单工具结果回填、连续两次依赖工具调用及无工具的后续追问。

本次也发现新增 P1 阻断项：默认生成的 Responses 配置没有温度 profile，但 scene 接受阶段无条件要求该字段，默认 prompt 退出码 2。其后通过公开 `--temperature 0.2` 覆盖完成真实链路验证，该值未发送至 Responses 服务端。此绕过不计为默认入口通过，本轮未修改生产代码修复该项。应先使温度解析遵守协议能力并补齐 init 到 scene/Host 的集成回归，再不带覆盖重验。

本次没有完成完整财报下载、解析与研究，也没有主动验证真实 token 轮换。方案继续保持 `in_review`。用户 shell 配置、原凭据及全局安装元数据指纹均未改变。过程、run 身份、证据位置和新 finding 详见 [真实账号验收记录](2026-11-02-responses-chatgpt-live-acceptance.md)。

## 温度 profile P1 修复计划（已获用户授权）

### Step1 修复温度适用性与空值契约

- Goal：init 生成的 Responses 配置无需温度 profile 即可进入 Service 与 Host。
- Do：在共享 scene 温度解析中依据 Runner 协议返回适用值，Responses 返回 None，并使 Service 接受结果及展示 DTO 正确表达不适用。Host 自有 scene 路径复用同一解析规则。
- Do Not：不补造未使用的温度 profile，不改 API provider 既有覆盖优先级，不引入第二套温度解析或修改 OAuth。
- Planned Files And Purpose：`dayu/execution/options.py` 负责温度适用性，`dayu/services/scene_execution_acceptance.py` 与 `dayu/services/contracts.py` 保持 nullable 契约，写作模型序列化 `dayu/services/internal/write_pipeline/models.py` 的返回类型同步容纳 None，测试放在 `tests/application/`。
- Risks：将空值误转为默认温度，或 Service 与 Host 两条 scene 路径不一致。通过契约断言和实际 HTTP payload 检查防止回归。
- Tests：先增加 init → scene 接受 → Host 准备及 Runner 请求的集成回归，确认修复前失败；覆盖无覆盖值、显式温度覆盖、Host 自有 scene 和原 API provider 行为。
- Acceptance Criteria：Responses 不要求 profile，已接受模型、展示 DTO、Host AgentCreateArgs 均以 None 表示温度不适用，HTTP 请求没有 temperature；原 API provider 测试通过。

### Step2 自动化与真实账号重验

- Goal：不使用 `--temperature` 即可完成真实模型、依赖工具链及后续追问。
- Do：使用项目 `.venv` 和现有 `responses-live-20261102-183840` 隔离授权，重跑三项实际请求，检查 Host 终态和 trace；运行 CLI、application、architecture 与相关 Engine 回归及 pyright。
- Do Not：不重新注册账号、不覆盖全局 CLI、不改日常配置、不推送或合并。
- Planned Files And Purpose：真实验收产物继续留在 `workspace/tmp/`，同步更新本方案、真实验收及复审记录，更新相关配置与测试 README。
- Risks：远端模型或工具服务的临时失败与本地 P1 混淆，记录实际错误和边界，不把模拟成功作为线上证据。
- Tests：默认初始化配置无覆盖启动、跨轮工具执行、独立 CLI 进程追问、外部配置/凭据及安装元数据指纹对比。
- Acceptance Criteria：三项真实 run succeeded，无温度覆盖，新增集成测试通过，pyright 不新增错误，全局安装和原凭据不变。

### 本轮完成与复核结果

已按上述计划修复温度适用性，None 贯穿 Service 接受契约、展示 DTO、写作模型序列化及 Host AgentCreateArgs。共享解析保证 Service 与 Host 自有 scene 路径一致，API provider 的现有温度规则未改变。新增完整 init → scene → Host → 本地 HTTP 集成回归已先失败后通过，无覆盖和显式覆盖均验证请求不携带 temperature。

扩大回归 **2004 passed, 1 skipped**，补充 memory、interactive 及 scene replay 回归 **102 passed**，合计 **2106 passed, 1 skipped**。pyright 与基线逐条一致，仍为原有 17 errors，无新增。Service 相关修改模块覆盖率 95%–100%，温度解析函数 83%；原有执行选项整文件为 71%，低于单文件目标的缺口如实保留，未扩写无关测试。相关日志见真实验收记录。

使用同一隔离授权，不传 `--temperature` 或 `--model-name` 的三个实际 run 均为 succeeded，覆盖默认模型调用、三轮依赖工具链与新 CLI 进程追问。温度 P1 已关闭。复核代码和架构边界后未发现本次修复引入的新阻断项，完整财报业务验收和真实 token 轮换仍未完成，整体功能继续等待最终审阅。未改变原凭据、shell 配置或全局安装，也未提交、推送或合并。

## 最终整体复审与安装授权

用户已授权整体 review 无阻断问题后替换原全局安装。本轮复审未发现新增阻断 finding，认证、init 到 Service/Host、Responses 及全部架构边界复核测试为 **84 passed**。既有扩大回归、无温度覆盖的真实账号验收以及剩余风险均已核查，完整财报研究与真实轮换尚未验收的范围没有扩大。

为安装确切审阅版本，将当前功能分支提交为本地固定 SHA，再通过 `uv tool install --managed-python --python 3.11 --force --constraints <原环境依赖约束> "dayu-agent @ git+file:///Users/bytedance/Documents/workspace/dayu-agent@<SHA>"` 安装非 editable 副本。此步骤取代原计划的先推送/合入远端 dev，原因是本次授权只要求本机安装。安装脚本固定远端仓库，不能直接安装未发布的本地提交，故直接使用对应 uv 命令。保留原依赖版本，新增认证依赖按包声明解析。普通更新不运行 init，不覆盖日常配置、不迁移或切换账号。

安装前后运行 doctor 和 CLI surface 检查，核对 PEP 610 安装来源与提交，逐文件比对安装包与审阅提交，并使用已隔离授权从安装后的环境完成模型/工具及追问冒烟。安装收据、原依赖快照、精确命令和回滚版本保存在 `~/.local/share/dayu/installations/2026-11-02-responses/`。原安装提交为 `c40664a7cd7e3094e75e107d03b43adf14087c7f`。

## ChatGPT 两档默认模板补齐（已获用户授权）

### Step1 让 provider 使用包内模板

- Goal：将 `gpt-6.1-sol` 的 high / low 预设纳入既有默认模型目录，消除 Python 中重复拼装配置的真源。
- Do：在包内 `dayu/config/llm_models.json` 增加 `chatgpt-thinking`（high）和 `chatgpt`（low），使用账号凭据引用和已核实的 500000 默认窗口。初始化读取模板，账号目录存在模板模型时默认选中该项，不依赖目录顺序；模型不可见时提示并保留手动选择。目录窗口或用户确认值覆盖静态窗口，thinking 档位提示默认取自模板。
- Do Not：不自动切换日常工作区 provider，不改 OAuth、Host 或工具循环，不引入独立配置文件或另一个模板框架。
- Planned Files And Purpose：`dayu/config/llm_models.json` 为预设真源，`dayu/cli/chatgpt_setup.py` 读取并复制模板，只覆盖明确选择的模型、窗口与 thinking 档位；相关测试放在 `tests/cli/test_chatgpt_provider.py` 和 `tests/application/test_responses_initialization.py`。
- Risks：仅增加 JSON 条目但初始化未使用、首选模型不在账号目录、目录顺序影响默认选择、模板嵌套对象被修改、重新配置覆盖无关供应商。通过默认选择、替代模型、模板字段继承、两档实际请求与旧配置保留测试验证。
- Tests：验证包内预设内容、preferred 模型不在首位、不可见时的提示与选择、手动覆盖、模板参数继承和不变性，以及 init → scene/Host → HTTP 两档 effort 的集成回归。
- Acceptance Criteria：默认 high / low 来自模板，Responses HTTP 请求实际使用对应模型与 effort，无温度 profile 要求，原 API provider 与凭据回滚测试通过。

### Step2 验证、发布与安装

- Goal：提交并推送通过验证的功能分支，从 fork 固定提交重装全局 CLI。
- Do：运行受影响回归与 pyright，同步根 README、配置和测试说明及本记录；用已有隔离授权检查两档真实模型请求；commit、push 后核对远端 SHA，再使用安装技能脚本指定该 SHA 并跳过 init。
- Do Not：不合并 dev、不重置或覆盖日常工作区、不迁移隔离账号、不顺便升级现有依赖。
- Planned Files And Purpose：前述 README 和方案文件记录实际行为；安装、回滚和验证收据留在本机 `~/.local/share/dayu/installations/`。
- Risks：装到旧提交、依赖漂移或日常配置改变。通过 PEP 610 元数据、模板内容/源码哈希、安装前后依赖及配置指纹核对。
- Tests：安装前后 doctor、CLI help、安装包模板断言与真实调用冒烟。
- Acceptance Criteria：本地、fork 分支和全局安装提交一致，测试通过且无新增类型错误，日常配置与凭据不变。

### 模板实施与验证结果

两个预设已写入包内 `llm_models.json`，provider 通过现有配置加载器读取并复制完整模板，仅覆盖用户选择的模型、实际窗口和 thinking 档位。初始化默认优先采用模板模型，与账号目录顺序无关；不可见时提示并允许选择。源代码不再独立拼装模型/认证/快速档默认值。

新增默认与模板继承测试先在旧实现产生 8 failed，实施后定向测试 **27 passed**。扩大回归 **2115 passed, 1 skipped**，初始化模块覆盖率 **94%**。pyright 仍为基线 17 errors，无新增。模板损坏时在授权前拒绝，用户覆盖、其他 provider 配置保留、凭据异常回滚和原 API 流程均由回归覆盖。

使用现有隔离授权重新进入真实 `init --provider chatgpt`，全部采用回车默认值，成功生成 `gpt-6.1-sol` high / low 两档。期间真实刷新接口与模型目录均返回 HTTP 200。随后从生成配置经过 Service/Host 装配发起两次真实模型请求，两档均完成并返回正文。日志在 `workspace/tmp/chatgpt-templates-*`。本轮复核未发现新的阻断项，发布时固定本地提交并推送同一功能分支，再从 fork 的完整提交 SHA 安装。
