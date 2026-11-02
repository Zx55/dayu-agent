---
create-date: 2026-11-02
lifecycle: active
status: reviewed
description: Responses 与 ChatGPT 登录整体复审，5 项历史 findings 已关闭，批准范围内可进行本地全局安装。
---

# Responses 与 ChatGPT 登录整体复审

## 结论与范围

首轮 4 项 findings 已修复并有回归证据。随后真实账号验收发现的温度 profile P1 现也已修复，完整 init → scene/Host 集成回归和不带温度覆盖的默认入口真实验收均通过，详见 [真实账号验收记录](2026-11-02-responses-chatgpt-live-acceptance.md)。本次修复复核未发现新的代码阻断项。完整财报研究和真实 token 轮换仍未验收，不将本轮限定范围通过扩大为全部业务已验收。

用户随后授权再次整体 review，无阻断问题时替换全局 CLI。本轮重新审阅整个功能 diff 及新增文件，核对 provider 分派、候选授权提交、轮换与错误分类、共享工具执行器、Responses 终态、continuation、Service/Host 接受边界及温度空值传播。未发现新增阻断 finding，历史 5 项均已关闭。新增验证为 **84 passed**，包括认证、完整初始化集成、provider、Responses 与全部架构边界测试；此前 2106 passed、1 skipped 的扩大回归和 3 个默认配置真实 run 继续作为验收证据。代码未因本轮审阅而修改。

本次允许安装的结论限定于已验证功能及保留的风险。安装采用本地固定提交的非 editable uv 副本，不推送或合并远端，不运行日常工作区 init，不迁移隔离授权到正式连接。安装前 doctor 通过，现有全局版本为 `c40664a7cd7e3094e75e107d03b43adf14087c7f`。安装的确切提交、命令、依赖约束、检查结果和回滚信息记录在本机 `~/.local/share/dayu/installations/2026-11-02-responses/`。

审阅对象为 `feat/responses-chatgpt` 相对本地 `dev@c40664a7cd7e3094e75e107d03b43adf14087c7f` 的整个改造，前几轮审阅包含尚未跟踪的新文件，本轮审阅后才固定本地提交用于安装。设计依据为 [改造方案](2026-11-02-responses-chatgpt-plan.md)、仓库 `AGENTS.md` 及现有包 README。仓库没有通用 `docs/design/`、review checklist 和 testing guidelines，本轮使用上述实际存在的项目真源。

## Code Review Lane

| finding | 复核结果 |
| --- | --- |
| P1 刷新结果在异步客户端关闭后才保存 | 已关闭。完整响应返回后，同步校验与保存位于下一次 await 之前。关闭阶段取消不会丢失本地已取得的轮换结果，锁由 finally 释放。 |
| P2 登录先激活、配置失败后旧连接丢失 | 已关闭。候选授权仅用于目录查询，交互完成后按工作区锁、连接锁的固定顺序提交。generation 冲突会拒绝提交，配置及凭据的进程内异常回滚覆盖部分写入。 |
| P2 超限与 incomplete 破坏 Agent 治理语义 | 已关闭。超限归一后由 Agent 压缩，文本截断走现有续写并保留原始状态，过滤终态不续写，任何 incomplete 工具请求不执行。 |
| P2 HTML/空 503 被误判为协议失败 | 已关闭。刷新先分类 429/5xx，Runner 有限退避且不清凭据，永久 4xx 不按临时错误重试。 |

工具循环复核包括完整终态及参数校验、工具事件身份和顺序、工具结果回填、多轮 reasoning 回传、关闭和取消传播。未完成响应不会进入工具执行，流中止和请求重试不会执行已收到的部分参数。

## Architecture Review Lane

- CLI 拥有 provider 交互及配置提交，API key 型与登录型流程分开，定向入口仍为 `init --provider`。没有新增独立 auth 命令或第二套模型配置。
- auth 拥有浏览器授权、身份校验、凭据存储及轮换。`PendingAuthorization` 不落盘，`CredentialStore` 自行恢复凭据。配置事务只处理明确列出的配置文件，不解释凭据内容。
- Host 通过最小凭据协议装配 Runner，继续拥有生命周期与取消。auth、codec 和执行器不反向依赖 Host 或 CLI。
- Runner 负责单次模型交互、错误映射及调用共享执行器，Agent 负责迭代、工具结果回填、压缩和续写。没有引入 Codex runtime 或第二个隐藏会话循环。
- continuation 绑定注册身份与模型，保留完整有序输出，不作为正文、摘要或 UI 事件数据。工具执行提取到共享模块，没有复制出两套工具行为。

## Verification Evidence（首轮复审历史）

扩大回归包含整个 `tests/cli`、`tests/application`、`tests/architecture`，以及 Agent、上下文预算、取消、SSE、OpenAI Runner 和 Responses Runner 的相关测试，结果为 **1642 passed, 1 skipped**。

覆盖率专测运行 `test_responses_runner.py`、`test_async_openai_runner_tools.py`、`test_async_openai_runner_utils.py`、`test_chatgpt_provider.py` 和 `test_chatgpt_auth.py`，结果为 **104 passed**。使用 `coverage run --source=dayu`，新增模块合计 **92%**。auth 为 88%–100%，ChatGPT 配置为 91%，配置事务为 96%，Responses Runner 为 88%，codec/parser 为 90%，共享工具执行器为 96%。均满足项目单文件至少 80% 的目标。

全项目 pyright 与改造前基线均为 **17 errors**，逐条一致。错误来自未修改的 Docling 导入和 web tools 测试替身，无新增或屏蔽。相关新模块及测试的 Black 检查、`git diff --check` 均通过。

本轮验证输出保留在项目 `workspace/tmp/responses-fixes-*.log`。没有运行全仓库所有领域测试，本轮选择覆盖 CLI、应用装配、架构及所有受影响 Engine 路径。真实服务推理没有运行，自动化证据使用隔离凭据、模拟 OpenAI 响应和真实本地 HTTP/loopback。

## 剩余风险与验收建议

1. 隔离真实验收先发现 scene 温度解析阻止默认启动，现已修复并补齐完整 init 到 Service/Host 的集成回归。无温度覆盖的 `gpt-6.1-sol` 默认调用、网页工具循环和同 label 追问均通过。完整财报业务研究仍未完成。真实探针晚于初次实现的顺序偏差保留为历史记录，当前结论以补充的真实证据为准。
2. 配置和凭据提供的是进程内异常回滚。断电、SIGKILL 或恢复时磁盘持续失败可能留下跨文件不一致，未实现跨文件日志事务。
3. 若取消发生在完整刷新响应返回之前，客户端无法确认服务端是否已经轮换 refresh token。本轮修复仅消除响应已交给调用方后、异步关闭阶段的确定性丢失窗口。
4. 本地回滚不能撤销 OpenAI 服务端已经完成的注册或授权操作，旧 token 是否继续被服务端接受取决于服务端生命周期规则。
5. 本轮共享温度解析函数覆盖率为 83%，但其所在原有执行选项大模块整文件只有 71%，低于项目单文件 80% 目标。该缺口未通过无关测试或覆盖率排除掩盖。

全局 uv 安装元数据仍指向 `Zx55/dayu-agent@dev` 的 `c40664a7cd7e3094e75e107d03b43adf14087c7f`。后续安装、合并或推送均需在整体验收后另行执行。
