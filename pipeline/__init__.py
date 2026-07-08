"""
pipeline — CI 原生 agent 管线。

将 Repoforge 接入 GitHub 事件流，实现：
- Issue 自动修复（issue opened → agent → PR）
- PR 自动 Review（PR opened/synchronize → agent → review comment）
- CI 失败自动调试（check_run failed → agent → fix commit）

依赖：
    pip install flask
"""

__all__ = []
