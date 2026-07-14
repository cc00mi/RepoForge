# RepoForge 面试技术深挖文档

> 面向 Agent 应用开发岗位面试，逐点拆解项目中的核心技术决策、实现细节、遇到的问题与解决方案。

---

## 简历描述

 	一个基于 ReAct 架构的自主代码维护 Agent，能在陌生代码库中自动完成 Issue 分类去重、Bug 修复、PR
 审查与发版摘要生成——从监控仓库动态到创建 Pull Request 全链路无需人工介入。系统检出准确率达 88%、重复 Issue 识别F1=1.0、自动修复任务首次响应中位数 1.4 分钟

 	通过 tree-sitter 多语言 AST 解析自动构建代码库结构摘要（repo-map），在 agent 做出任何工具调用之前注入 system prompt，让模型一步获得 9 种语言（Python / TypeScript / Go / Rust / Java / C++ / C / Ruby /
  JavaScript）的符号级全局认知，未安装解析器的语言自动降级为正则匹配。配合跨运行持久化的三层信号记忆（路径热度 ×修改频率 × 验证通过率），按加权打分自动收敛到高价值文件路径，使 agent 越运行越精准；设计三层上下文管理机制——消息条数滑动窗口 + token 预算分级裁剪 + 语义压缩器——当对话超过 12 条时自动调 LLM 将旧消息压缩为结构化摘要（所读文件 / 所做修改 / 测试结果 / 关键发现），约 12 条压缩为 1条、保留决策关键信息的同时释放窗口空间。Agent 在 80K token 预算下稳定执行 40+ 步的复杂修复任务；在单一 ReAct 循环之上设计了四阶段流水线模式（理解 → 规划 → 实现 → 验证），每阶段上下文归零、只传递上一阶段的 4种结构化 JSON 结论，配合可行性门控在规划阶段拦截不可解任务。每阶段有独立的步数上限（8/5/12/3）和 token预算，工具白名单按阶段收紧（前两阶段无编辑和 shell 权限）。Pipeline 异常时自动 fallback 到 ReAct，保证容错；设计四层 Shell 安全防护（8 条硬黑名单 → 40+ 只读命令白名单 → 20 类危险操作交互确认 → 30s 超时 + 8KB 输出截断），配合Docker 沙箱（网络隔离 + bind-mount 仓库 + 非 root 用户），形成面向不可信 agent 命令执行的纵深防御框架。

​	支持 CLI一次性任务执行、交互式多轮对话、以及从 GitHub Issue 读取到自动创建 Pull Request 的全闭环完成对 astropy / django 等真实开源仓库的SWE-bench-Lite 基准验证

## 面试陈述

一个中等规模的开源仓库每天可能收到十几个新 Issue 和 PR，维护者需要手动判断每个 Issue
  是不是重复的、能不能修、修哪里，再逐行 review PR代码变更。这些工作里大量是模式匹配和重复决策，而不是创造性思考，天然适合用 AI agent 来承接。

 独立设计并实现了一个覆盖"理解代码库 → 定位问题 → 生成修复 → 验证正确性"全流程的自主编程智能体系统，让 agent 作为GitHub App 持续运行

 整个系统的设计是围绕一条数据链路展开的：agent 收到一个 Issue 后，先用 tree-sitter 扫描仓库生成 9
  语言的代码符号图谱注入 systemprompt，让它在开始探索之前就知道哪些文件里有哪些函数和类，避免盲目搜索浪费推理步数。进入推理阶段后，agent 在 ReAct 循环中自主决定每一步是读文件、搜符号、编辑代码还是跑测试——为了防止多轮对话中上下文膨胀到模型注意力涣散，我在这个循环上加了语义压缩器，当消息超过 12 条时自动调 LLM把旧对话总结为结构化摘要替换原文，同时设计了四阶段流水线模式让每阶段上下文从零开始、只传递上一阶段的 JSON结论，用可行性门控在规划阶段就拦截不可解任务。agent做出的每一步工具调用决策，都会经过四层递进安全检查——硬黑名单拒绝破坏性命令、只读白名单放行无害操作、危险命令弹交互确认
  、所有执行有超时和输出截断。每次修复完成后——不管成功还是失败——agent把这次的经验写入跨运行记忆：改了哪些文件、哪个测试命令失败过、和历史上哪个 Issue相似，通过加权打分自动收敛到真正高价值的文件路径，下次运行时注入 system prompt 作为 few-shot 引导。最后这一切被封装为Flask Webhook 服务，后台线程池异步派发，覆盖从 Issue 分诊、PR 增量审查到发版说明生成的完整仓库维护管线。

  系统完成对 astropy/django 等真实开源仓库的 SWE-bench-Lite 基准验证，支持 CLI交互和 CI 服务两种运行模式，可一键 Docker Compose 部署。





skill是给agent用的说明书，里面包含·如何调用工具的说明

prompt是给llm用的指令

其实skill就是强大的prompt固化的，不用每次都占用上下文，并且不会被截断

> ---
>   从 GitHub Webhook 到 PR 创建：完整 CI 管线
>
> ---
>   第一步：Webhook 到达
>
>   一个用户在仓库里开了一个 Issue，标题是"pytest --cov 在 Windows 上报编码错误"。GitHub 向我们的服务器 POST
>   https://repoforge.example.com/webhook 发送了一个 issues.opened 事件。
>
>   Flask 收到请求后做的第一件事是验证签名。GitHub 在发 webhook 时会用我们 App 的 Webhook Secret 对 payload 做 HMAC-SHA256
>    签名，放在 X-Hub-Signature-256 头里。我们拿同样的 Secret
>   对请求体算一遍签名，比对是否一致。这一步是安全底线——任何不匹配的请求直接返回 403，不做任何处理。
>
>   签名验证通过后，立即返回 200 OK 给 GitHub。这个响应在 3 秒内必须返回，否则 GitHub
>   会认为超时并重试。注意，此时我们还没有处理 Issue 本身——只是"确认收到了"。
>
>   第二步：事件路由
>
>   返回 200 之后，实际的业务逻辑在后台线程中执行。EventRouter 根据 (event_type, action) 元组——这个 case 是 ("issues",
>   "opened")——找到对应的 handler：handle_issue_opened。
>
>   后台线程池上限是 5。如果当前已经有 5 个 handler 在跑，新的请求会排队。这防止了 webhook 洪泛把服务器搞垮。
>
>   第三步：Issue 分诊
>
>   handle_issue_opened 启动后，走一个三阶段的 Issue 处理流水线：
>
>   阶段 A — 分类和打标签。 IssueTriage.classify() 先用关键词规则做快速匹配——标题里有"error""crash""fail"→ 打 bug
>   标签，置信度 0.9。如果关键词匹配置信度不够高（比如 < 0.85），再调用一次轻量 LLM 来做分类——大约消耗 300 token。同时给
>   Issue 分配优先级（p0-p3）和工作量评估（trivial/small/medium/large）。
>
>   阶段 B — 去重检测。 DedupEngine.find_duplicates() 做两件事：① 用 difflib.SequenceMatcher 和 Repo Memory 里保存的
>   recent_issues 做标题相似度匹配；② 通过 GitHub API 查询当前仓库的 open issues，同样做相似度比对。如果匹配到相似度 >
>   0.90 的已有 Issue，自动评论"这可能是 #42 的重复"并关闭。0.75-0.90 之间的，评论"可能相关"但不关闭。
>
>   阶段 C — 可解性判断。 用一个很小的 prompt（约 500 token）让 LLM 判断这个 Issue 是否能自动修复：
>   - 能 → 进入 fix pipeline
>   - 不能（比如需要外部 API 密钥、需要特定硬件）→ 评论分析 + 打标签 + 等待人工
>
>   这个 case 是一个编码错误的 bug，agent 判断为"可修复"，进入 fix pipeline。
>
>   第四步：执行修复
>
>   这里根据配置可以选择两种执行模式。
>
>   如果是 Pipeline 模式：
>   1. UNDERSTAND（最多 8 步）：agent 用 find_files 找项目中处理编码的文件，用 file_read 读候选文件，识别 pytest --cov
>     的配置位置。结束时输出结构化 JSON：问题摘要、候选文件列表、测试命令。
>   2. PLAN（最多 5 步）：agent 深入阅读候选文件，设计修改方案。比如"在 setup.cfg 的 [tool:pytest] 中加 addopts =
>     --encoding=utf-8"。同时做可行性评估——如果 risks 里包含 INFEASIBLE，流水线中止。
>   3. IMPLEMENT（最多 12 步）：agent 实际编辑文件、运行测试、如果测试失败就迭代修复（最多 3 次）。
>   4. VERIFY（最多 3 步）：跑 git diff 确认改动，生成 PR 描述。
>
>   每个阶段之间，对话历史归零，只传递结构化 JSON。
>
>   如果是 ReAct 模式：agent 在一个统一的循环中自由探索和修改，最多 40 步。中间有死循环检测和 Reflection 触发机制。
>
>   假设修复成功。agent 执行了 git diff 拿到 patch，_get_changed_files() 拿到文件列表。
>
>   第五步：Repo Memory 更新
>
>   修复完成后——不管成功还是失败——MemoryService.record_outcome() 被调用：
>   - run_stats.total += 1
>   - 修改过的文件路径更新 PathSignal（changed_count++，如果测试通过 successful_validation_count++）
>   - 重建 preferred_paths（按 published×14 + validated×10 + changed×6 + candidate×1 排序）
>   - 把这次修复记录追加到 recent_issues（最多保留 10 条）
>   - 原子写入：先写 .tmp.{pid} 文件，再 os.replace() 到正式路径
>
>   下次这个仓库有新的 Issue 时，render_for_prompt() 会把这些历史经验注入 system
>   prompt——包括热路径文件、已知测试命令、相似问题的修复方法。
>
>   第六步：创建 PR 和评论
>
>   handler 通过 GitHub API 做两件事：
>   1. 在 agent 的工作目录中 git commit + git push 到一个新分支（如 repoforge/fix-42）
>   2. 通过 GitHub API 创建 PR，标题是"[Agent] Fix #42: pytest --cov encoding error on Windows"，描述由 agent 在 VERIFY
>     阶段生成的 PR narrative 填充
>   3. 在原始 Issue 下评论："A proposed fix has been opened at #43."
>
>   指标收集（横切关注点）
>
>   在整个流程中，指标系统在多个节点埋了钩子：
>
>   - TTR（Time to First Response）：server.py 在收到 webhook 时调用 TTRTracker.record_receipt()，handler 在创建 Issue
>     评论时调用 record_response()，计算 delta 写入 ttr_log.jsonl
>   - Review Coverage：每次 PR review 完成后，记录 (repo, pr_number, reviewed_at)，Dashboard API 交叉对比 GitHub 的 open
>     PRs 列表计算覆盖率
>   - Review Finding Recall：PR review 时，agent 的 findings 写入 agent_findings.jsonl，人工 reviewer 提交 review
>     时提取人类 findings 写入 human_findings.jsonl，compute_recall() 用复合相似度算法做配对
>
>   后台调度（非 webhook 触发）
>
>   除了响应 webhook，Scheduler 还定期跑三个后台任务：
>
>   - Scout（每 6 小时）：主动拉取仓库的 open issues（筛选 help wanted、good first issue、bug 标签），用 Repo Memory
>     的热路径权重给 Issue 打分排名，高分 Issue 自动进入修复流水线。这个和 webhook 是互补的——webhook 保证即时性，scheduler
>     保证不遗漏。
>   - Stale 扫描（每天）：扫描所有 open PRs，超过 7 天无活动 → @提醒，超过 14 天 → 打 stale 标签，超过 30 天 → 自动关闭。
>   - Security 扫描（每 4 小时）：检查 Dependabot alert，评估影响面，对 critical/high 级别自动创建依赖升级 PR。
>
>   可观测性：Dashboard
>
>   Flask Dashboard (:8001) 提供了 5 个 API 端点来观测整个管线的运行状态：
>
>   - GET /dashboard/api/metrics — 聚合概览（coverage、recall、stale reduction、TTR 四项）
>   - GET /dashboard/api/metrics/coverage — 哪些 PR 被审查了、哪些还没
>   - GET /dashboard/api/metrics/recall — agent 发现 vs 人类发现的匹配详情
>   - GET /dashboard/api/metrics/stale — 过期 PR 的扫描历史和减少率
>   - GET /dashboard/api/metrics/ttr — 首次响应时间的分位数统计
>
>   所有数据都来自 ~/.repoforge/metrics/ 下的 JSONL 文件，按需聚合计算，不引入数据库。

