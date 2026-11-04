# Dayu 架构一阶原则

本文档记录 Dayu 当前的架构讨论基线。

它的目标不是复述当前代码，而是先固定后续重构与评审时不应再摇摆的判断：

- 系统到底分几层。
- 每层负责什么，不负责什么。
- 哪些是层，哪些只是某一层内部或层间使用的 public 模块。
- 哪些对象是稳定契约，哪些对象只是内部产物。

如果本文档与当前实现不完全一致，应优先把它理解为架构讨论坐标系，而不是当前代码说明书。

## 0. 设计目标

Dayu 的目标不是把 LLM 包成一次性的推理调用，而是建设一个在宿主强约束下可长期托管的执行系统。

这套系统应满足：

- 范式是宿主强约束下的 `LLM in the loop`，不是 `LLM on the loop`，也不是 intent classification 加预定义 DAG。
- 宿主显式拥有生命周期、资源边界、治理能力与失败处理能力。
- Agent 在宿主给定的边界内完成消息交互，而不是反向理解业务或控制系统。
- 同一套宿主治理能力可以同时支撑普通 Agent、金融专门 Agent、复合流水线和 direct operation。

后文所有边界判断都服务于同一个北极星：让宿主稳定托管执行，让 Agent 在严格约束下仍保持通用。

## 1. 核心结论

Dayu 的架构层次只有四层：

- `UI`
- `Service`
- `Host`
- `Agent`

稳定执行链按层看应固定为：

`UI -> Service -> Host -> Agent`

如果把执行过程展开，链路可以写成：

`UI -> startup preparation -> Service -> Contract preparation -> Host -> scene preparation -> Agent`

但这里必须明确：

- `startup preparation` 不是独立架构层，它是供 `UI` 在启动期使用的 public 模块。
- `Contract preparation` 不是独立架构层，它是供 `Service` 内部使用的 public 模块。
- `scene preparation` 不是独立架构层，它是供 `Host` 内部使用的 public 模块。

因此，层次和执行展开不是一回事：

- 讨论层次时，只谈四层：`UI / Service / Host / Agent`。
- 讨论执行展开时，可以显式写出 `startup preparation` 、 `Contract preparation` 和 `scene preparation`。

## 2. 四层职责

### 2.1 UI

`UI` 只负责：

- 采集宿主输入。
- 校验 `workspace`、`config` 等启动期原始路径参数是否合法。
- 构造 `Request DTO`。
- 在启动期获取稳定依赖。
- 调用目标 `Service`。
- 渲染结果、事件和状态。

`UI` 不负责：

- 解释业务语义。
- 决定执行策略。
- 直接驱动 `Agent`。
- 拼装复杂运行依赖。

### 2.2 Service

`Service` 是唯一允许理解业务语义的一层。

它负责：

- 接收 `Request DTO`。
- 解释用户请求，定义本次业务动作。
- 判断是否受理请求，以及缺失前提时如何处理。
- 决定使用哪个 `scene`。
- 决定本次执行需要哪些领域上下文、工具集合和动态权限。
- 提供领域侧 `Prompt Contributions`。
- 调用 `Contract preparation` 生成稳定的 `Execution Contract`。
- 把执行契约提交给 `Host`，或把其它被托管的业务操作提交给 `Host`。
- 在拿到 `Host` 终态结果后，决定当前业务路径是否需要使用 `Host` 暴露的可选 `reply outbox` 能力。
- 只通过 `Host` 暴露的稳定能力边界交互，不能依赖 `Host` 具体实现或直接访问其内部子组件。

它不负责：

- 拥有 run 生命周期。
- 直接构造 `Agent Input`。
- 自己拼最终 `messages`、`tools` 或 system prompt。
- 自己持有 transport 级入站 / 出站交付真源。
- 把原始配置文件结构继续往下游传播。

### 2.3 Host

`Host` 是通用托管执行层，不是 Agent 专属壳。

它负责：

- 接收 `Service` 提交的托管执行单元。
- 建立 session、run、取消、恢复、并发和资源治理边界。
- 托管一次执行的完整生命周期。
- 作为可选通用能力托管 `reply outbox` 真源与交付状态机，但仅在 `Service` 显式提交交付请求时生效。
- 在自身边界内调用 `scene preparation`、`Agent`、领域 runtime 或 pipeline。
- 记录和暴露宿主侧事件与状态。
- 对上层暴露稳定公共门面，但不把 `executor`、registry、governor 等内部组件直接泄漏给 `Service`。

它不负责：

