# Forge Agent：一个自主编程 Agent 的设计与思考

## 这是什么

一个基于 ReAct 架构的自主代码维护 Agent，能在陌生代码库中自主完成 Bug 修复与功能迭代，支持 CLI 一次性任务执行、交互式多轮对话（跨轮上下文保持）、以及 GitHub Issue 到 Pull Request 的全自动闭环——从读取 issue、克隆仓库、创建分支、运行 agent、到推送代码和创建 PR，无需人工介入

通过 tree-sitter 多语言 AST 解析自动构建代码库结构摘要（repo-map），让 agent 在做出任何工具调用之前就具备对目标仓库的全局认知，减少无效探索步骤。支撑 9 种编程语言的符号级代码分析，未安装解析器的语言自动降级为正则匹配，零中断

设计了两层上下文管理机制（消息条数滑动窗口 + token 预算分级裁剪），配合多语言 tree-sitter 代码库结构摘要，让 agent 在 80K token 预算下稳定执行 40+ 步的复杂修复任务

 设计四层 Shell 安全防护（黑名单硬拦截 → 只读白名单 → 交互确认 → Timeout/截断）及 Docker 沙箱隔离执行，形成面向不可信 agent 的纵深防御执行框架

通过 Runtime 抽象将命令执行环境与工具逻辑解耦，支持本地/Docker 两种执行模式零代码切换；通过异常契约和双层兜底捕获保证单点工具失败不中断整体运行

---

## 整体架构

从外到内分四层。

**入口层**（`entry/`）：CLI 命令行和交互对话两种使用模式，负责解析参数、组装组件、把控制权交给 Agent 循环。另有一个 GitHub Issue 自动化入口，能拉取 issue、创建分支、运行 agent、推送并创建 PR，形成完整的 issue-to-PR 闭环。

**核心层**（`agent/`）：Agent 的大脑。包含 ReAct 主循环、system prompt 管理、事件日志系统和所有数据模型定义。这一层不关心 LLM 的具体 API 协议，也不关心工具的具体实现。

**LLM 抽象层**（`llm/`）：用统一的 `LLMBackend` 接口屏蔽不同 LLM 提供方的差异。目前覆盖 Anthropic Claude、OpenAI、DeepSeek、Groq、Ollama 五种后端。Agent Core 只依赖抽象接口，永远不 import 具体 SDK。

**工具层**（`tools/`）：12 个具体工具（Shell 执行、文件读写、代码搜索、测试运行、Git 操作），全部通过 `BaseTool` 抽象注册到 `ToolRegistry`。工具和执行环境通过 Runtime 抽象解耦——可以本地跑，也可以 Docker 沙箱里跑，工具代码不需要改动。

夹在核心层和工具层之间的还有**上下文管理模块**（`context/`），负责对话历史的滑动窗口、token 预算分配与裁剪、以及 repo-map 的生成——这是让 agent 在长任务中不迷失方向的关键基础设施。

数据流是单向且清晰的：入口层构造 Task → Agent 循环读 Task 和配置 → 每步组装 messages 调 LLM → 拿到 Action 调 ToolRegistry → 拿到 Observation 写回历史 → 检测终止条件 → 返回 RunResult。

---

## Agent 循环：不只是"调 LLM、跑工具"

主循环在 `Agent.run()` 里，是一个 for 循环，每一步走五个阶段：组装消息、调 LLM（带重试）、解析 Action、执行工具、检测终止/触发反思。

看起来是标准的 ReAct，但每个阶段里都有刻意为之的设计。

### Reflection 不是"重置"，是"提醒"

很多 Agent 系统在检测到异常时，会重置对话或切到一个特殊的 recovery prompt。我刻意不走这条路——Reflection prompt 以 `role="user"` 消息的形式直接注入当前对话历史，就像有人在 agent 耳边低声提醒了一句。agent 不会感觉"被重启了"，上下文的连续性得以保留。

目前有两个反射触发条件。一个是"测试工具返回失败"——这是最直接的反馈信号，agent 需要重新审视自己的修改。另一个是"连续 N 步没有做任何文件编辑"——agent 可能陷入了"读这个文件、再读那个文件、再读回来"的探索死循环。用"你没在改东西"作为反思触发条件，是我在实际跑任务时发现的一个痛点——agent 很容易在大型代码库里迷失，无限探索而迟迟不动手。