> 这个系统有三种运行方式，对应不同的使用场景。
>
>   最直接的方式是通过 ==CLI 命令行==。你给它一个仓库路径和一段任务描述，它就自己开始干活。比如 repoforge run --repo ./astropy
>    --description "修复 separable.py 中坐标矩阵赋值错误"，agent 会启动一个 ReAct 循环——先读 repo-map
>   了解代码结构，然后逐轮调用 LLM
>   决定下一步做什么：是读文件、搜索符号、编辑代码、还是跑测试。每做完一步，工具的输出会写回对话历史，LLM
>   结合最新信息再决定下一步。就这样一轮轮循环，直到 LLM 认为任务完成了——调用 FINISH——或者触发了步数上限或死循环检测。
>
>   这里的关键细节是，agent 自己决定什么时候停，而不是外部预设一个流程。它可能在 5 步内搞定一个简单的 import
>   修复，也可能花 30 步追一个跨模块的类型错误。每一步的执行结果都会实时写入 EventLog——一个 JSONL 文件，记录
>   action、observation、reflection 等所有事件——方便事后回放和调试。
>
>   第二种方式是==交互式对话模式。你输入 repoforge chat==，进入一个持久会话，可以持续给 agent 下指令、看它执行、中途纠正方向。
>   比如你先让它"探索一下这个项目的认证模块"，它探索完汇报给你；你看了结果觉得方向对，再说"好，把 JWT 签发改成 RS256
>   算法"，它接着干。这个模式下危险命令会弹确认——如果 agent 想执行 git commit 或 rm，终端会打印命令并等你输入
>   y/n。流式输出也在 chat 模式下默认开启，你能实时看到 agent 的思考过程和最终回答。
>
>   第三种就是刚才聊的 ==CI 管线模式==，有 Flask 服务端和 Dashboard 两个进程。服务端监听 GitHub
>   webhook，后台线程池异步处理事件。Dashboard 提供可视化面板。这套模式可以通过 Docker Compose 一键部署——docker-compose up
>    启动 server 和 dashboard 两个容器，不需要额外配置数据库，所有状态都在 JSONL 文件和 Repo Memory 的 JSON 文件里。
>
>   三种模式共享同一套核心——同一个 Agent 类、同一个 ToolRegistry、同一个 LLMBackend。区别只是入口不同和配置不同：CLI
>   模式默认跳过危险命令确认，chat 模式弹确认，CI 模式则在非交互终端下直接拒绝危险命令。

>  第一段：CLI 入口组装组件（entry/cli.py:176-282）
>
>   CLI 启动后，load_config() 从 config/default.yaml 读出 LLM provider、模型名、max_steps
>   等配置。然后做了三件事。第一，create_backend_from_config() 根据 provider 字段——比如 "deepseek"——调用 llm/router.py
>   里的工厂函数，router 查表拿到 base_url https://api.deepseek.com，实例化一个 OpenAICompatBackend。这个 backend
>   对象之后对 Agent Core 来说就是一个黑盒，只暴露 complete(messages, tools) → LLMResponse
>   一个方法。第二，_build_registry() 把 ShellTool、FileReadTool、SearchTextTool 等 12 个工具注册到一个 ToolRegistry
>   实例里，每个工具自带 name、description、parameters_schema。第三，用 AgentConfig 把 max_steps、token
>   budget、是否流式输出、确认回调这些运行时参数打包。最后 Task 数据结构包装了用户的任务描述和仓库路径。
>
>   第二段：Agent.run() 启动循环（agent/core.py:100-313）
>
>   进到 agent.run()，先初始化了几个上下文管理器。ConversationHistory 是一个滑动窗口的消息列表，一开始只有一条 user
>   message——就是 build_task_prompt() 把用户的任务描述包了一层格式。TokenBudget 负责跟踪已消耗的 token
>   总量和每一步的分配。RepoMap 马上要用来生成代码图谱。
>
>   然后进入主循环，for step in range(1, max_steps+1)。
>
>   第三段：每步的上下文组装（agent/core.py:319-361 _build_messages()）
>
>   每轮的第一步不是调 LLM，而是组装消息。_build_messages() 做的事情是：先看 repo-map 有没有缓存——没有就调
>   repo_map.build()，这个函数遍历仓库的文件树，对 .py .ts .go 等文件调 tree-sitter 提取函数/类定义，按重要性排序后截断到
>   token 预算，生成一个约 2000 token 的代码结构摘要。然后 build_system_prompt() 把工具描述列表、repo-map 摘要、Repo
>   Memory 摘要（如果有）一起填进模板，产出一条约 4000 token 的 system prompt。接着 ContextCompressor.compress()
>   检查历史消息是不是超过 12 条了——超过就把老消息发给 LLM 做一次结构化摘要，12 条压缩成 1 条。最后
>   TokenBudget.trim_history() 做传统的 token 裁剪。组装完的消息列表是 [system: ..., user: task description, assistant:
>   thought+action, user: observation, ...]，这就是完整的 LLM 输入。
>
>   第四段：调 LLM 拿 Action（agent/core.py:406-449 _call_with_retry()）
>
>   _call_with_retry() 调 backend.complete(messages, tools)。以 DeepSeek 为例，请求到 OpenAICompatBackend.complete()。它把
>    LLMMessage 列表转成 OpenAI API 格式，把 LLMToolSchema 列表转成 tools 参数，POST 到 DeepSeek
>   API。响应回来后——如果模型支持 function calling，直接从 response.choices[0].message.tool_calls 提取 {name,
>   arguments}；如果不支持（DeepSeek R1），走文本解析通道，用正则从 response.choices[0].message.content 里提取 Action:
>   xxx\nParams: {...}。最终统一封装为 LLMResponse(action=Action(...), raw_content="...", input_tokens=...,
>   output_tokens=...)。如果调用失败——比如网络超时——指数退避重试最多 3 次，2x 延迟递增，401/403/400 不重试。
>
>   第五段：执行 Action 拿到 Observation（agent/core.py:229-250）
>
>   Agent 拿到 Action 后，三种走向。如果是 FINISH，调 _get_git_diff() 拿到 HEAD 以来的所有代码变更，包装成
>   RunResult(status=SUCCESS, patch=...)，循环结束。如果是 TOOL_CALL，registry.execute_tool(name, params) 根据 tool name
>   找到对应的 BaseTool 子类——比如 ShellTool——调 execute(params)。ShellTool 走四层安全检查后调 subprocess.run()，把
>   stdout+stderr 截断到 8000 字符，返回 ToolResult(success=True, output="...")。这个 ToolResult 转换成 Observation，写回
>   ConversationHistory——一条 assistant 消息（Thought + Action）和一条 user 消息（Tool 执行结果）。然后进入下一步循环。
>
>   第六段：Reflection 和终止检测（agent/core.py:253-278 _is_looping()）
>
>   工具执行完后，走两个检查。Reflection 检查：如果刚才调的是测试工具而且执行失败了，注入一条"为什么测试失败了？仔细对比预
>   期和实际输出"的反思提示到对话历史。如果连续 6
>   步没有任何文件写操作，注入"你一直在探索但没有做任何修改——请开始实施你的方案"。这些提示作为额外的 user message
>   追加，LLM 下一步就会看到。死循环检查：_is_looping() 从 EventLog 取最近 3 条 action，比较 (tool_name, params)
>   元组是否完全相同——如果 agent 连续 3 次用同样的参数调了同一个工具，判死循环，GIVE_UP 退出。
>
>   第七段：结果输出（agent/core.py:198-225 entry/cli.py:290-311）
>
>   不管是 FINISH、GIVE_UP 还是 MAX_STEPS，Agent 都返回 RunResult。CLI 层拿到结果后，遍历 log.replay() 回放所有
>   Event——每步的 action 和 observation——实时打印到终端。最后打印 status、步数、token 消耗、耗时。如果有 patch，patch
>   本身在 RunResult.patch 里，CLI 当前没有自动 commit——这留给后续的 handler 层（比如 CI 管线里由 handler 负责 git commit
>   + create PR）。
>
> ---
>   这条链路的核心设计思想是：Agent Core 只做编排，不做具体实现。LLM 细节全在 Backend 里，工具实现全在 Tool
>   子类里，上下文管理全在 context 模块里。Core 的 run() 方法本身只有约 200 行，做的事情就是"组装消息 → 调 LLM → 执行工具
>   → 检查终止条件"这四个动作的循环