- 理解高层业务目标。
- 理解领域语义本身。
- 在 internal success 时自动决定是否创建 reply outbox 记录。
- 直接承担 WeChat / Web 等渠道 transport 的发送实现。
- 代替 `Service` 决定这次请求要做什么。

### 2.4 Agent

`Agent` 是最低层的消息执行器。

它负责：

- 消费最终输入。
- 在预算、取消信号和工具边界内执行消息交互。
- 返回事件或结果。

它不负责：

- 理解服务身份。
- 理解领域语义。
- 理解配置文件结构。
- 拥有执行生命周期。

## 3. Public 模块，而不是新层

### 3.1 Startup Preparation

`startup preparation` 是供 `UI` 在启动期使用的 public 模块。

它不是新的架构层，而是把启动期原始来源收敛成稳定依赖的装配模块。

它负责：

- 接收 `workspace path`、`config path` 等启动期原始来源。
- 把路径和配置来源收敛成稳定对象，而不是把裸路径继续传下去。
- 构造 `WorkspaceResources`、`ConfigLoader`、`PromptAssetStore`、`ModelCatalog` 等稳定依赖。
- 构造特定领域依赖，例如金融领域使用的 `DocumentRepository` 或其运行时封装。
- 为 `UI` 显式 `new Host(...)` 和 `new Service(...)` 提供所需的小型 public 函数。

它不负责：

- 解释业务语义。
- 参与请求期控制流。
- 替 `Service` 受理业务请求。
- 替 `Contract preparation` 或 `scene preparation` 决定运行时契约。

因此，`startup preparation` 的目标是把原始路径消灭掉，换成稳定依赖；它不应把 `workspace path`、`config path`、`DocumentRepository path` 之类原始来源继续包装成请求数据。

### 3.2 Contract Preparation

`Contract preparation` 是供 `Service` 内部使用的 public 模块。

它不是新的业务层，也不是位于 `Service` 与 `Host` 之间的一层。

它负责：

- 接收 `Service` 已经做出的业务决策。
- 生成稳定的 `Execution Contract`。
- 处理通用聊天路径可复用的公共装配数据。
- 生成 GeneralChat 侧公共 `Prompt Contributions`。
- 合并 Service 提供的领域侧 `Prompt Contributions`。
- 把 Service 已接受的通用执行参数收敛为 `accepted_execution_spec`。

它不负责：

- 再次解释业务语义。
- 决定是否受理请求。
- 读取 scene manifest 并做 scene 级规则判断。
- 产出最终 `Agent` 创建参数。

它负责读取的是形成 `Execution Contract` 直接需要的配置，而不是所有配置。

### 3.3 Scene Preparation

`scene preparation` 是供 `Host` 内部使用的 public 模块。

它不是新的架构层，而是 `Host` 内部的执行装配能力。

它负责：

- 读取 `scene_name` 对应的声明式策略。
- 消费 contract 中已受理的 `accepted_execution_spec`。
- 把 `Execution Contract` 收敛为最终 `Agent Input`。
- 组装 system prompt、messages、tools 与会话状态。
- 校验 `Prompt Contributions` 是否命中合法 slot，并按 scene 顺序统一追加到 system prompt 尾部。
- 把 `selected_toolsets`、`execution_permissions` 与系统级硬边界共同落实为最终 tool binding。

它不负责：

- 受理或拒绝 execution options。
- 重新定义任务身份。
- 解释业务语义。
- 拥有执行生命周期。

它负责读取的是形成 `Agent Input` 直接需要的 scene 配置，而不是 `Service` 的业务受理配置。

## 4. 数据对象边界

Dayu 在 Agent 执行路径上需要固定三类对象：

- `Request DTO`
- `Execution Contract`
- `Agent Input`

它们不是新的层，而是跨层契约或内部产物。

### 4.1 Request DTO

`Request DTO` 是 `UI -> Service` 的请求契约。

它回答的问题只有一个：用户这次显式提交了什么。

它不应承载启动期环境来源或稳定依赖。

这里的显式参数可分为两类：

- 领域显式参数，例如 `ticker`。
- 通用执行显式参数，例如 `model_name`、`temperature`、`max_turns`。

两类参数都必须先进入 `Service`，由 `Service` 决定是否接受。

默认不属于 `Request DTO` 的内容包括：

- `workspace path`
- `config path`
- `DocumentRepository` 或其它领域仓储对象
- `Host`、`ConfigLoader`、`PromptAssetStore` 等稳定依赖