### 死循环检测：不只是比较字符串

`_is_looping()` 检测最近 N 步的 `(tool_name, params)` 是否完全相同。这个逻辑很简单，但有个容易被忽略的细节：它只对 `TOOL_CALL` 类型的 action 做检测。如果 agent 连续发了几条空 reflection 或 finish 却被后端解析器错误地处理了，这些不会被误判为死循环。另外检测窗口是可配置的——短窗口敏感但不稳定，长窗口稳定但反应慢，默认 3 步是一个折中。

### 跨轮对话的"最小侵入"实现

这是整个项目中最满意的一个工程决策。`ChatSession` 需要在多轮对话之间保持 agent 的上下文，但 `Agent.run()` 默认会创建新的 `ConversationHistory`。我没有去重构 `run()` 的签名或引入一个复杂的 session manager，而是在 `run()` 的开头加了一个四行的检查：

```python
if hasattr(self, "_pending_history") and self._pending_history is not None:
    history = self._pending_history
else:
    history = ConversationHistory(max_messages=self._cfg.history_max_messages)
```

`ChatSession` 在调用 `run()` 之前设置 `agent._pending_history = self._shared_history`，调用完后删除这个属性。一个 attribute 的 set/check/delete，实现了跨轮状态的注入，零侵入、零依赖、可测试。

这个模式在别的地方也用到了——`ChatSession` 通过 monkey-patch `EventLog._append` 实现实时事件打印，`AnthropicBackend` 通过 monkey-patch 绑定 `stream()` 方法。这些不是"干净"的架构，但它们是"恰到好处"的务实选择。如果为了"干净"引入事件总线或插件系统，代码量会翻倍而价值不大。

---

## 上下文管理：让 agent 在长任务中不迷路

上下文窗口是所有 Agent 系统的硬约束。当任务需要 20 步以上的探索和修改时，早期产生的工具输出会成为噪声，挤占后期的推理空间。

### 双层裁剪：条数 + Token

我用了两层独立的裁剪机制。第一层是 `ConversationHistory` 的消息条数限制——这是一个粗粒度的滑动窗口，超出后从索引 1 开始丢弃（索引 0 的任务描述永不丢弃）。第二层是 `TokenBudget` 的 token 级裁剪——每次组装消息发往 LLM 之前，按 token 计数做二次裁剪，从旧消息往新消息方向贪婪剔除。

两层分离是因为它们的制约因素不同。条数限制防止 LLM API 的 message 数量超限（有些 API 有硬性的消息条数上限），token 限制防止总上下文超出模型窗口。两个限制独立配置，各自裁剪自己的维度。

裁剪会插入占位提示（"有 N 条更早的消息被截断"），这不是完美的解决方案——agent 仍然不知道被丢弃的消息里有什么关键信息——但它至少让 agent 知道自己"失忆了"，而不是无声无息地丢失上下文。更好的做法应该是先做语义压缩（让 LLM 对旧消息生成摘要），再做丢弃，这是当前设计的一个重要改进方向。

### Repo-map：在动手之前先给一张地图

Repo-map 是注入 system prompt 的一段代码库结构摘要。用 tree-sitter 扫描仓库中的源码文件，提取函数和类的定义，按"重要性"排序（顶层定义比方法重要，小文件比大文件更像核心模块），然后在 token 预算内输出一个结构化的摘要。

这个设计解决的是一个很具体的问题：agent 在拿到任务后，往往会先花 3-5 步做"探索"——列出文件、读取关键文件、搜索符号。repo-map 直接把这一步的结果预加载到了 system prompt 里，agent 在第一步就能看到代码库的大致轮廓，可以更快地进入"定位问题"阶段。

未安装 tree-sitter 语言包时自动降级为正则解析，不会因为缺少某个语言包而整个功能崩溃。这个降级策略贯穿了整个项目的设计哲学——**渐进增强，优雅降级**。

### 缓存策略

Repo-map 在第一步生成后缓存，后续步骤直接复用。缓存按 `repo_path` 做 key，换仓库时自动失效。`ChatSession` 跨轮之间 repo-map 也不需要重建——agent 对象复用，缓存一直有效。只有用户切换到不同的仓库时，cache key 不匹配，才会触发重建。