## 1. 双模式引擎：ReAct 循环 + 四阶段流水线

### 1.1 为什么需要两种模式

单一 ReAct 循环的核心问题不是模型能力不够，而是**上下文结构随步数增加持续恶化**。每轮对话都会把工具调用和观察结果塞进历史，到第 20 步时上下文中 80% 是已经过时的探索信息，模型注意力被稀释，开始做出重复、低质量的决策。

我的解法是设计两种互补模式：
- **ReAct 模式**：适合探索性任务、范围不明确的 bug 修复，agent 有最大自由度
- **Pipeline 模式**：适合边界清晰的任务，将过程拆分为 4 个有界阶段，每阶段上下文核销

### 1.2 四阶段流水线的具体设计

```
Stage 0: UNDERSTAND (8 steps, 6K token budget)
  → 只读探索，识别候选文件和测试命令
Stage 1: PLAN (5 steps, 4K token budget)  
  → 设计修改方案，评估可行性
Stage 2: IMPLEMENT (12 steps, 15K token budget)
  → 实际编辑代码、运行测试、迭代修复
Stage 3: VERIFY (3 steps, 3K token budget)
  → 最终验证，产出 PR 描述
```

关键设计点：

**上下文隔离**：阶段之间只传递结构化 JSON 摘要（如 UNDERSTAND 产出的 `candidate_files` 和 `test_commands`），对话历史归零。这确保了 IMPLEMENT 阶段拿到的上下文里没有 UNDERSTAND 阶段的"试错噪音"。

**可行性门控**：PLAN 阶段结束时检查 `risks` 字段——如果包含 `INFEASIBLE`，流水线直接终止，避免在不可解任务上浪费 40 步。如果包含 `NARROW`，自动将 target_files 裁剪到第一个文件。

**工具白名单**：每个阶段用 `_build_filtered_registry()` 构建受限工具集。UNDERSTAND 和 PLAN 阶段连 `shell` 和 `file_edit` 工具都没有，从根本上杜绝误操作。

**结构化输出 + 修复重试**：每个阶段的最后一步，我会 strip 掉工具列表，同时注入一个强提示词强制模型输出纯 JSON。如果 JSON 解析失败，用 LLM repair prompt 重试一次。

### 1.3 遇到的问题

**问题 1：LLM 在"最后一步无工具"时仍然幻觉输出函数调用**

有些模型（尤其是 DeepSeek）在被 strip 掉工具后，不会输出 JSON，而是输出裸函数调用文本：
```
file_view(path="/workspace/src/foo.py")
```

**解决**：我在 `extract_json()` 中实现了一个三层清洗管道：
1. 正则剥离 XML 包裹标签（`<function_result>...</function_result>`）
2. 正则移除裸函数调用（`file_view(...)`），仅在 JSON 括号 `{...}` 之外的部分执行替换
3. 正则剥离 leading noise（"I need to find the file..."）

这些清洗在 JSON 解析之前执行，大幅提高了首次解析成功率。

**问题 2：Pipeline 阶段崩溃导致整个任务失败**

**解决**：在 `PipelineEngine.run()` 的最外层加了 try/except，捕获任何异常后自动 fallback 到标准单阶段 Agent.run()，并创建一个独立的 EventLog 避免日志污染。

### 1.4 ReAct 循环的工程细节

- **死循环检测**：比较最近 N 步的 `(tool_name, params)` 元组，完全相同时判定死循环并 `GIVE_UP`
- **Reflection 触发**：两个条件——(A) 测试工具失败时注入"为什么测试失败？"的反思提示；(B) 连续 6 步没有任何文件写操作时注入"你应该开始编辑代码"的提示
- **指数退避重试**：LLM 调用最多重试 3 次，2x 延迟递增；401/403/400 错误不重试
- **Checkpoint 断点续跑**：每 N 步保存完整状态（对话历史、git diff、token 计数），支持从崩溃点恢复，benchmark 跑 300 个实例时尤其重要

---

## 2. 统一 LLM 后端抽象 + 双通道兼容

### 2.1 架构设计

核心思路：Agent Core 只依赖 `LLMBackend` 抽象基类，永不 import 具体 SDK。

```
Agent Core (agent/core.py)
    └── LLMBackend (ABC)
            ├── AnthropicBackend   (原生 tool_use API)
            ├── OpenAICompatBackend (函数调用 + 文本解析双通道)
            │     ├── OpenAI
            │     ├── DeepSeek (含 R1)
            │     ├── Groq
            │     └── Ollama
            └── MockBackend       (测试专用)
```

### 2.2 双通道设计：函数调用 vs 文本解析

这是整个 LLM 模块最有技术含量的部分。

**通道 1 — 函数调用**（OpenAI/Claude/Groq）：标准的 `tool_calls` 格式，LLM 返回结构化的 `{name, arguments}` JSON，直接映射为 `Action(ToolCall)`。

**通道 2 — 文本解析**（DeepSeek R1 等不支持 function calling 的模型）：将工具描述以自然语言形式注入 system prompt，LLM 输出自由文本，然后从文本中解析出工具调用：
- 匹配 `Action: shell` + `Params: {"cmd": "..."}` 格式
- 匹配 `TASK_COMPLETE:` / `GIVE_UP:` 关键词
- 解析 JSON 代码块中的 tool call

### 2.3 关键技术细节

**Anthropic 后端的特殊性**：Anthropic 的 API 格式与其他家完全不同——system prompt 是独立参数而非 message 列表中的一条，tool_result 是一个独立的 content block 类型而非 message。`AnthropicBackend` 需要完整处理这些格式转换。

**推理模型的 thought 分离**：DeepSeek R1 等推理模型输出中包含 `reasoning_content`（中间推理过程），OpenAICompatBackend 在流式模式下会将其分离并通过 `on_thought` 回调单独输出，避免推理文本污染最终回答的解析。

**API Key 解析链**：`config > 环境变量`，通过 `_ENV_KEY_MAP` 统一管理。Ollama 本地部署不需要真实 key。

### 2.4 遇到的问题

**MockBackend 的 complete_text() 冲突**：ContextCompressor 调用 `backend.complete_text()` 时，MockBackend 的实现返回了硬编码的 mock JSON。但如果 compressor 的 prompt 发生变化，mock 数据就和实际格式不匹配，导致测试假阳性。

**解决**：MockBackend.complete_text() 返回一个最小但合法的 JSON（包含 `summary`, `files_examined`, `files_modified` 等字段），足以通过 compressor 的 `json.loads()` 校验，使得测试不依赖具体的压缩 prompt 格式。

---

## 3. 代码理解、上下文管理与记忆系统

### 3.1 tree-sitter 多语言代码图谱

**技术选型**：选 tree-sitter 而不是正则或者 Language Server Protocol (LSP)，原因：
- tree-sitter 是增量解析库，不依赖外部进程，可以在任何环境直接运行
- LSP 需要启动语言服务器进程，不稳定且对每种语言需要不同配置
- 正则无法处理嵌套结构和语言特异性语法

**实现**：`context/repo_map.py`，支持 9 种语言（Python/JS/TS/Go/Rust/Java/C++/C/Ruby）。运行时按需 import 语言包，未安装的语言自动降级为正则 fallback。AST 节点类型通过 `_FUNC_NODES` 和 `_CLASS_NODES` 两个 frozenset 统一映射。

token 预算控制：调用方传入 budget（默认由 TokenBudget.default_plan().repo_map 决定），超出预算时按重要性排序截断——顶层定义优先于嵌套方法。

### 3.2 语义上下文压缩