这些内容应由 `startup preparation` 收敛并在启动期注入，而不是作为请求字段在运行时传递。

### 4.2 Execution Contract

`Execution Contract` 是 `Service` 产出的稳定执行契约。

更准确地说，它通常由 `Service` 调用 `Contract preparation` 后生成，再提交给 `Host`。

它回答的问题是：这次 Agent 子执行已经确定了哪些执行约束，以及宿主应如何托管它。

它应承载的核心信息包括：

- `service_name`
- `scene_name`
- `host_policy`
- `preparation_spec`
- `message_inputs`
- `accepted_execution_spec`
- `metadata`

其中：

- `host_policy` 描述 Host 生命周期治理约束。
- `preparation_spec` 描述 scene preparation 需要的机械装配说明。
- `message_inputs` 承载当前轮用户输入。
- `accepted_execution_spec` 承载 Service 已接受的模型、预算、tool limits、web/trace/memory 配置，而不是 UI 原样传入的原始请求字段。
- `accepted_execution_spec.web_tools_config.allow_private_network_url` 必须同步写入 `preparation_spec.execution_permissions.web.allow_private_network_url`，后者才是 Host 落权限时的真源。

`Execution Contract` 首先描述的是单个 Agent 子执行单元，不强行充当所有服务路径的统一总契约。

### 4.2.1 术语约定

后续实现与评审必须固定区分这四个概念：

- `session`：Host 持有的长期会话身份。
- `run`：一次 Host 托管执行尝试。
- `conversation turn`：一次用户输入到一次 assistant 最终答复的提交单元。
- `agent iteration`：`AsyncAgent` 内部一次 LLM 调用加工具闭环。

当前聊天主链里，通常是一条用户输入对应一个 `conversation turn`，也对应一次 `run`；但一个 `run` 内可以包含多个 `agent iteration`。

因此：

- 恢复对象不应表述为“恢复旧 run”。
- `resume` 的 V1 正确语义是：恢复 pending `conversation turn`，并在同一 `session` 下新建一个 `run` 去重放它。
- 这里的“重放”是指重放同一条用户输入以及当时已被 Service 接受的通用执行参数，而不是套用恢复发生时 UI 当前的临时配置。
- `AsyncAgent` 内部的 iteration 不应再和 transcript / memory 中的 `turn` 混用同一术语。

### 4.3 Agent Input

`Agent Input` 是 `scene preparation` 在 `Host` 内部产出的最低可执行输入。

它通常包含：

- `system_prompt`
- `messages`
- `tools`
- `agent_create_args`
- `session_state`
- `runtime_limits`
- `cancellation_handle`

这里必须强调：

- `Agent Input` 是 `Host` 内部产物。
- 它不是和 `Agent` 的上层接口契约。
- 它只是 `Host` 在内部完成 scene 收敛后交给底层执行器的最终输入形态。

因此，架构上应把 `Agent Input` 视为 `Host` 内部执行产物，而不是上层可自由依赖的公共抽象。

## 5. 配置读取与 preparation 边界

配置相关的数据流转至少要分成四段：

1. `UI` 校验启动期原始输入，例如 `workspace path`、`config path` 是否存在且合法。
2. `startup preparation` 把这些原始来源收敛成稳定依赖，例如 `WorkspaceResources`、`ConfigLoader`、`PromptAssetStore`、`ModelCatalog`、`DocumentRepository`。
3. `Contract preparation` 读取形成 `Execution Contract` 直接需要的配置。
4. `scene preparation` 读取形成 `Agent Input` 直接需要的 scene 配置。

这里最关键的判断是：

- `Service` 不需要知道 config 在哪里。
- `Contract preparation` 可以读取 config 对象，但它读的是 contract-time config，而不是原始 config path。
- `scene preparation` 也可以读取 config 对象，但它读的是 scene-time config，而不是业务受理配置。

因此，配置边界不应表述成“Service 读 config，Host 不读 config”，而应表述成：

- `startup preparation` 负责把路径变成对象。
- `Contract preparation` 负责读取形成 `Execution Contract` 所需的配置。
- `scene preparation` 负责读取形成 `Agent Input` 所需的 scene 配置。

### 5.1 execution options 边界

`execution_options` 的边界应固定为三步：

1. `UI` 只负责传原始显式参数。
2. `Service` 决定是否接受这些通用执行参数。
3. `Contract preparation` 把它们收敛成 `accepted_execution_spec` 写入 `Execution Contract`。

之后：