---

## 处理不支持 Function Calling 的模型

这是项目中投入最多工程精力的一个模块。不是所有模型都支持原生的 function calling——DeepSeek R1 这样的 reasoning 模型只能输出纯文本。但你不能因此就放弃工具调用能力。

我的方案是让 `OpenAICompatBackend` 在初始化时通过模型名前缀自动判断是否支持 function calling。如果不支持（模型名以 `deepseek-reasoner` 或 `deepseek-r1` 开头），就走文本解析路径。

文本解析的核心逻辑分三步。第一步，在 system prompt 末尾注入一段工具描述和输出格式要求，告诉模型"如果要调用工具，输出一个特定格式的 JSON block"。第二步，从模型的文本输出中按优先级提取：先匹配 ` ```json ``` ` 代码块，再匹配内联 JSON，最后做关键词匹配（TASK_COMPLETE / GIVE_UP）。第三步，把 JSON 字符串解析为结构化的 `Action`。

三个层次的匹配不是冗余——JSON block 匹配处理"规矩"的输出，内联 JSON 匹配处理模型忘记加代码块标记的情况，关键词匹配处理 JSON 解析全部失败的兜底场景。每一层都比上一层更宽松，但也都更不可靠，所以按严格度排序优先级。

`_try_parse_tool_json()` 里有一个容易被忽略的细节：它同时兼容了 `"tool"`、`"name"`、`"function"` 三种字段名来提取工具名，以及 `"params"`、`"arguments"`、`"input"` 三种字段名来提取参数。这不是过度设计——不同模型的训练数据里可能见过不同的 JSON 字段约定，兼容多种命名比要求模型遵循单一格式更健壮。

---

## 工具体系：让失败不传染

### 异常契约：工具不抛异常

所有工具必须遵守一条铁律：`execute()` 不能抛出未捕获的异常。任何错误——文件不存在、正则语法错误、Shell 命令失败——都必须封装在 `ToolResult(success=False, error=...)` 里返回。

这条契约的动机很明确：Agent 循环不应该因为一个工具调用失败而崩溃。如果 agent 第 15 步时 `file_read` 因为一个编码错误抛了异常，整个运行中断，前面 14 步的上下文全部丢失，这是不可接受的。

`ToolRegistry.execute_tool()` 还加了一层兜底——即使某个工具实现违反了契约、真的抛了异常，registry 也会捕获并封装成 error 结果。两层保护：第一层是"设计约束"，第二层是"防御性兜底"。

### Shell 安全的四层防御

Shell 执行是攻击面最大的工具——它可以直接操作系统。我没有只做一个黑名单，而是拉了四道防线。

第一道防线是硬黑名单。`rm -rf /`、`mkfs`、fork bomb 这些明显破坏性的命令，匹配到就直接拒绝，不给任何绕过的机会。黑名单是字符串匹配而非正则，因为这里的威胁模型是"明显恶意"而非"精心构造的攻击"。

第二道防线是只读白名单。`ls`、`grep`、`git status`、`pytest` 这些只读或安全命令，直接放行，不给用户制造不必要的确认负担。白名单判断时专门检查了 `>` 写重定向——即使是 `echo` 这样的安全命令，如果带了 `> file` 写入重定向，也不算只读。

第三道防线是权限确认。不在白名单中且包含危险关键词（`rm`、`git push`、`npm install`、`sudo` 等）的命令，会弹出交互式确认。确认回调是可注入的——在测试或 CI 环境里可以替换为 `always_allow` 或 `always_deny`。非交互式终端（如管道或 CI）默认拒绝，保证不会静默执行危险命令。

第四道防线是 timeout 和输出截断。命令默认 30 秒超时，防止挂起。输出超过 8000 字符时截断，保留头部 60% 和尾部 40%——这种"掐头去尾留中间"的截断方式保留了命令输出的开头（通常是关键信息）和结尾（通常是执行结果），适合 agent 的阅读习惯。

### Runtime 抽象：工具和环境的解耦