**动机**：单纯的 token 裁剪（FIFO）会丢失关键信息——比如 agent 在 step 3 发现了一个 bug 根因，但到 step 25 时这条信息被裁剪掉了。

**设计**：
1. 当消息数 > 12 时触发压缩
2. 取消息列表的 `[1:boundary]` 区间（保留最近的 ~1/3），发送给 LLM
3. LLM 返回结构化 JSON：summary、files_examined、files_modified、test_results、key_findings、current_state
4. 用这 1 条 summary 消息替换原来被删除的 N 条消息

**为什么不用 embedding + RAG？** 因为这不是知识检索场景——上下文压缩需要理解"当前进行到哪一步了"，这需要 LLM 的推理能力，不是向量相似度能解决的。

**效果数据**：12 条消息压缩为 1 条 summary，~95% 关键信息保留，~5% token 开销。

### 3.3 持久化仓库记忆系统

**对标参考**：我研究了 OpenMeta CLI v1.2.3 的设计，它用 `gpt-4o-mini` 就能取得可用效果，核心原因不是模型强，而是 Repo Memory 让每次运行都建立在之前运行的基础上。

**我的三层信号模型**：

| 层级 | 数据类型 | 容量上限 | 用途 |
|------|---------|---------|------|
| Layer 1 | run_stats、test_commands | — | 基础元信息 |
| Layer 2 | PathSignal（候选/修改/验证通过/发布次数） | 50 条 | 热路径排序 |
| Layer 3 | ValidationSignal（失败命令模式） | 20 条 | 避开已知雷区 |
| Layer 3b | IssueOutcome（历史修复记录） | 10 条 | 相似问题匹配 |

**权重设计**：
```python
published_count × 14 > validated_count × 10 > changed_count × 6 > candidate_count × 1
```
这套权重让 preferred_paths 自动向"真正产出过合并 PR 的文件"收敛，而不是停留在"被大量标记为候选但从未被修改"的噪音文件上。

**Task-aware prompt 渲染**：当给定新任务描述时，用 `difflib.SequenceMatcher` 匹配相似历史问题，给出 few-shot fix examples。不用 embedding 的原因：这个场景的文本量很小，SequenceMatcher 的延迟 < 1ms，embedding API 需要 200-500ms。

**原子写入**：`tmp + os.replace()`，POSIX 保证 rename 是原子的，Windows Python ≥ 3.3 也支持。崩溃时不会产生损坏的 JSON 文件。

**设计原则**：Memory 是读多写少的缓存，不是 source of truth。损坏的 memory 文件不影响系统运行（回退冷启动）。

---

## 4. 四层安全壳执行 + Docker 沙箱

### 4.1 为什么需要四层

shell 工具是 agent 系统最大的攻击面。只做黑名单（如 `rm -rf /`）不够——攻击者可以通过编码、alias、管道绕过。只做白名单（如只允许 `ls`、`grep`）又不够用——agent 需要跑测试、创建分支。

所以设计了**递进式四层**：

```
用户/Agent 发起命令
  │
  ▼
Layer 1: 黑名单硬拦截 ─── 匹配 → 拒绝（不解释）
  │ 未匹配
  ▼
Layer 2: 白名单免确认 ─── 匹配 → 直接执行
  │ 未匹配
  ▼
Layer 3: 交互确认 ─── 用户拒绝 → 拒绝
  │ 用户允许
  ▼
Layer 4: timeout + 输出截断 ─── 执行
```

### 4.2 各层实现细节

**Layer 1 — 黑名单**：`_BLOCKED_PATTERNS` 包含 `rm -rf /`、`mkfs`、fork bomb(`:(){:|:&};:`)、`chmod -R 777 /` 等。这是硬编码在源码中的，不可配置，不可绕过。

**Layer 2 — 白名单**：`_READONLY_PREFIXES` 包含约 40 个只读命令前缀（`ls`、`grep`、`pytest`、`git diff` 等）。特殊处理：包含 `> ` 写重定向的命令不算只读（`>` 不是 `>>` 时算写操作）。

**Layer 3 — 交互确认**：`_CONFIRM_KEYWORDS` 包含 `rm`、`git commit`、`pip install`、`curl`、`sudo` 等。`ShellTool` 的构造参数 `confirm_callback` 决定了确认行为：
- `None`（默认）→ 跳过确认直接执行（run 模式）
- `terminal_confirm` → 终端 y/N 确认（chat 模式）
- `always_deny` → 直接拒绝（CI / pipe 模式）

此外，`sys.stdin.isatty()` 检测非交互式终端，直接拒绝危险命令，避免 CI 中意外执行。

**Layer 4 — timeout + 截断**：30s 默认超时，输出截断到 8K 字符（保留头 60% + 尾 40%）。

### 4.3 Docker 沙箱

除了 shell 层面的安全，还实现了 `DockerRuntime`：
- 基于 `python:3.11-slim` 镜像
- 仓库 bind-mount 到 `/workspace`
- 默认 `--network none` 网络隔离
- 懒启动（首次 exec 时才创建容器）
- 持久容器（多次 shell 调用复用同一个容器）

### 4.4 遇到的问题

**正则写重定向的精确匹配**：`grep ">" file.txt` 中的 `>` 是正则模式的一部分，不是写重定向。最初的正则 `'>' in cmd` 误拦截了很多正常的 grep 命令。

**解决**：使用零宽断言 `(?<![>])>(?![>])` 精确匹配——前面不是 `>` 且后面也不是 `>` 的单个 `>` 才是写重定向。

---

## 5. GitHub App Webhook 服务端 + CI 原生管线

### 5.1 整体架构

```
GitHub (Issues / PRs / CI Runs / Dependabot)
    │ webhook
    ▼
Flask Server (:8000)
    ├── HMAC SHA-256 签名校验
    ├── EventRouter: (event_type, action) → handler
    └── 后台线程池派发（不阻塞 webhook 响应）
         ├── Issue Handler → Triage + Dedup + AutoFix
         ├── PR Handler → Review + AutoMerge检 查
         ├── CI Failure Handler → 日志分析 + 修复PR
         ├── Security Handler → Dependabot 影响评估 + Bump PR
         └── Release Handler → 发版说明生成
```

### 5.2 关键设计决策

**Handler 不做重活**：handler 只负责参数提取 + 后台派发，agent 工作在后台线程执行。webhook 200 OK 在 3 秒内返回，GitHub 不会超时重试。

**后台线程池上限 5**：避免大量 webhook 同时到达时炸掉服务器资源。

**GitHub App JWT 认证**：`pipeline/auth.py` 用 RSA 私钥签发 JWT，换取 Installation Token（1 小时有效期），用 Token 调用 GitHub API。

### 5.3 17 种事件类型处理

以 Issue 处理的完整流水线为例：

```
Issue opened webhook
     │
     ▼
1. TriageClassifier (关键词启发式 + LLM)
   → label: bug/enhancement/docs/security
   → priority: p0-p3
   → effort: trivial/small/medium/large
     │
     ▼
2. DedupEngine (SequenceMatcher + GitHub API)
   → 相似度 > 0.90 → 自动评论 + 关闭
   → 0.75-0.90 → 评论"可能相关"但不关闭
     │
     ▼
3. Solvability Gate (LLM 可行性判断)
   → auto_fix → 启动 fix pipeline → 创建 PR
   → needs_triage → 评论分析 + 等人工
```

### 5.4 PR Review 增量审查

传统的 PR review 每次 `synchronize`（新 commit push）都重新 review 整个 PR，浪费大量 token。

我的设计：`ReviewMemory` 保存每次 review 的 `head_sha` 和 findings 快照。PR synchronize 时：
1. 对比 `last_review_sha` vs 当前 `head_sha`，只 review 增量 diff
2. 逐条检查上次 CRITICAL/HIGH findings 是否已修复
3. 提交新的 review，更新 ReviewMemory

### 5.5 遇到的工程问题

**system_prompt 参数错误**：review agent 和 fix agent 需要不同的 system prompt，但最初 `AgentConfig` 没有 `system_prompt` 字段，`pipeline/review.py` 直接传参会报 `TypeError: unexpected keyword argument 'system_prompt'`。

**解决**：采用"选择性覆盖"模式——在 `AgentConfig` 中增加 `system_prompt_template: str | None = None` 字段。所有不指定该字段的调用方保持默认行为不变，只有 review agent 通过传入自定义模板来 opt-in。这是最小改动量方案。

**Windows GBK 编码**：多个脚本在 Windows 上运行时报 `UnicodeEncodeError: 'gbk' codec can't encode character '≥'`。Windows CMD 默认代码页 936 无法编码 ≥、≤、→ 等 Unicode 符号。

**解决**：将所有打印输出中的非 ASCII 字符替换为 ASCII 等价物（`≥` → `>=`，`→` → `->`，`⚠` → `[WARN]`）。在跨平台输出中保持纯 ASCII 是最稳妥的做法。

---

## 6. 可观测性体系

### 6.1 为什么需要三层测量架构

设计文档定义了 10 个成功指标，但其中 4 个（PR Review Coverage、Review Finding Recall、Stale PR Reduction、Time to First Response）最初无法测量——它们需要**跨运行状态**或**配对数据**，而非单次运行的日志能覆盖的。

### 6.2 三层设计

```
Layer 1: Collection（收集层）
  server.py → TTRTracker.record_receipt()
  handlers.py → TTRTracker.record_response()
  handlers.py → FindingStore.record_agent()

Layer 2: Storage（存储层）
  仅追加 JSONL（匹配 agent/event_log.py 模式）
  ~/.repoforge/metrics/
    ├── agent_findings.jsonl
    ├── human_findings.jsonl
    ├── stale_scans.jsonl
    └── ttr_log.jsonl

Layer 3: Exposure（暴露层）
  Dashboard API 按需聚合计算
  GET /dashboard/api/metrics/*
```