- `scene preparation` 只消费已经受理的 `accepted_execution_spec`。
- `scene preparation` 可以结合 scene 静态策略生成最终执行输入。
- `scene preparation` 不承担参数受理职责。

因此，正确边界不是“Host 去理解 execution options”，而是：

- `Service` 负责受理。
- `Contract preparation` 负责契约化。
- `scene preparation` 负责 scene 相关落实。

### 5.2 resume gate

`resume` 的 Host 生效前提应固定为三项同时满足：

- `host_policy.resumable == True`
- `scene_definition.conversation.enabled == True`
- `host_policy.session_key` 非空

其中：

- `resumable` 只是 Service 受理后写入 contract 的治理意图。
- 对 `conversation.enabled == True` 的 scene，Service 可以默认把 `resumable` 写为 `True`，但这仍不改变 Host 才是真正 gate 持有者。
- 是否允许真正进入恢复路径，由 Host 在 `scene preparation` 或等价的 Host 机械校验边界决定。
- 当 `conversation.enabled == False` 时，即使请求方显式传入 `resumable=True`，Host 也不应静默降级，而应显式拒绝。

`timeout` 也遵循同样边界：

- `timeout_ms` 是 Service 受理后写入 `host_policy` 的治理意图。
- `timeout_ms=None` 表示“参数已受理，但当前不启用 deadline”。
- 真正的 deadline 计时、取消触发与取消原因记录都属于 Host 运行时语义。

### 5.2 请求通道与启动通道

`UI -> Service` 实际上至少有两条不同性质的通道：

- 请求期通道：传递 `Request DTO`。
- 启动期通道：通过 `startup preparation` 注入稳定依赖。

因此，`workspace` 在哪儿、`config` 在哪儿、`DocumentRepository` 在哪儿，这些问题默认不属于 `Request DTO`，而属于启动期装配边界。

### 5.3 三套真源

沿用户侧主链简化后，可以把系统看成：

`User -> 渠道 UI -> Service -> Host -> Service -> 渠道 UI -> User`

其中必须固定区分三套彼此独立的真源：

- 入站交付真源
  - 回答“用户消息是否已被系统可靠接收”。
  - 典型内容包括渠道消息 ID、去重状态、拉取位点、上游 ACK / receipt。
  - 这套真源属于 `UI` / 渠道适配层，而不属于 `Host`。
- 执行真源
  - 回答“这条已受理请求在 Host 内是否仍未完成、是否可恢复、是否已 internal success / failed / cancelled”。
  - 这套真源属于 `Host`。
  - pending `conversation turn`、`Host Run`、取消意图、timeout 都属于这一层。
- 出站交付真源
  - 回答“最终回复是否已经可靠送达用户”。
  - 典型内容包括 reply outbox、发送重试、渠道回执、补投递状态。
  - 这套真源可以作为 `Host` 暴露的一项可选通用能力被托管。
  - 具体 transport 发送与渠道回执回写仍属于 `UI` / 渠道适配层。

后续设计必须满足以下边界：

- `Service` 负责业务受理语义与回复语义，并在拿到 `Host` 终态结果后决定是否显式调用 `reply outbox` 能力；它不持有 transport 级真源。
- `Host` 始终持有执行真源；若启用 `reply outbox`，Host 只托管出站交付真源的持久化记录与状态机，不负责自动创建记录。
- `UI` / 渠道适配层负责真实 transport 发送、回执接收，以及把发送结果回写给 `Host` 的 delivery 能力。
- pending `conversation turn` 只表示“Host 内尚未完成的当前 turn”，不能拿来兼任出站 delivery outbox。
- 一次执行一旦达到 Host internal success，pending turn 就应结束；是否还需要对外补发，是出站交付真源的问题，而不是执行真源的问题。
- `Host` internal success 的 answer 不会自动进入 reply outbox；必须先交回 `Service`，再由 `Service` 决定是否显式提交 delivery request。
- 如果产品需要 reply reliable delivery，必须使用独立的 reply outbox 真源，而不是延长 pending turn 生命周期。

CLI 的结果文件属于 `UI` 交付边界。Host 执行完成、最终答案写入文件、进程资源清理结束是三个不同事实，不能用“进程尚未退出”推断“答案尚未生成”。

`prompt --output` 只在事件流正常结束、收到非空最终答案且未收到错误或取消事件时提交文件。CLI 停止状态提示并原子交付后，再等待事件循环及默认线程池清理。执行或交付失败时，CLI 保留已有文件并返回非零退出码。文件交付与 Host 执行状态、reply outbox 的重试和渠道回执分别管理。

