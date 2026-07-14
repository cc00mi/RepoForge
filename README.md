# RepoForge

自主编程智能体。给它一个任务描述，它会自己探索代码库、修改文件、运行测试，直到完成。

支持 **Claude、DeepSeek、OpenAI、Groq、Ollama** 多种模型，内置流式输出、Docker 沙箱、GitHub Issue 自动修复、四阶段管线引擎和持久化仓库记忆。

---

## 快速开始

```bash
# 安装
git clone https://github.com/cc00mi/RepoForge.git && cd RepoForge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 配置（编辑 config/default.yaml，填入 provider 和 api_key）
export DEEPSEEK_API_KEY=sk-xxx   # 或 ANTHROPIC_API_KEY / OPENAI_API_KEY

# 验证
python smoke_test.py

# 使用
cd your-project
repoforge chat
```

---

## 使用方式

### chat 模式（推荐）

持续对话，每轮历史保留，最接近 Claude Code 的体验：

```bash
repoforge chat                            # 当前目录
repoforge chat --repo /path/to/project   # 指定目录
repoforge chat --model deepseek-v4-pro   # 切换模型
repoforge chat --sandbox                  # Docker 沙箱
```

对话内命令：`/exit` 退出、`/stats` 查看统计、`/clear` 清空历史、`/help` 帮助

### run 模式

一次性任务，适合明确的批处理场景：

```bash
repoforge run --task "修复所有 failing 的测试"
repoforge run --task-file task.txt           # 从文件读任务
repoforge run --task "..." --confirm         # 危险命令需确认
repoforge run --task "..." --sandbox         # Docker 沙箱
```

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
    --repo owner/repo --issue 42 --local-path /tmp/myrepo
```

自动拉取 Issue → 运行 agent → 提交 PR。

---

## 配置

编辑 `config/default.yaml`：

```yaml
llm:
  provider: deepseek                      # anthropic | openai | deepseek | groq | ollama
  model: deepseek-v4-flash
  api_key: ${DEEPSEEK_API_KEY}            # 从环境变量读取
  base_url: https://api.deepseek.com      # OpenAI-compatible 时填写，anthropic 留空

agent:
  max_steps: 40           # 每轮最大步数
  budget_tokens: 80000    # token 预算

context:
  repo_map_budget: 8000   # repo-map 注入量
  history_window: 20      # 保留历史轮数