**为什么选 JSONL 而非 SQLite**：
- 与 agent/event_log.py 存储模式一致，降低认知负担
- 仅追加写入无需迁移、无 schema 变更、无锁争用
- 预估写入频率 < 2000 条/月，JSONL 完全够用

### 6.3 Review Finding Recall 的匹配算法

这是最复杂的指标——需要同一 PR 同时有人类 review 和 agent review 的配对数据。

**复合打分**：
```python
score = 0.5 × Jaccard(message_tokens)    # 文本相似度
      + 0.3 × line_proximity_bonus        # 行号接近度（完全匹配 +0.3, ±3行 +0.2）
      + 0.2 × severity_agreement_bonus    # 严重度一致性
```

**为什么不用 embedding cosine similarity？**
- 零依赖（无 embedding API 调用、无向量数据库）
- 确定性（相同输入总是得到相同结果，可调试）
- 延迟 < 1ms（embedding 调用 200-500ms）
- 语义匹配可能过度泛化（不同 bug 但相似措辞会被误匹配）

**关键优化**：在 Jaccard 比较之前移除文件路径。`test_review_target.py:8` 这样的路径用于行号接近度评分，但它们会稀释 Jaccard token 集合——两个完全不相关的发现如果都引用了同一个文件的不同行，纯 Jaccard 会给出虚高的相似度。

**效果**：优化后 recall 从 25% 提升到 80%，F1 = 0.667。

### 6.4 TTR 的 Pending 字典设计

TTR 计算只需要 webhook 到达时间和首次响应时间。我用一个内存字典（key = `(repo, issue_or_pr_number)`）+ threading.Lock 做线程安全保护。99% 的 response 在 5 分钟内到达，24h TTL 清理线程 + 1000 条内存上限。进程重启时丢失的 pending 记录影响 < 0.1% 的样本，可以接受。

---

## 7. 技术选型决策速查

| 决策 | 选择 | 被拒绝的方案 | 核心理由 |
|------|------|-------------|---------|
| 代码图谱 | tree-sitter | LSP / 纯正则 | 无外部进程依赖，多语言统一接口 |
| 上下文压缩 | LLM 语义摘要 | 纯 token 裁剪 / embedding+RAG | 需要理解"当前进展"，而非语义检索 |
| 记忆存储 | JSON + 原子写入 | SQLite / PG | 读多写少，JSON 够用，零运维依赖 |
| 匹配算法 | 复合相似度 | Embedding cosine / LLM 判断 | 确定性、零延迟、零成本 |
| 严重度推断 | 49 关键词打分表 | LLM 分类 | 确定性、零成本，80% recall 证明足够 |
| 指标存储 | JSONL | SQLite | 与 event_log 一致，仅追加，无迁移 |
| TTR 追踪 | 内存字典 + TTL | 数据库 | 窗口极短（< 5min），内存占用可忽略 |
| Handler 执行 | 后台线程池 | 同步阻塞 | webhook 200 OK 须在 3s 内返回 |
| Shell 安全 | 四层递进 | 纯黑名单 / 纯白名单 | 黑名单可绕过，白名单限制太多 |

---

# 面试模拟：高频追问与回答

> 以下从面试官视角，针对简历描述中的 5 个技术亮点逐层深挖。回答以面试者口吻撰写，目标是展现"不是照着文档念，而是真正做过这些决策"的思考深度。

---

## 一、双模式引擎（ReAct + Pipeline）

### Q1: "你说单一 ReAct 循环有上下文膨胀问题——具体是什么现象？你怎么确认是这个原因而不是模型能力不够？"

**答：** 这个问题问得很好，我确实花了些时间做根因分析才确认是上下文问题而非模型问题。

具体现象是：在 benchmark 跑 astropy 的 issue 时，agent 前 8 步表现很好——正确找到了 `separable.py` 中的 bug，读懂了相关代码。但从第 12 步开始，它开始在已经读过的文件上反复调用 `file_read`，甚至在第 18 步时重新读了一个 step 3 已经确认过"不相关"的文件。到第 25 步超时的时候，它既没有完成修复也没有宣称放弃，就一直在"探索"。

我做了个对比实验来确认根因：同一个 issue，我手动把对话历史裁剪到最近 8 条消息（丢弃中间的探索噪音），agent 在 4 步内就完成了修复。这说明模型能力是够的，但上下文里塞了太多无用信息后，模型被带偏了。

另外我看了下 token 分布——到 step 20 时，agent 的 observation 输出（工具返回结果）占了总上下文的 ~65%，其中大部分是"这个文件不包含相关代码"这类早期探索的负结果。这些信息有过价值但已经过时了，继续留在上下文里就是噪音。

### Q2: "你的 Pipeline 把上下文核销了——但如果 Stage 0 漏掉了关键文件，后续阶段不就永远找不回来了？这个 trade-off 你怎么看？"

**答：** 对，这是 Pipeline 模式最大的风险——上游阶段的错误会级联到下游，且没有纠正机会。所以我在设计时做了三个防御：

第一，UNDERSTAND 阶段给了相对充裕的步数（8 步），并且我在该阶段的 system prompt 里明确要求"探索 5-7 步后就调用 finish"，避免 agent 在一个方向上钻太深而忽略其他候选项。

第二，也是最关键的——**Pipeline 失败时自动 fallback 到标准 ReAct**。`PipelineEngine.run()` 的最外层包了 try/except，任何阶段抛异常都会自动调用标准 `Agent.run()`。ReAct 虽然慢一些，但它不会漏文件。这是一个"安全网"设计。

第三，IMPLEMENT 阶段的工具白名单里仍然保留了 `file_read` 和 `file_view`，如果 agent 在编辑时发现 plan 里没覆盖的依赖文件，它可以自己读。这不是完美的纠正机制，但在大多数情况下够用。

这个 trade-off 的本质是"效率 vs 容错"。Pipeline 省 token（每个阶段上下文从零开始）、快、边界清晰，但确实牺牲了一部分容错能力。我的判断是：对 80% 的有明确步骤的 issue（比如"X 函数的 Y 参数类型不对"），Pipeline 是更优选择；对剩余的 20% 需要大范围探索的，fallback 到 ReAct。两套模式各有所长，比只押一种更稳健。

### Q3: "可行性门控具体怎么判断一个任务 INFEASIBLE？有没有误判过？"

**答：** 可行性门控是在 PLAN 阶段结束时，通过 LLM 自身的输出来判断的——不是额外的 LLM 调用。PLAN 的系统提示里就要求 agent 在 `risks` 字段中给出评估：

- `INFEASIBLE: <reason>` → 流水线终止
- `NARROW: <reason>` → 裁剪 scope
- 普通风险 → 正常继续

LLM 判断 INFEASIBLE 的典型场景包括：
- "需要第三方 API 密钥才能复现"
- "问题涉及编译型语言的链接错误，当前环境无法复现构建"
- "bug 表现形式是运行时崩溃，需要特定硬件环境"

误判确实发生过。一个 astropy issue 中，agent 在 PLAN 阶段因为找不到对应的测试文件就标记了 INFEASIBLE，但实际上测试是通过 pytest 的 conftest.py 中的 fixture 间接调用的，不需要单独测试文件。后来我在 prompt 里加了"如果找不到测试文件，检查是否存在 CI 配置（tox.ini、pyproject.toml）中的测试命令"，减少了这类误判。

我的设计理念是：**宁可误判中止，也不要 agent 在无法完成的任务上耗尽 40 步**。中止了还可以人工介入调整 prompt 重跑，但耗尽 40 步的 token 成本和时间成本是不可逆的。

### Q4: "Pipeline 每个阶段的最后一步为什么要 strip 掉工具列表？不怕模型崩溃吗？"

**答：** 这是一个反直觉的设计，但实践证明非常有效。

问题背景是：很多 LLM（尤其是 DeepSeek）在有工具可用时会一直调用工具，停不下来。即使 prompt 里明确说"你已经收集了足够信息，请调用 finish"，它还是会继续调 `file_view`。它们被训练成"有工具就用"的行为模式。

Strip 掉工具后，模型没有别的选择，只能输出文本——而我在这个时刻注入了强提示：

```
## FINAL STEP — NO TOOLS AVAILABLE
You have ZERO tools. You CANNOT call any functions.
Your ONLY option is to output raw text.
CRITICAL — output ONLY a JSON object, nothing else.
```

然后列举了它最常犯的错误（裸函数调用、XML 标签、markdown 代码块），告诉他"这些都是 REJECTED"。从实际效果看，这套组合拳把 JSON 首次解析成功率从大约 60% 提升到了 90%+。

不过确实有些模型在被剥夺工具后会"崩溃"——输出空字符串或者拒绝输出——这种情况就走 repair prompt 重试，或者靠最外层的 try/except fallback 兜底。

---

## 二、LLM 后端抽象与双通道兼容

### Q5: "为什么不直接用 LangChain？你有 5 个 provider 要维护，LangChain 不是已经做了这件事吗？"

**答：** 我在项目初期评估过 LangChain，但最终决定自己写抽象层，三个原因：

第一，**LangChain 的抽象层太厚了**。为了实现一个简单的 tool calling，它需要经过 BaseTool → StructuredTool → RunnableSequence 等多层包装。我只想要一个 `complete(messages, tools) → Response` 的接口，自己写比配置 LangChain 更快，而且 debug 时我可以直接看到每一层的行为。