Shell 工具、Pytest 工具、Git 工具都需要"执行命令"，但它们不应该关心命令是在本地跑还是 Docker 容器里跑。`Runtime` 抽象把命令执行从工具逻辑中分离出来——所有工具通过 `runtime.exec()` 执行命令，`LocalRuntime` 直调 subprocess，`DockerRuntime` 通过 `docker exec` 在沙箱容器中执行。

容器是懒启动的——首次调用 `exec()` 时才创建，后续 `docker exec` 复用同一个容器。默认断网（`--network none`），repo 通过 bind mount 挂载。`cleanup()` 时删除容器。

这个抽象让"是否沙箱化"变成了一个配置选项，而不是工具的实现细节。在 `entry/cli.py` 里一行 `create_runtime(sandbox=True)` 就能切换，所有工具自动在容器里运行。

### 文件工具：防止上下文爆炸

`file_read` 对大文件做了行数截断——超过 500 行时只返回前 500 行，并携带行号。输出末尾提示总行数和"用 file_view 分页继续阅读"。`file_view` 提供 100 行一页的分窗口浏览，返回当前窗口的行号和导航指令（"下一页：file_view start_line=X"），让 agent 能像翻书一样阅读大文件。

这个设计是把"上下文预算意识"嵌入到了工具层——工具不只是"执行操作"，还要"帮助 agent 节省 token"。一行一行的行号不是装饰，它让 agent 在后续的 file_write 中能精确引用行号，不需要再搜索定位。

### 搜索工具的自限设计

所有搜索工具都内置了严格的结果上限：`MAX_RESULTS = 50`，单行显示 `MAX_LINE_LENGTH = 200`。自动跳过 `.git`、`__pycache__`、`node_modules` 等无关目录。这些限制不是配置项——它们是硬编码的设计约束，表达的是"工具层有责任防止上下文爆炸"的立场。如果搜索返回 2000 个匹配，agent 的上下文会被瞬间撑爆，不如在源头截断并告知"还有更多结果"。

---

## 流式输出与思考分离

推理模型（DeepSeek R1、Claude with extended thinking）的输出有两个层次：推理过程中的"思考"（reasoning tokens）和最终给用户的"回答"。如果把两种内容混在一起打印，用户看到的是杂乱无章的文本流。

流式回调用了两个独立的 callback——`on_thought` 和 `on_text`。在 OpenAI 兼容后端里，`reasoning_content` delta 走 `on_thought` 以暗色（dim）打印，`content` delta 走 `on_text` 以正常颜色打印。用户视觉上能区分"模型在思考"和"模型在回答"，但又不会丢失任何信息。

非推理模型没有 `reasoning_content`，`on_thought` 全程不触发，`on_text` 直接打印模型输出。这个兼容性是无感的——用同一个 chat 界面，不同的模型给出不同的视觉呈现，但代码逻辑不需要分支。

---

## 测试策略：每一层 mock 刚好它的邻居

测试文件的组织方式本身就是在表达"每一层只测自己的逻辑"。11 个测试文件覆盖从数据类到多轮对话的全部层次，每层用不同的 mock 策略。

`test_day1` 测数据类和事件日志——不 mock，因为数据对象是纯逻辑、文件 I/O 是确定性的。`test_day2` 测 agent 主循环——`MockBackend` 完全替代 LLM，`NoopTool` / `FailingTool` 替代真实工具，这样 finish、give_up、max_steps、死循环、reflection 触发的所有控制流分支都能在毫秒内跑完，不需要 API 调用。`test_day3` 测 12 个工具——完全不用 mock，在临时目录里真实读写文件、真实执行 `pytest` 和 `git init`，因为工具的价值就在于真实执行。`test_day4` 测 LLM 后端——用 `unittest.mock.patch` 拦截 SDK 调用，验证发给 API 的请求格式正确、收到的响应解析正确。

`MockBackend` 是整个测试体系的基石。它不只是返回预编排的 Action——它还记录了每次 `complete()` 收到的完整 messages 列表（`received_messages`），让测试可以断言"agent 在反射触发后确实把 reflection prompt 注入到了 LLM 的输入里"。这是从"测行为"到"测因果"的升级。

---

## 配置系统：三层覆盖

