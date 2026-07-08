"""
pipeline/auth.py

GitHub App 认证。

GitHub App 有两层认证：
1. JWT（App 级别）—— 用 App 私钥签发，用于获取 installation token
2. Installation Token（仓库级别）—— 用 JWT 换取，用于实际操作仓库

用法：
    auth = GitHubAppAuth(app_id="...", private_key_path="key.pem")
    token = auth.get_installation_token(installation_id=12345)
    # 用 token 操作 GitHub API
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class GitHubAppAuth:
    """GitHub App 认证管理器，处理 JWT 签发和 token 交换。"""

    def __init__(
        self,
        app_id: str | int,
        private_key: str | Path | None = None,
        private_key_path: str | Path | None = None,
    ) -> None:
        self.app_id = int(app_id)
        self._private_key: str | None = None
        self._key_path: Path | None = None

        if private_key:
            self._private_key = private_key
        elif private_key_path:
            self._key_path = Path(private_key_path)

    # ------------------------------------------------------------------
    # JWT
    # ------------------------------------------------------------------

    def create_jwt(self) -> str:
        """签发 GitHub App JWT（有效期 10 分钟）。"""
        import jwt

        now = int(time.time())
        payload = {
            "iat": now - 60,          # 允许 60s 时钟偏移
            "exp": now + 600,         # 10 分钟（GitHub 最大允许值）
            "iss": self.app_id,
        }
        key = self._load_key()
        return jwt.encode(payload, key, algorithm="RS256")

    def _load_key(self) -> str:
        """加载私钥。"""
        import jwt

        if self._private_key:
            key = self._private_key
            # 可能是 PEM 文本，也可能是文件路径
            if "-----BEGIN" in key:
                return key
            path = Path(key)
            if path.exists():
                return path.read_text(encoding="utf-8")
            # 当作裸 PEM（没有文件头尾）
            return key

        if self._key_path:
            return self._key_path.read_text(encoding="utf-8")

        raise ValueError(
            "No private key provided. Set GITHUB_APP_PRIVATE_KEY "
            "environment variable or pass private_key_path."
        )

    # ------------------------------------------------------------------
    # Installation Token
    # ------------------------------------------------------------------

    def get_installation_token(self, installation_id: int) -> str:
        """
        用 JWT 换取 installation access token。

        这个 token 有权限操作该 installation 下的仓库，
        有效期 1 小时。
        """
        import requests

        jwt_token = self.create_jwt()
        url = (
            f"https://api.github.com/app/installations/"
            f"{installation_id}/access_tokens"
        )
        resp = requests.post(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["token"]
        logger.info(
            "Installation token obtained for %d, expires %s",
            installation_id, data.get("expires_at", "?"),
        )
        return token

    def get_github_client(self, installation_id: int):
        """获取已认证的 PyGithub 客户端。"""
        from github import Github

        token = self.get_installation_token(installation_id)
        return Github(token)