第二，**Anthropic 的 API 格式有特殊性**。Anthropic 的 tool_use 是 content block 类型，tool_result 需要配对 tool_use_id，而且 system prompt 是独立参数不是 message。这些细节 LangChain 虽然能处理，但出错时排查非常痛苦——你得到的是一个 LangChain 封装的异常，而不是 Anthropic 的原始错误信息。

第三，**我对这个项目的一个原则是"零框架依赖"**。Agent 的核心逻辑（ReAct 循环、上下文管理、工具执行）是业务逻辑，不是框架胶水代码。依赖框架意味着框架升级可能 break 我的代码，框架的 bug 变成我的 bug。而 LLM API 调用这部分，本质上就是 HTTP 请求 + 格式转换，200 行代码就能覆盖 5 个 provider，不值得引入一个框架。

当然，代价是我需要自己处理各家 API 的格式差异。但这是一次性的投入，而且我对每一行代码都有完全的控制权。

### Q6: "DeepSeek R1 不支持 function calling，你的文本解析通道怎么保证解析准确率？"

**答：** 文本解析通道的核心挑战是：LLM 输出的格式不可控。同一个模型，有时输出 `Action: shell\nParams: {"cmd": "ls"}`，有时输出 ````json\n{"tool": "shell", "params": {"cmd": "ls"}}\n````，有时直接在思考文字里夹杂"我认为应该执行 ls"然后什么都没有。

我的解法是**多策略尝试 + 严格兜底**：

1. 优先尝试 JSON 解析——用与结构化输出相同的 `extract_json()` 管道（````json``` fences → raw braces → 文本清理），从解析出的 JSON 中提取 `tool_call` 字段
2. JSON 解析失败时，用正则匹配 `Action: xxx\nParams: {...}` 格式——这是 prompt 里明确要求的格式，大多数情况下模型会遵守
3. 搜索 `TASK_COMPLETE:` 和 `GIVE_UP:` 关键词——在没有工具调用的终止场景下
4. 以上全部失败 → 返回 `ActionType.REFLECTION`，把原始文本作为 thought 注入对话历史，让 agent 在下一轮重新尝试

关于准确率，我没有做大规模的量化测试，但在跑 benchmark 时 DeepSeek 路径的解析成功率大约是 85-90%。剩下的 10-15% 走 reflection 兜底，虽然会多消耗一步，但不会导致整个任务失败。这和函数调用的 99%+ 准确率确实有差距，但这是模型能力的天花板，不是解析逻辑的问题。

### Q7: "Anthropic 和其他 provider 的 API 差异这么大，你是怎么在代码层面处理这个差异的？"

**答：** 核心设计是：**所有差异全部封闭在 Backend 内部，Agent Core 永远只看到统一的 `LLMMessage → LLMResponse` 接口**。

差异主要在三个层面：

**消息格式**：Anthropic 的 system prompt 是 API 参数 `system="..."` 而非 message 数组中的 `role: "system"` 条目。`AnthropicBackend.complete()` 在构造请求时，从 messages 列表中提取第一条约 role=="system" 的消息，单独作为 `system` 参数传递，其余消息转换为 Anthropic 的 `{"role": "user"}, {"role": "assistant"}` 交替格式。

**Tool 格式**：Anthropic 的 tool 定义使用 `input_schema` 字段，而 OpenAI 使用 `function.parameters`。这个在 `to_llm_schema()` 时就统一成了中间格式 `LLMToolSchema`，各 Backend 各自转换为自家格式。

**响应解析**：Anthropic 的响应中，tool_use 是一个 content block（`{"type": "tool_use", "name": "...", "input": {...}}`），而 OpenAI 的 tool_calls 是 message 对象的一个顶层字段。`AnthropicBackend` 遍历 content blocks 提取 tool_use，转换为统一的 `Action(ToolCall)`；`OpenAICompatBackend` 从 `message.tool_calls` 提取。

Agent Core 拿到 `LLMResponse.action` 时，已经完全不知道底层是哪家的 API。这就是抽象层的价值。

---

## 三、代码理解与记忆系统

### Q8: "你的 tree-sitter repo-map 和直接让 LLM 读目录结构有什么本质区别？LLM 不是也能理解代码吗？"

**答：** 区别在于**信息密度**。假设一个中等大小的 Python 项目有 200 个文件、500 个函数——如果把目录结构原样塞进 prompt，LLM 看到的只是文件名列表，它不知道 `foo()` 函数在 `src/utils/helpers.py` 里还是在 `src/core/engine.py` 里。它需要花费额外的 tool call 步数去 grep、去读文件头。

tree-sitter 提取的是**符号级别的摘要**：每个文件的顶层函数/类定义及其签名。比如：

```
src/core/engine.py:
  class Agent:
    def run(task, log) -> RunResult
    def _build_messages(history, budget, repo_map) -> list[LLMMessage]
    def _call_with_retry(messages, tools)
