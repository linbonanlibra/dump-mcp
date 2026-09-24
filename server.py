import asyncio
import os
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient, PyJWTError
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class Settings:
    """保存 MCP Gateway 启动所需的最小配置。"""

    issuer_url: str
    resource_url: str
    jwks_url: str
    jwt_algorithm: str
    required_scope: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取配置，缺少认证配置时直接拒绝启动。"""

        required = {
            name: os.environ.get(name)
            for name in (
                "MCP_ISSUER_URL",
                "MCP_RESOURCE_URL",
                "MCP_JWKS_URL",
                "MCP_JWT_ALGORITHM",
                "MCP_REQUIRED_SCOPE",
            )
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")

        return cls(
            issuer_url=required["MCP_ISSUER_URL"],
            resource_url=required["MCP_RESOURCE_URL"],
            jwks_url=required["MCP_JWKS_URL"],
            jwt_algorithm=required["MCP_JWT_ALGORITHM"],
            required_scope=required["MCP_REQUIRED_SCOPE"],
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )


class JwtTokenVerifier(TokenVerifier):
    """通过授权服务器的 JWKS 验证 JWT，并提取最终用户身份。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks = PyJWKClient(settings.jwks_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        """验证签名和标准声明，验证失败时让 SDK 返回 401。"""

        try:
            claims = await asyncio.to_thread(self._decode, token)
            subject = self._required_string(claims, "sub")
            client_id = self._required_string(claims, "client_id")
            scope = self._required_string(claims, "scope")
            expires_at = claims["exp"]
            if not isinstance(expires_at, int):
                return None

            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scope.split(),
                expires_at=expires_at,
                resource=self._settings.resource_url,
                subject=subject,
                claims={"iss": claims["iss"]},
            )
        except (PyJWTError, KeyError, TypeError, ValueError):
            return None

    def _decode(self, token: str) -> dict[str, Any]:
        """在线程中获取签名密钥，避免阻塞 ASGI 事件循环。"""

        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[self._settings.jwt_algorithm],
            audience=self._settings.resource_url,
            issuer=self._settings.issuer_url,
            options={"require": ["aud", "client_id", "exp", "iss", "scope", "sub"]},
        )

    @staticmethod
    def _required_string(claims: dict[str, Any], name: str) -> str:
        """确保身份声明是非空字符串。"""

        value = claims[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"JWT 声明 {name} 必须是非空字符串")
        return value


settings = Settings.from_env()
mcp = MCPServer(
    "MCP Gateway",
    version="0.1.0",
    token_verifier=JwtTokenVerifier(settings),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.issuer_url),
        resource_server_url=AnyHttpUrl(settings.resource_url),
        required_scopes=[settings.required_scope],
        validate_token_resource=True,
    ),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """提供无需认证的存活检查。"""

    return JSONResponse({"status": "ok"})


@mcp.tool()
def whoami() -> dict[str, object]:
    """返回当前 MCP 请求中已经验证的用户身份。"""

    access_token = get_access_token()
    if access_token is None:
        raise RuntimeError("当前请求缺少已验证的用户身份")

    return {
        "issuer": (access_token.claims or {}).get("iss"),
        "user_id": access_token.subject,
        "client_id": access_token.client_id,
        "scopes": access_token.scopes,
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        json_response=True,
    )
