import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient


os.environ.setdefault("MCP_ISSUER_URL", "http://127.0.0.1:8000")
os.environ.setdefault("MCP_RESOURCE_URL", "http://127.0.0.1:8000/mcp")
os.environ.setdefault("MCP_REQUIRED_SCOPE", "mcp")

import server
from dynamic_tools import DynamicToolRegistry


class DynamicRegistrationTest(unittest.TestCase):
    """验证动态注册请求头的当前原型规则。"""

    def test_header只校验是否传入(self) -> None:
        payload = {
            "base_url": "http://localhost:9000",
            "tools": [
                {
                    "name": "get_order",
                    "description": "查询订单",
                    "path": "/internal/tools/get-order",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            registry = DynamicToolRegistry(Path(directory) / "tools.json", {"localhost"})
            with patch.object(server, "tool_registry", registry), TestClient(server.app) as client:
                missing = client.put("/internal/services/order/tools", json=payload)
                present = client.put(
                    "/internal/services/order/tools",
                    json=payload,
                    headers={"X-Tool-Registration-Token": ""},
                )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(present.status_code, 200)
        self.assertEqual(present.json()["tools"], ["order__get_order"])


if __name__ == "__main__":
    unittest.main()