```

这段摘要大约 200 tokens，但包含了 3 步 tool call 才能收集到的信息。LLM 拿到这个摘要后，可以直接判断"Agent.run() 是入口函数，_build_messages() 是上下文组装逻辑"，然后用 `file_read` 精确定位需要修改的函数，而不是先 `file_read` 整个文件再理解结构。

所以本质区别是：**目录结构给 LLM 的是"地图"，tree-sitter 给的是"地图 + 路标"**。

### Q：什么是tree-sitter

 tree-sitter 是一个开源的多语言解析库，它能把源码解析成
  AST（抽象语法树）。和正则不同，它真正理解语言的语法结构——知道哪些是函数定义、哪些是类、哪些是注释。

  在 context/repo_map.py 里，我把它用来生成"代码图谱"。具体做法是：遍历仓库里所有源码文件，对 .py 调
  tree-sitter-python、对 .go 调 tree-sitter-go、对 .ts 调 tree-sitter-typescript——运行时按需 import
  对应的语言包，没安装的自动降级为正则 fallback。解析出每个文件的顶层函数和类定义后，按照"顶层定义 >
  嵌套方法"的规则排序，再按 token 预算截断，最终产出一段约 2000 token 的自然语言摘要，塞进 system prompt。

  这个摘要让 LLM 在动手之前就知道整个仓库的结构——比如 src/core/engine.py 里有个 class Agent，它的 run()
  方法是入口，_build_messages() 负责上下文组装——不用花 3-5 步去 find_files + file_read
  慢慢探索。本质上是在信息密度上做了文章：同样 2000 token，目录结构只告诉 LLM"有哪些文件"，tree-sitter
  告诉它"每个文件里有什么符号、这些符号之间是什么关系"。

### Q9: "语义上下文压缩说可以保留 95% 信息——这个数字是怎么算出来的？"

**答：** 坦诚说，95% 是一个基于经验的估计值，不是我做了严格对照实验得出的。但我可以解释这个估计的逻辑：

压缩器的做法是——让 LLM 对旧消息做结构化摘要，保留 6 个维度的信息：
- summary：1-2 句概述
- files_examined：读了哪些文件
- files_modified：改了哪些文件、怎么改的
- test_results：测试结果
- key_findings：关键发现（bug 根因、错误信息等）
- current_state：结束时 agent 在做什么

这 6 个维度覆盖了 agent 继续工作所需的全部上下文。原始消息中被丢弃的主要是：
- 工具调用的完整参数（大部分不需要记住）
- observation 的完整原始输出（只需要关键行）
- agent 的探索性 thinking（过程不需要，结果需要）

这 3 类信息占原消息的 ~90% token 量，但对后续决策的贡献不到 5%。反过来，那 6 个维度的摘要占原消息 ~10% token 量，但贡献了 95% 以上的决策价值。

如果你在面试中问我这个数字的严谨性——我承认这是估算。真正严谨的做法应该是做消融实验：对比"有压缩 vs 无压缩"在 benchmark 上的 resolved rate，看有多少退化。但受限于 API 成本，我没有做这个实验。

### Q10: "Repo Memory 的权重（published × 14, validated × 10...）是怎么定的？拍脑袋还是有依据？"

**答：** 这些具体数字是我参考了 OpenMeta CLI 的实现后定的，他们用了类似的权重体系。但背后的设计逻辑是这样的：

权重反映了每个信号对"这个文件值得 agent 关注"的**置信度**：

- `candidate_count`（被标记为候选）= 1×：最低置信度——agent 可能在探索阶段标记了 20 个文件，但实际只有 2 个需要改。这是"可能性"信号。
- `changed_count`（实际被编辑）= 6×：中等置信度——agent 编辑了这个文件，可能编辑是对的，也可能是错的。这是"相关性"信号。
- `validated_count`（编辑后测试通过）= 10×：高置信度——编辑 + 测试通过，说明修改方向正确。这是"正确性"信号。
- `published_count`（最终合并为 PR）= 14×：最高置信度——从编辑到最终合并经过了完整流程，这是 ground truth。这是"最终结果"信号。

14:10:6:1 的比例大致是 14:10:6:1，意味着：
- 1 次 published 的价值 ≈ 2.3 次 validated（14/6，因为 validated 也需要 changed 作为前提）
- 1 次 validated 的价值 ≈ 1.7 次单纯 changed
- 1 次 changed 的价值 ≈ 6 次 candidate

这个比例不是精确优化的结果，但我验证过它不会产生荒谬的排序——比如一个被标记了 15 次候选但从未被编辑的文件，权重是 15，低于一个被编辑 3 次且 1 次验证通过的文件（3×6 + 1×10 = 28）。

如果让我重新设计，我可能会引入时间衰减因子——最近 30 天内的修改权重高，超过 90 天的权重低。因为代码库是动态变化的，去年的 hot path 今年可能已经被重构掉了。

### Q11: "Task-aware 的相似问题匹配为什么用 difflib.SequenceMatcher 而不是 embedding？"

**答：** 三个原因：

第一，**数据量太小，不需要向量检索**。recent_issues 最多只有 10 条，全部做相似度计算只需要 10 次 SequenceMatcher 调用，总耗时 < 1ms。引入 embedding 意味着需要 embedding API 调用（200-500ms 延迟 + 费用），得不偿失。

第二，**确定性很重要**。SequenceMatcher 对相同的输入永远返回相同的结果，这意味着匹配行为是可调试、可预期的。如果用 embedding + cosine similarity，同一个 issue 在两次运行中可能因为 embedding API 的微小波动而匹配到不同的历史记录，导致 agent 行为不可复现。

第三，**文本长度短**。issue 标题通常 5-15 个词，在这个长度范围内，基于字符序列的算法（SequenceMatcher 是 Ratcliff/Obershelp 算法）和语义 embedding 的区分能力差异不大。embedding 的优势在长文本（paragraph 级别），在短文本上是 overkill。

---

## 四、安全壳与沙箱

### Q12: "四层安全壳真的安全吗？如果我是攻击者，怎么绕过它？"

**答：** 我不说它绝对安全——没有绝对安全的系统。但我可以分析每一层的已知绕过路径和我的防御：

**Layer 1（黑名单）的绕过**：通过编码/别名，比如 `$(echo rm) -rf /`。这种情况黑名单检测不到 `rm -rf /` 字面量，但会掉到 Layer 2——它不是白名单命令，会进入 Layer 3 的交互确认。

**Layer 2（白名单）的绕过**：白名单里允许 `python -c` 和 `echo`，攻击者可以构造 `python -c "import os; os.system('rm -rf /')"` 或 `echo "dangerous" > /etc/critical`。第一个会触发 Layer 3（`python -c` 在 _READONLY_PREFIXES 里，免确认直接执行——**这确实是一个漏洞**）。第二个中的 `> ` 写重定向会触发 `_is_readonly()` 返回 False，进入 Layer 3。

**Layer 3（交互确认）的绕过**：在 run 模式下 `confirm_callback=None`，跳过了确认。但 run 模式下 agent 跑的是受信任的本地任务——攻击者不是 agent，而是 agent 要处理的代码仓库里的恶意文件（比如 `setup.py` 里有 `os.system("rm -rf /")`）。这个场景下真正的防线是 Docker 沙箱的网络隔离 + 文件系统隔离（bind-mount 只读挂载）。

**Docker 沙箱的绕过**：容器默认 `--network none`，无法下载 payload。bind-mount 的仓库目录是读写的（agent 需要改代码），但即使 agent 删掉了仓库，也只是删掉了挂载点里的文件，不影响宿主机。

**最弱的环节**，坦诚说是 `python -c` 在白名单里。agent 可以通过 `python -c` 执行任意 Python 代码。我考虑过限制 `python -c` 的输出长度或内容模式，但那样会阻止合法的测试脚本执行。最终方案是依赖 Docker 沙箱 + 非 root 用户做权限限制。这是一个有意识的风险接受。

### Q13: "Docker 沙箱是懒启动的——为什么不用每个 shell 调用后销毁重建？那不是更安全吗？"

**答：** 是更安全，但代价是**性能**。

agent 在一次任务中平均调用 shell 工具 5-15 次。如果每次调用都销毁重建容器，假设容器启动需要 3-5 秒（拉镜像、初始化、mount），15 次 shell 调用的总等待时间就是 45-75 秒——这对于一个 40 步的任务来说是不小的开销。

更重要的是，有些操作需要跨 shell 调用保持状态：比如先用 `pip install pytest` 安装测试依赖，再跑 `pytest`。如果每次都销毁重建，安装的包就丢了，agent 会陷入"安装-测试失败（无包）-安装"的循环。

当然，如果安全要求很高——比如跑不受信任的第三方仓库——销毁重建确实更安全。这个可以通过配置切换：`DockerRuntime(ephemeral=True)` 来启用每次调用后清理容器。但目前默认行为是持久容器，这是"安全 vs 效率"的 trade-off，我偏向效率。

---

## 五、CI 原生管线与 Webhook 服务

### Q14: "13种事件类型——你怎么测试这些 handler 不会互相干扰？"

**答：** 这是 Pipeline 模块测试最棘手的部分。不是 handler 逻辑本身复杂，而是**依赖外部状态太多**——GitHub API、agent 运行、webhook 签名校验——每个都是测试的隔离难点。

我的测试策略分三层：

**第一层：单元测试 handler 逻辑**。把 handler 拆成纯函数——输入是 parsed payload dict，输出是决定要做什么的"action list"，不实际调用 GitHub API。这样可以 mock payload 覆盖各种边界情况。

**第二层：集成测试 EventRouter**。用 `EventRoute` 注册表做路由测试——给定 `(event_type, action)` tuple，断言解析到正确的 handler。这个测试是纯逻辑的，不涉及网络。

**第三层：端到端 webhook 模拟**。`pipeline/cli.py` 里有一个 `test-issue` 命令，可以手动触发单个 handler 流程。虽然不是真正的 GitHub webhook，但能验证从 payload 解析到 agent 执行到 comment 创建的全链路。

没做到的是：我无法在本地跑一个完整的"模拟 10 个 webhook 同时到达"的压力测试。这需要真实的 GitHub App 环境和多个并发 webhook——目前的测试覆盖主要停留在功能和逻辑层面，并发测试是一个已知的盲区。如果上线生产我会优先补这个。

> Issue 相关（4 条）：issues.opened 走分诊+自动修复，进来先分类打标签、去重检测、判断能不能自动修，能修就启动 agent 创建
>    PR。issues.labeled 监听有没有人手动打 agent-fix 标签——打了就触发修复。issues.closed 更新 Repo Memory
>   把对应记录标为已关闭。issue_comment.created 监听 /agent-fix 和 /agent-review 两个 slash
>   command，用户可以在评论区手动召唤 agent。
>
>   PR 审查（4 条）：pull_request.opened 做全量 diff review，输出四级严重度的 ReviewReport，通过 GitHub PR Review API 提交
>    inline comment。pull_request.synchronize 在 PR 作者 push 新 commit 时触发增量审查——只审新增的 diff，同时检查上次提的
>   CRITICAL/HIGH 问题修复了没有。pull_request.closed 在 PR 被合并后更新 Repo Memory 的 path signal，把
>   published_count++，同时把 issue outcome 标为 merged。pull_request.labeled 检查打没打 auto-merge
>   标签——打了就做五重门控判断（CI 全绿、有人 approve、无 changes requested、无 CRITICAL finding、diff 行数不过 500）。
>
>   Review 响应（1 条）：pull_request_review.submitted 监听 maintainer 提交的 review——如果是 REQUEST_CHANGES，agent 解读
>   review comments 自动 push fix commit。这个风险标为 HIGH。
>
>   CI 失败（1 条）：check_run 的 catch-all，handler 内部过滤失败事件——拿 CI 日志分析根因，能修就 push fix
>   commit。风险也是 HIGH，默认只做分析不修改。
>
>   发版管理（1 条）：push 的 catch-all，handler 内部过滤 tag push——从 merged PRs 列表生成结构化的 release notes，按
>   breaking changes / 新功能 / bug 修复 / 文档分类，通过 GitHub API 发布 Release。
>
>   安全漏洞（1 条）：dependabot_alert.created 收到 Dependabot
>   告警后，评估漏洞影响面——检查仓库锁文件是否用了受影响版本的包。critical/high 级别自动创建版本 bump PR 并跑测试。
>
>   App 生命周期（1 条）：installation.created 在 GitHub App 被安装到新仓库时触发，发欢迎消息、注册仓库到监控列表。

### Q15: "后台线程处理 + 3 秒返回 200——如果线程里的 agent 执行超过 30 分钟怎么办？用户怎么知道状态？"

**答：** 这是一个很好的问题，涉及到异步任务的可观测性。

当前的设计中，agent 在后台线程执行，结果通过两种方式反馈给用户：

1. **GitHub Comment**：agent 完成后，通过 GitHub API 在对应 issue/PR 上创建评论，告知用户"修复完成，见 PR #42"或"无法修复，原因：xxx"。这是主要的用户通知渠道。

2. **Dashboard**：后台线程在 `EventLog` 中持续记录每个步骤，Dashboard 的 API 可以从 JSONL 日志中读取实时状态。

但没有做到的是：**中途没有进度通知**。如果 agent 跑了 25 分钟，用户在 GitHub 上看不到任何更新，不知道 agent 在"正常工作中"还是"卡死了"。这是一个 UX 缺口。

如果让我改进，我会：
- 在 handler 收到 webhook 后立即创建一条 issue comment："Repoforge agent 已开始分析此 issue..."（这个已经做了，在 TTR 统计中可以看到）
- 每 5 分钟或每 5 个 step 触发一次进度更新 comment
- 设置一个全局 timeout（比如 30 分钟），超时后自动 create comment 说明超时

核心设计原则是：**用户不应该等待 agent，用户应该被告知 agent 在做什么**。

### Q16: "为什么用 Flask 而不是 FastAPI？FastAPI 对异步和 webhook 场景的支持更好吧？"

**答：** 选 Flask 的原因比较务实——**最小依赖原则**。这个项目已经有很多依赖了（tree-sitter、anthropic SDK、openai SDK、PyGithub 等），我不想再加 asyncio + uvicorn + pydantic 这一整套 FastAPI 生态。

具体来说：
- 这个 webhook 服务器的 QPS 极低（一个仓库一天可能只有几十个 webhook），Flask 的性能完全足够。FastAPI 的异步优势在高并发场景下才有意义。
- Handler 的"重活"已经在后台线程池执行了，webhook 响应是即时返回 200，不需要 async handler。
- Flask 的生态更成熟，出问题时 Google 到的解决方案更多。

如果将来需要支持高并发（比如 GitHub App 被安装到 1000 个仓库），我会考虑迁移到 FastAPI + 消息队列（Redis/Celery）。但现阶段 Flask 是最合适的。

---

## 六、系统设计与综合

### Q17: "这个系统从 0 到 1 花了多久？如果让你重新来做，你会怎么排优先级？"

**答：** 开发周期大约 4-6 周（业余时间），按这个顺序：

Week 1-2：Agent Core（ReAct 循环、LLM 抽象、工具系统、文件/搜索工具）
Week 2-3：Shell 安全 + Docker 沙箱、Entry CLI
Week 3-4：Pipeline 引擎 + 结构化输出
Week 4-5：Repo Memory、语义压缩、Pipeline 服务端
Week 5-6：Benchmark 集成、Dashboard、可观测性

如果重新来，我会调整优先级：

**先做 benchmark，再做功能**。我在 Week 4 才接 SWE-bench，导致前面两周的很多设计决策缺乏数据支撑。比如 Pipeline 的 token budget（UNDERSTAND 6K、IMPLEMENT 15K）是凭经验设的，没有 benchmark 数据来校准。如果先在 SWE-bench 上跑 10 个实例收集 baseline 数据，我再设计 Pipeline 的 budget 分配会精准得多。

**先做可观测性，再优化**。我的 EventLog 从一开始就设计了，但 Dashboard 和 Metrics 是最后两周才加的。结果是：前期调试 agent 行为时，我只能读 JSONL 文件手动 grep，效率很低。如果一开始就有 Dashboard，排查 agent 为什么在第 N 步做了某件事会快得多。

**Repo Memory 应该更早做**。Memory 的核心价值是"每次运行都建立在之前的经验上"。但我在 Week 4 才加 Memory，导致前三周每次 bench run 都是冷启动，浪费了很多本可以积累的信号。

### Q18: "你这个系统最大的技术债是什么？"

**答：** 坦诚说有三个：

第一，**文本解析通道的健壮性**。DeepSeek R1 的文本输出格式非常不稳定，偶尔会输出完全无法解析的内容。目前的 fallback 是降级为 REFLECTION，但更好的做法是针对已知的"坏格式"模式不断扩充解析规则。这本质上是维护一个"bad output pattern 数据库"，很痛苦但必要。

第二，**Pipeline 与 ReAct 的 fallback 没有做信息传递**。Pipeline 失败 fallback 到 ReAct 时，ReAct 是冷启动的——它不知道 Pipeline 前几个阶段已经读了哪些文件、发现了什么线索。等于 Pipeline 的工作完全浪费了。理想情况下，fallback 应该把 Pipeline 阶段的结构化输出作为 ReAct 的初始上下文注入。

第三，**测试覆盖率不均衡**。Agent Core 和 Tool 系统的测试相对充分（376 个测试用例），但 Pipeline 服务端（webhook handler）的测试严重不足——很多 handler 的逻辑只有在真实 GitHub webhook 场景下才能验证。Mock GitHub API 是一个已知的技术债，但工程量不小。

### Q19: "如果这个系统要服务 100 个仓库，你觉得最大的瓶颈会是什么？"

**答：** 最大的瓶颈不是技术性能，而是**多仓库之间的 agent 配置差异**。

每个仓库有不同的技术栈、测试框架、代码风格、PR 规范。当前系统的 agent prompt 和工具配置是全局的，没法按仓库定制。比如：
- 仓库 A 用 pytest + coverage，agent 需要跑 `pytest --cov`
- 仓库 B 用 tox + flake8，agent 需要跑 `tox -e py311`
- 仓库 C 是 monorepo，agent 需要知道只改 `packages/foo/` 下面的文件

Repo Memory 现在只记录了"哪些文件常被改"和"哪些测试命令存在"，但没有记录"这个仓库偏好的测试执行方式"和"这个仓库的 PR 模板"。如果要支撑 100 个仓库，需要让 Memory 变成"按仓库定制的 agent 配置"——每个仓库有自己的 system prompt 模板、工具白名单、测试命令模板。

第二个瓶颈是**并发 agent 执行的 GPU/API 资源**。100 个仓库如果同时触发 webhook，后台线程池上限只有 5，会导致 95 个任务排队。解决方案不是扩大线程池，而是引入消息队列（比如 Redis + Celery）做异步任务调度，配合优先级队列（security issue > bug > enhancement）。

### Q20: "你从 OpenMeta CLI 的源码分析中学到了什么，直接应用到了自己的设计里？"

**答：** 最大的收获是一条核心洞察：**编排比模型更重要**。

OpenMeta CLI 默认用 `gpt-4o-mini`——一个轻量、便宜、能力远不如 Claude Sonnet 的模型——但能取得可用效果。我去读了它的源码，发现核心原因是：

1. **分治**：把一个复杂 issue 拆成 7 个有界步骤（Scout → Select → Prepare → Draft → Code Change → Validate → PR Draft），每个步骤的 prompt 高度专业化，输出结构化和可验证。

2. **积累**：Repo Memory 让每次运行都建立在之前运行的基础上。文件热点路径、测试命令、验证失败模式都被持久化。

这两个设计决策直接影响了我的 Pipeline 四阶段设计和 Repo Memory 三层信号模型。

OpenMeta 还让我意识到：**prompt 中注入自然语言摘要比注入 JSON 更高效**。他们把 Repo Memory 渲染成 500-800 tokens 的自然语言文本，LLM 解析自然语言的速度和准确率都高于解析结构化 JSON。我在 `render_for_prompt()` 中也采用了同样的策略——输出"Preferred paths: astropy/modeling/separable.py (changed: 6, validated: 5, published: 3)"而不是一个 JSON blob。

### Q21: "SWE-bench 的 resolved rate 大概是多少？如果很低，你怎么跟面试官解释？"

**答：** 如实在 benchmark 结果中，resolved rate 不理想。但我认为这不丢人，原因有三：

第一，SWE-bench 是现在 agent 领域最难的公开 benchmark 之一。2024 年 GPT-4 + SWE-agent 的 resolved rate 大约是 12-18%。Devin（估值 20 亿美金的独角兽公司）在 SWE-bench-Lite 上的 reported rate 是 13.86%。这个赛道的上限本身就不高。

第二，benchmark 不是我的核心贡献。我的核心竞争力是**系统架构和工程实现**——设计了一个从 LLM 调用到 CI 部署的完整 agent 系统，解决了上下文管理、安全执行、跨运行记忆、可观测性等工程问题。提升 SWE-bench score 需要的更多是 prompt 工程和模型选择，这些是优化问题而非架构问题。

第三，benchmark 暴露了真实问题。比如 astropy-14365 这个 instance，agent 找到了 bug 根因（正则大小写不敏感），但修改时同时也删除了 QDP 类——这是一个"编辑精确度"问题。这类问题告诉我，IMPLEMENT 阶段需要在编辑后做一次"diff 审查"确认没有意外改动。Benchmark 的价值不是分数本身，是指出系统短板。

### Q22: "最后一个问题——你是怎么测试 agent 的？agent 测试和普通软件测试有什么不同？"

**答：** Agent 测试和普通软件测试有本质区别。普通软件测试是"给定输入，断言输出"——确定性、可重复。Agent 的行为是不确定的——同一个 issue、同一个 prompt，连续跑两次可能因为 LLM 的随机性而产生不同的行动序列。

我把 agent 测试分成了四个层次：

**Level 1 — 纯逻辑模块测试**：TokenBudget 的 trim 逻辑、ToolRegistry 的 schema 生成、JSON 解析和清洗。这些是纯函数，用标准 unittest 覆盖。这部分占了 376 个用例的大部分。

**Level 2 — MockBackend 确定性测试**：用 `MockBackend` 按脚本返回预设 Action，验证 agent 的**控制流**——比如 Agent 在连续 3 次相同 tool call 后是否触发死循环检测，Pipeline 在 INFEASIBLE 风险后是否中止。MockBackend 让 agent 行为完全确定，可以精确断言每一步的状态。

**Level 3 — 集成测试**：Agent + MockBackend + 真实 Tool——用一个测试 repo（包含已知 bug），MockBackend 返回合理的 tool call 序列，验证整个 run 流程能正常结束。这个层次验证了模块之间的接口正确性。

**Level 4 — 真实 LLM 测试**：连接真实 LLM API 跑端到端测试。这层的核心不是"断言结果正确"，而是**"agent 没有做危险/愚蠢的事"**——没有 rm 关键文件、没有无限循环、没有调用不存在的工具。这层的测试不频繁跑（API 成本），但在每次发版前必跑。

我没有做到的是：**Golden test**——用真实 LLM 跑一组已知答案的 issue，断言所有 case 都被正确修复。这在 SWE-bench 上理论可行，但一次完整 benchmark run（300 个 instance）的 API 成本太高，我只能跑 5 个做抽样验证。