```

---

## 项目结构

```
repoforge/
├── agent/              # 核心：ReAct 主循环、四阶段管线引擎、事件日志、数据结构
│   ├── core.py         # Agent 类，驱动 ReAct 运行循环（Reflection / 死循环检测）
│   ├── pipeline.py     # 四阶段管线引擎（UNDERSTAND→PLAN→IMPLEMENT→VERIFY）
│   ├── task.py         # Task / Action / Observation / RunResult 数据类
│   ├── event_log.py    # JSONL append-only 事件流，支持回放
│   ├── prompt.py       # System prompt 模板
│   ├── checkpoint.py   # Agent 运行状态检查点（支持中断恢复）
│   ├── structured_output.py  # 结构化 JSON 提取与幻觉清洗
│   └── sub_agent.py    # 子 Agent 委托
│
├── llm/                # LLM 后端
│   ├── base.py         # LLMBackend 抽象基类 + MockBackend + 流式 mixin
│   ├── anthropic_backend.py   # Claude 原生（tool_use + 流式）
│   ├── openai_compat.py       # OpenAI / DeepSeek / Groq / Ollama
│   └── router.py       # 按配置选择 backend，多 provider 统一路由
│
├── tools/              # 工具层（agent 可调用的操作）
│   ├── base.py         # BaseTool + ToolRegistry
│   ├── file_tool.py    # 文件读写查看
│   ├── shell_tool.py   # Shell 执行（四层安全防护）
│   ├── search_tool.py  # 文本搜索 / 文件查找 / 符号定位
│   ├── test_tool.py    # pytest 执行 + 结构化结果解析
│   ├── git_tool.py     # git status / diff / add / commit
│   └── runtime.py      # LocalRuntime / DockerRuntime
│
├── context/            # 上下文管理
│   ├── repo_map.py     # tree-sitter 多语言符号提取 + 重要性评分
│   ├── compressor.py   # 语义压缩（N 条历史 → 1 条结构化摘要）
│   ├── token_budget.py # Token 预算分配与裁剪
│   └── history.py      # 对话历史滑动窗口
│
├── memory/             # 持久化仓库记忆
│   └── repo_memory.py  # 三层信号模型（路径/验证/Issue）+ 加权评分
│
├── pipeline/           # GitHub App 管线（Webhook → 事件分发 → 后台处理）
│   ├── server.py       # Flask Webhook 服务器
│   ├── event_registry.py  # 13 种事件类型路由分发
│   ├── handlers.py     # Issue/PR/Comment 事件处理
│   ├── triage.py       # Issue 自动分类
│   ├── scout.py        # 代码库探索前置
│   ├── review.py       # PR 自动审查 + 历史发现召回
│   ├── review_memory.py # PR 审查发现持久化记忆
│   ├── auto_merge.py   # 自动合并
│   ├── security.py     # 安全检查
│   ├── stale_manager.py # Issue/PR 过期管理
│   ├── metrics.py      # 三层指标收集（采集→JSONL→Dashboard）
│   ├── dashboard.py    # 指标仪表盘
│   ├── release_notes.py # 发布说明自动生成
│   ├── scheduler.py    # 定时任务
│   ├── welcome.py      # 新 Issue/PR 欢迎消息
│   └── cli.py          # 管线 CLI 入口
│
├── benchmark/          # 评测
│   ├── cli.py          # 评测 CLI
│   ├── swe_bench.py    # SWE-bench 任务适配
│   ├── measure_metrics.py  # 指标测量
│   └── offline_metrics.py  # 离线指标分析
│
├── docs/               # 设计文档与面试准备资料
│
├── entry/              # CLI 入口层
│   ├── cli.py          # Click CLI（chat / run / log 子命令）
│   ├── chat.py         # ChatSession，跨轮持久化历史
│   └── github_issue.py # GitHub Issue → PR 自动化
│
├── config/
│   ├── default.yaml    # 默认配置
│   └── schema.py       # 配置加载与校验
│
├── tests/              # 383 个测试，覆盖所有核心模块
├── smoke_test.py       # 端到端联通验证
└── USAGE.md            # 完整使用教程
```

---

## 核心特性

**多模型支持**
- Anthropic Claude（原生 tool_use）
- OpenAI、DeepSeek、Groq、Ollama（OpenAI-compatible）
- DeepSeek R1 等不支持 function calling 的模型走文本解析 fallback
- 配置文件一行切换，或 `--model` 参数临时覆盖

**四阶段管线引擎**
ReAct 循环之外提供结构化任务分解：UNDERSTAND（6K tokens）→ PLAN（4K tokens）→ IMPLEMENT（15K tokens）→ VERIFY（3K tokens），每阶段独立上下文窗口、工具白名单和可行性门禁。

**多语言 Repo-map**
用 tree-sitter 精确提取符号（函数、类、方法），生成 repo 摘要注入 system prompt，支持 Python / JavaScript / TypeScript / Go / Rust / Java / C++ / C / Ruby。

**语义上下文压缩**
历史超过 12 条时触发 LLM 摘要压缩，将早期消息压缩为单条结构化 JSON 表示，在保留语义信号的同时大幅节省 token。

**流式输出**
模型 thought 逐 token 实时打印，工具调用实时显示，体验接近 Claude Code。

**安全机制（四层）**
- 第一层：黑名单硬拦截 `rm -rf /`、`mkfs`、`curl | bash` 等危险模式
- 第二层：只读白名单 `ls`、`grep`、`git status`、`pytest` 等直接放行
- 第三层：写操作确认 `--confirm` 模式下 `git commit`、`pip install` 等需 y/n 确认
- 第四层：30s 超时 + 8KB 输出截断，防止失控

**Docker 沙箱**
`--sandbox` 参数，所有命令在 `python:3.11-slim` 容器里执行，repo 通过 bind mount 双向同步，默认断网。

**Reflection 机制**
- 测试失败 → 自动触发反思 prompt，重新分析错误原因
- 连续 6 步无文件修改 → 触发反思，防止探索死循环
- 连续 3 步相同操作 → 判定死循环，自动终止

**事件日志**
每次运行生成 JSONL 日志，记录所有 action / observation / reflection，支持完整回放和统计分析。

**持久化仓库记忆**
跨任务积累仓库知识：路径信号（哪些文件经常被修改）、验证信号（哪些测试覆盖哪些模块）、Issue 处理结果，加权评分后注入后续任务的 prompt。

---

## 安全说明

`--confirm` 模式（`run`）和 `chat` 模式默认对写操作要求确认，执行前显示：

```
  ⚠  Agent wants to run:
     $ git commit -m "fix parser bug"
  Allow? [y/N]
```

`--sandbox` 模式在 Docker 容器中执行，宿主机环境完全隔离。

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest                     # 全量（383 passed，7 skipped）
pytest tests/test_day3.py  # 单个文件

# 可选：更多语言的 tree-sitter 支持
pip install tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-go tree-sitter-rust tree-sitter-java

# 可选：精确 token 计数
pip install tiktoken
```

---

## 命令参考

```bash
# chat
repoforge chat [--repo PATH] [--model MODEL] [--sandbox] [-v]

# run
repoforge run --task TEXT [--repo PATH] [--task-file FILE]
          [--model MODEL] [--confirm] [--sandbox] [--no-stream] [-v]

# log
repoforge log list [--dir DIR]
repoforge log show LOG_FILE

# github issue
python -m entry.github_issue \
    -r owner/repo -i ISSUE_NUM -l LOCAL_PATH [--no-pr] [-v]

# pipeline
repoforge-pipe serve [--host HOST] [--port PORT]
```

详细用法见 [USAGE.md](USAGE.md)。
