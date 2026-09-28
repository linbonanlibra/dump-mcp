
## 测试环境
Quick Tunnel 每次重启地址都会变化，而且不支持 SSE，只适合当前验证.

然后保持 Gateway 运行，并启动隧道：
```bash
cloudflared tunnel --url http://127.0.0.1:8111
```
终端会输出类似：
https://random-name.trycloudflare.com
保持这个进程运行，在另一个终端用该地址重启 Gateway：

```bash
export MCP_ISSUER_URL=https://random-name.trycloudflare.com
export MCP_RESOURCE_URL=https://random-name.trycloudflare.com/mcp
export MCP_REQUIRED_SCOPE=mcp
export MCP_DEV_USERS=user-a,user-b
export MCP_HOST=0.0.0.0
export MCP_PORT=8111

python3 server.py
```

验证
```bash
curl https://random-name.trycloudflare.com/healthz

curl https://random-name.trycloudflare.com/.well-known/oauth-protected-resource/mcp
```

MCP Client 填写：
```bash
https://random-name.trycloudflare.com/mcp
```

## how to start
uv run --env-file .env python server.py