当前 reply outbox 的 `claim` 语义只保证“领取发送权”这一步是独占的；`ack / nack` 仍然是对 delivery record 的状态回写，而不是对某个领取者身份的校验。

因此，如果后续需要更强的分布式 delivery worker 保证，例如防止 worker 崩溃后被其它 worker 接管期间出现旧 worker 误回写，就应在 reply outbox 真源上继续增强 `lease / owner token` 语义：

- `claim` 不只返回“已领取”，还返回可验证的领取身份。
- `ack / nack / renew` 只能由持有当前有效领取身份的一方提交。
- 领取超时后的重新接管，应由 Host 托管的 delivery 真源统一收敛，而不是交给渠道层各自约定。

## 6. 稳定执行形态

Dayu 至少存在三类稳定执行形态。

### 6.1 单次 Agent 执行

适用于 `prompt`、`interactive`、`wechat`。

按层看，稳定链路是：

`UI -> Service -> Host -> Agent`

按执行展开看，是：

`UI -> Service -> Contract preparation -> Host -> scene preparation -> Agent`

其中 `wechat` 是聊天服务的 UI 适配入口，不是新的业务层。

### 6.2 复合流水线

适用于 `write`。

一次业务请求内部可以触发多次 scene 级 Agent 子执行。

这里应固定的判断是：

- `write` 是复合流水线，不应被压扁成单个 scene 调用。
- 流水线内部的单个 Agent 子执行，仍然遵守 `Execution Contract -> Agent Input` 的边界。

### 6.3 Direct Operation

适用于 `Fins` 的下载、上传、处理等命令。

这类路径可以完全不经过 `scene preparation -> Agent`，但仍应由 `Host` 托管，从而复用统一的 run、取消、并发和事件治理能力。

## 7. 必须固定的边界

以下判断后续不应再摇摆：

- 架构层次只有四层：`UI / Service / Host / Agent`。
- `startup preparation` 是 public 模块，不是新层。
- `Contract preparation` 是 public 模块，不是新层。
- `scene preparation` 是 public 模块，不是新层。
- `Service` 是唯一业务理解层。
- `Host` 是生命周期拥有者，且没有业务理解能力。
- `scene preparation` 只负责按 scene 收敛执行输入。
- `Agent` 只执行已经准备好的消息交互。
- `Execution Contract` 首先描述单个 Agent 子执行单元。
- `Agent Input` 是 Host 内部产物，不是对 Agent 暴露的上层接口契约。
- 动态 prompt 只能以 `Prompt Contributions` 形式进入 contract，并由 `scene preparation` 统一追加到 scene system prompt 尾部。
- `selected_toolsets` 负责工具集合启用选择，`execution_permissions` 负责动态权限收窄，二者再与系统级硬边界和 scene 候选集合共同求交。
- `Host Session` 是唯一 session 概念。
- `run_id` 属于 Host 执行上下文，不进入 `Agent Input` 对外契约。
- pending `conversation turn` 只属于执行真源，不承担入站或出站交付真源职责。
- `reply outbox` 可以是 `Host` 暴露的可选公共能力，但它必须由 `Service` 显式调用；`Host` internal success 不自动入 outbox。

## 8. 典型坏味道

以下都是架构坏味道：

1. `UI` 自己拼装复杂执行依赖。
2. `Service` 直接驱动 `Agent`。
3. 把 `startup preparation` 写成运行时解释层，或让它参与请求期控制流。
4. 把 `Contract preparation` 写成新的解释层。
5. 把 `scene preparation` 写成新的架构层。
6. `Host` 反向理解高层业务目标。
7. `scene preparation` 负责受理或拒绝 execution options。
8. `Service` 自己手工拼 `Agent Input`。
9. `Agent` 反向理解 `ticker`、写作、审计、修复或配置文件结构。
10. 用 `scene manifest` 承担业务受理、缺失前提处理或服务身份判定。

## 9. 暂不在本文档内决定的实现细节

以下问题仍可继续讨论，但都属于在基线内选实现，而不是重新定义基线：

- `startup preparation` 的具体 API 形状。
- `Contract preparation` 的具体 API 形状。
- GeneralChat 公共装配模块如何拆分。
- `scene preparation` 是独立对象还是 Host 内部 facade。
- scene manifest 与工具动态注册如何衔接。
- 恢复点、检查点和副作用提交边界如何设计。

这些问题的判断标准只有一个：是否让 `UI`、`Service`、`Host`、`Agent` 四层边界更清晰，而不是更混乱。
