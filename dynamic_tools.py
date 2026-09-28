import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import types
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, Field


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_RESPONSE_BYTES = 1024 * 1024


class BusinessTool(BaseModel):
    """描述业务服务对外注册的单个 MCP 工具。"""

    name: str
    description: str
    path: str
    input_schema: dict[str, Any]
    read_only: bool = False


class ServiceCatalog(BaseModel):
    """描述一个业务服务提交的完整工具目录。"""

    base_url: str
    tools: list[BusinessTool] = Field(default_factory=list)


class DynamicToolRegistry:
    """持久化动态工具目录，并负责把 MCP 调用转发给对应业务服务。"""

    def __init__(self, state_file: Path, allowed_hosts: set[str]) -> None:
        self._state_file = state_file
        self._allowed_hosts = allowed_hosts
        self._catalogs: dict[str, ServiceCatalog] = {}
        self._load()

    def replace_service_catalog(self, service_name: str, catalog: ServiceCatalog) -> list[str]:
        """校验并整体替换指定服务的工具目录。"""

        self._validate_service_name(service_name)
        self._validate_catalog(catalog)
        self._catalogs[service_name] = catalog
        self._save()
        return [self._public_name(service_name, tool.name) for tool in catalog.tools]

    def list_tools(self) -> list[types.Tool]:
        """将所有业务工具转换为 MCP 工具描述。"""

        return [
            types.Tool(
                name=self._public_name(service_name, tool.name),
                description=tool.description,
                inputSchema=tool.input_schema,
                annotations=types.ToolAnnotations(readOnlyHint=tool.read_only),
            )
            for service_name, catalog in self._catalogs.items()
            for tool in catalog.tools
        ]

    async def call_tool(
        self, public_name: str, arguments: dict[str, Any], access_token: AccessToken
    ) -> types.CallToolResult:
        """校验参数，携带最终用户身份调用业务服务。"""

        target = self._find_tool(public_name)
        if target is None:
            return self._error(f"工具不存在: {public_name}")

        catalog, tool = target
        if not access_token.subject:
            return self._error("当前 token 缺少最终用户身份")

        print(f"access_token: {access_token.model_dump_json()}")
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
            result = await asyncio.to_thread(
                self._post_json,
                f"{catalog.base_url.rstrip('/')}{tool.path}",
                arguments,
                access_token.subject,
                access_token.client_id,
            )
        except ValidationError as error:
            return self._error(f"工具参数不符合 schema: {error.message}")
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            return self._error(f"业务服务调用失败: {error}")

        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result,
        )

    def _load(self) -> None:
        """从本地状态文件恢复工具目录。"""

        if not self._state_file.exists():
            return
        state = json.loads(self._state_file.read_text(encoding="utf-8"))
        self._catalogs = {
            service_name: ServiceCatalog.model_validate(catalog)
            for service_name, catalog in state.items()
        }
        for service_name, catalog in self._catalogs.items():
            self._validate_service_name(service_name)
            self._validate_catalog(catalog)

    def _save(self) -> None:
        """原子写入动态工具目录。"""

        self._state_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._state_file.parent, 0o700)
        temporary_file = self._state_file.with_suffix(".tmp")
        descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(
                {
                    service_name: catalog.model_dump(mode="json")
                    for service_name, catalog in self._catalogs.items()
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_file, self._state_file)
        os.chmod(self._state_file, 0o600)

    def _validate_catalog(self, catalog: ServiceCatalog) -> None:
        """拒绝不安全的目标地址、路径和无效 JSON Schema。"""

        parsed_url = urlsplit(catalog.base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.hostname not in self._allowed_hosts
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.path not in {"", "/"}
        ):
            raise ValueError("base_url 必须是允许访问的 HTTP(S) 服务根地址")

        seen_names: set[str] = set()
        for tool in catalog.tools:
            if not NAME_PATTERN.fullmatch(tool.name):
                raise ValueError(f"工具名不合法: {tool.name}")
            if tool.name in seen_names:
                raise ValueError(f"工具名重复: {tool.name}")
            seen_names.add(tool.name)

            parsed_path = urlsplit(tool.path)
            if (
                not tool.path.startswith("/")
                or parsed_path.scheme
                or parsed_path.netloc
                or parsed_path.query
                or parsed_path.fragment
                or ".." in parsed_path.path.split("/")
            ):
                raise ValueError(f"工具路径不合法: {tool.path}")
            if tool.input_schema.get("type") != "object":
                raise ValueError(f"工具 {tool.name} 的 input_schema 根类型必须是 object")
            try:
                Draft202012Validator.check_schema(tool.input_schema)
            except SchemaError as error:
                raise ValueError(f"工具 {tool.name} 的 input_schema 不合法: {error.message}") from error

    @staticmethod
    def _validate_service_name(service_name: str) -> None:
        """校验服务名可安全用于公开工具名。"""

        if not NAME_PATTERN.fullmatch(service_name):
            raise ValueError("服务名只允许小写字母、数字、下划线和连字符")

    def _find_tool(self, public_name: str) -> tuple[ServiceCatalog, BusinessTool] | None:
        """按公开工具名定位业务服务与工具。"""

        for service_name, catalog in self._catalogs.items():
            for tool in catalog.tools:
                if self._public_name(service_name, tool.name) == public_name:
                    return catalog, tool
        return None

    @staticmethod
    def _public_name(service_name: str, tool_name: str) -> str:
        """生成 MCP Client 可见的全局唯一工具名。"""

        return f"{service_name}__{tool_name}"

    @staticmethod
    def _post_json(
        url: str, arguments: dict[str, Any], user_id: str, client_id: str
    ) -> dict[str, Any]:
        """使用标准库向业务服务发送一次 JSON 请求。"""

        request = Request(
            url,
            data=json.dumps(arguments, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-MCP-User-ID": user_id,
                "X-MCP-Client-ID": client_id,
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("业务服务响应超过 1 MiB")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("业务服务响应必须是 JSON 对象")
        return result

    @staticmethod
    def _error(message: str) -> types.CallToolResult:
        """生成 MCP 标准错误结果，避免业务异常中断会话。"""

        return types.CallToolResult(
            content=[types.TextContent(text=message)],
            isError=True,
        )
