import html
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


STATE_FILE = Path(__file__).parent / ".runtime" / "oauth-state.json"


@dataclass(frozen=True)
class Settings:
    """保存 MCP Gateway 与内置开发授权服务器的启动配置。"""

    issuer_url: str
    resource_url: str
    required_scope: str
    dev_users: tuple[str, ...]
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取配置，缺少必要配置时直接拒绝启动。"""

        required = {
            name: os.environ.get(name)
            for name in ("MCP_ISSUER_URL", "MCP_RESOURCE_URL", "MCP_REQUIRED_SCOPE")
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")

        dev_users = tuple(
            user.strip()
            for user in os.environ.get("MCP_DEV_USERS", "user-a,user-b").split(",")
            if user.strip()
        )
        if not dev_users:
            raise RuntimeError("MCP_DEV_USERS 至少需要配置一个开发用户")

        return cls(
            issuer_url=required["MCP_ISSUER_URL"],
            resource_url=required["MCP_RESOURCE_URL"],
            required_scope=required["MCP_REQUIRED_SCOPE"],
            dev_users=dev_users,
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )


class PendingAuthorization(BaseModel):
    """保存等待开发用户确认的授权请求。"""

    client_id: str
    params: AuthorizationParams


class PersistentAuthorizationServerProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """提供开发阶段的 OAuth 授权服务，并将运行状态持久化到本地文件。"""

    def __init__(self, settings: Settings, state_file: Path) -> None:
        self._settings = settings
        self._state_file = state_file
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.pending_authorizations: dict[str, PendingAuthorization] = {}
        self._load()

    def _load(self) -> None:
        """从状态文件恢复客户端、授权码和 token；文件损坏时拒绝启动。"""

        if not self._state_file.exists():
            return

        state = json.loads(self._state_file.read_text(encoding="utf-8"))
        self.clients = {
            key: OAuthClientInformationFull.model_validate(value)
            for key, value in state["clients"].items()
        }
        self.codes = {
            key: AuthorizationCode.model_validate(value)
            for key, value in state["codes"].items()
        }
        self.access_tokens = {
            key: AccessToken.model_validate(value)
            for key, value in state["access_tokens"].items()
        }
        self.refresh_tokens = {
            key: RefreshToken.model_validate(value)
            for key, value in state["refresh_tokens"].items()
        }
        self.pending_authorizations = {
            key: PendingAuthorization.model_validate(value)
            for key, value in state["pending_authorizations"].items()
        }

    def _save(self) -> None:
        """以原子替换方式写入状态，避免进程中断留下半个 JSON 文件。"""

        state = {
            "clients": {key: value.model_dump(mode="json") for key, value in self.clients.items()},
            "codes": {key: value.model_dump(mode="json") for key, value in self.codes.items()},
            "access_tokens": {
                key: value.model_dump(mode="json") for key, value in self.access_tokens.items()
            },
            "refresh_tokens": {
                key: value.model_dump(mode="json") for key, value in self.refresh_tokens.items()
            },
            "pending_authorizations": {
                key: value.model_dump(mode="json")
                for key, value in self.pending_authorizations.items()
            },
        }
        self._state_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._state_file.parent, 0o700)
        temporary_file = self._state_file.with_suffix(".tmp")
        descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_file, self._state_file)
        os.chmod(self._state_file, 0o600)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """按 client_id 获取动态注册的客户端。"""

        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """保存 MCP Client 动态注册得到的完整信息。"""

        self.clients[client_info.client_id] = client_info
        self._save()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """保存授权上下文，并跳转到开发用户选择页。"""

        if params.resource != self._settings.resource_url:
            raise AuthorizeError(error="invalid_target", error_description="resource 与 MCP Gateway 不匹配")

        request_id = f"request_{secrets.token_hex(20)}"
        self.pending_authorizations[request_id] = PendingAuthorization(
            client_id=client.client_id,
            params=params,
        )
        self._save()
        query = urlencode({"request_id": request_id})
        return f"{self._settings.issuer_url.rstrip('/')}/dev/authorize?{query}"

    def get_pending_authorization(self, request_id: str) -> PendingAuthorization | None:
        """读取等待用户选择的授权请求。"""

        return self.pending_authorizations.get(request_id)

    def complete_authorization(self, request_id: str, user_id: str) -> str:
        """将所选开发用户写入一次性授权码，并返回 Client 回调地址。"""

        if user_id not in self._settings.dev_users:
            raise ValueError("未知的开发用户")

        pending = self.pending_authorizations.pop(request_id, None)
        if pending is None:
            raise ValueError("授权请求不存在或已经完成")

        params = pending.params
        code = AuthorizationCode(
            code=f"code_{secrets.token_hex(20)}",
            client_id=pending.client_id,
            scopes=params.scopes or [self._settings.required_scope],
            expires_at=time.time() + 300,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=user_id,
        )
        self.codes[code.code] = code
        self._save()
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """读取待交换的一次性授权码。"""

        return self.codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """消费授权码，并签发绑定最终用户的 access token 与 refresh token。"""

        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="authorization code 已被使用")

        access_token, refresh_token = self._mint_tokens(
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """按 Bearer token 恢复 Gateway 请求中的用户身份。"""

        return self.access_tokens.get(token)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """读取属于当前客户端的 refresh token。"""

        token = self.refresh_tokens.get(refresh_token)
        return token if token is not None and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """轮换 refresh token，并保持原有最终用户身份。"""

        if self.refresh_tokens.pop(refresh_token.token, None) is None:
            raise TokenError(error="invalid_grant", error_description="refresh token 已被使用")

        access_token, new_refresh_token = self._mint_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=refresh_token.resource,
            subject=refresh_token.subject,
        )
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
            refresh_token=new_refresh_token,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """撤销指定的 access token 或 refresh token。"""

        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        self._save()

    def _mint_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str | None,
    ) -> tuple[str, str]:
        """生成随机 token，并保存其客户端、用户和资源绑定关系。"""

        access_value = f"access_{secrets.token_hex(32)}"
        refresh_value = f"refresh_{secrets.token_hex(32)}"
        self.access_tokens[access_value] = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 3600,
            resource=resource,
            subject=subject,
            claims={"iss": self._settings.issuer_url},
        )
        self.refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 30 * 24 * 3600,
            resource=resource,
            subject=subject,
        )
        return access_value, refresh_value


settings = Settings.from_env()
provider = PersistentAuthorizationServerProvider(settings, STATE_FILE)
mcp = MCPServer(
    "MCP Gateway",
    version="0.2.0",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.issuer_url),
        resource_server_url=AnyHttpUrl(settings.resource_url),
        required_scopes=[settings.required_scope],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[settings.required_scope],
            default_scopes=[settings.required_scope],
        ),
        validate_token_resource=True,
    ),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """提供无需认证的存活检查。"""

    return JSONResponse({"status": "ok"})


@mcp.custom_route("/dev/authorize", methods=["GET", "POST"])
async def dev_authorize(request: Request) -> HTMLResponse | RedirectResponse:
    """展示开发用户选择页，并把选中的用户绑定到授权码。"""

    if request.method == "POST":
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        user_id = str(form.get("user_id", ""))
        try:
            redirect_uri = provider.complete_authorization(request_id, user_id)
        except ValueError as error:
            return HTMLResponse(html.escape(str(error)), status_code=400)
        return RedirectResponse(redirect_uri, status_code=302)

    request_id = request.query_params.get("request_id", "")
    pending = provider.get_pending_authorization(request_id)
    if pending is None:
        return HTMLResponse("授权请求不存在或已经完成", status_code=400)

    client = await provider.get_client(pending.client_id)
    client_name = client.client_name if client and client.client_name else pending.client_id
    buttons = "".join(
        f'<button type="submit" name="user_id" value="{html.escape(user)}">'
        f"{html.escape(user)}</button>"
        for user in settings.dev_users
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MCP 开发授权</title></head>
<body>
  <h1>选择开发用户</h1>
  <p>应用：{html.escape(client_name)}</p>
  <p>此页面只用于验证 OAuth 和最终用户身份传递流程。</p>
  <form method="post">
    <input type="hidden" name="request_id" value="{html.escape(request_id)}">
    {buttons}
  </form>
</body>
</html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


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