配置优先级是 CLI 参数 > 用户指定的 YAML > `config/default.yaml` > 全默认值。这四层的合并逻辑在 `merge_cli_overrides()` 里只有十几行——CLI 传来的非 None 值直接覆盖对应字段，不需要复杂的深度合并。配置文件找不到时返回全默认值的 `AppConfig()`，agent 仍然能启动（只是需要用户通过 CLI 至少传入 provider 和 api_key）。

YAML 支持 `${ENV_VAR}` 环境变量展开，通过正则替换实现，不引入模板引擎。敏感信息（API key）永远不写在 YAML 里，通过 `${DEEPSEEK_API_KEY}` 占位符在运行时从环境变量注入——这是安全边界的设计，不是实现细节。

---

## 事件日志：一条 JSONL 里的完整运行记录

`EventLog` 是 append-only 的 JSONL 文件。每条 event 写入后立即 `flush()`，这意味着即使 agent 进程被 `kill -9`，已写入的事件也不会丢失。六种事件类型（TASK_START、ACTION、OBSERVATION、REFLECTION、TASK_COMPLETE、TASK_FAILED）覆盖了运行的完整生命周期。

日志不只是调试工具——`get_actions()` 从日志中提取所有 Action 用于死循环检测，`summarize_run()` 生成运行后的统计分析（每个工具被调用了多少次、成功/失败比例、最终状态）。这两者一个在运行时被 agent 循环消费，一个在运行后被 CLI 的 `log show` 命令消费。同一份数据，两种读法。

文件以 `{task_id}_{timestamp}.jsonl` 命名，多次运行不覆盖。支持 `replay()` 全量回放和 `iter_events()` 惰性迭代——前者用于 CLI 的一次性运行（跑完了全部打印），后者用于大文件的逐条分析。

---

## 已知局限与改进方向

当前最核心的局限是**单 Agent 线性执行**。探索（搜索、读文件）和编辑（写代码、跑测试）在同一个上下文窗口里竞争 token 预算。当代码库规模增大时，探索产生的工具输出会大量消耗上下文，挤压推理和决策的空间。这个问题的解法是引入 sub-agent 架构——让主 agent 把探索类任务委托给专门的 Explore agent 并行执行，拿回精炼过的摘要而非原始输出，把决策上下文留给真正需要的地方。

第二个局限是**上下文只裁剪不压缩**。超出预算的消息直接被丢弃，插一条"有 N 条被截断"的提示。Agent 知道自己忘了东西，但不知道忘了什么。正确的做法是先让 LLM 对即将丢弃的消息生成一段结构化摘要（改了什么、测试结果是什么、发现了什么关键信息），保留语义而非保留原文。

第三个局限是**无跨 Session 持久化**。每次 `agent chat` 启动时都是从零开始，不记得用户偏好、项目约定、之前的决策。应该像 Claude Code 那样在 `~/.forge/memory/` 下维护文件级记忆，按类型（用户偏好、项目上下文、反馈经验）组织，每次对话自动加载相关记忆注入 system prompt。

第四个局限是**工具集静态注册**。12 个工具硬编码在 `_build_registry()` 中，没有外部工具接入机制。接入 MCP（Model Context Protocol）可以让 agent 动态获取外部工具能力——数据库查询、API 调用、第三方服务——而不需要修改 agent 本身的代码。

最后一个局限是**无断点恢复**。长任务中断后只能从头开始。应该每隔 N 步自动保存完整的运行状态（对话历史 + 代码 diff + 工具调用栈）到 checkpoint 文件，支持 `agent resume --checkpoint X` 从中断点继续。

---

## 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.11+ | LLM/AI 生态最完整，迭代速度快 |
| CLI | Click | 装饰器风格、子命令天然支持、参数校验零代码 |
| 配置 | YAML + dataclass | 人类可读、类型安全、${VAR} 展开 |
| LLM SDK | Anthropic + OpenAI | 原生 SDK 保协议兼容，OpenAI SDK 通过 base_url 覆盖 4 种 provider |
| 代码分析 | tree-sitter | 多语言精确 AST 提取，未安装语言自动降级正则 |
| Token 计数 | tiktoken | 精确计数，不可用降级字符/4 估算 |
| 测试 | pytest + tmp_path | fixture 隔离、MockBackend 零 API 消耗 |
| 沙箱 | Docker | 容器隔离 + 断网 + bind mount |
