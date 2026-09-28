# 开源 MCP Gateway 方案调研

调研日期：2026-09-24

## 结论

按当前项目的目标（统一 MCP 入口、兼容 MCP OAuth、保留最终用户身份、按用户/工具授权、屏蔽下游鉴权）排序：

1. **IBM ContextForge**：能力最接近完整目标，也是 Python 项目；代价是系统很重。
2. **agentgateway**：入站 MCP OAuth、外部 IdP 适配和细粒度授权最成熟，单二进制部署也简单；不是 Python，且更偏基础设施代理。
3. **Cortex Gateway**：架构定位与当前设想最接近，直接面向普通 HTTP 业务后端并传播真实用户 JWT；但项目较新，不适合作为当前首选生产依赖。
4. **MetaMCP**：适合快速获得带 UI 的聚合、命名空间和 MCP OAuth；官方资料没有证明它能完整传播最终用户身份或实施细粒度工具授权。
5. **Docker MCP Gateway**：适合本地开发、容器隔离和统一工具入口，不适合当前面向公网、多用户的认证与授权目标。

如果目标是尽快交付现有设计，优先评估 **agentgateway 作为独立网关组件**；如果必须在 Python 内深度改造，并且能接受较重的平台，优先评估 **ContextForge**。不建议继续从零实现 OAuth 协议适配层，除非现有项目只打算保留非常小的业务专用能力集。

## 对比

| 方案 | 聚合/路由 | MCP OAuth / 外部 IdP | 最终用户与细粒度授权 | 下游鉴权屏蔽 | 技术栈与部署 | 判断 |
| --- | --- | --- | --- | --- | --- | --- |
| [IBM ContextForge](https://github.com/IBM/mcp-context-forge) | MCP、REST、gRPC、A2A 联邦与统一端点；REST/gRPC 可虚拟化为 MCP 工具 | RFC 9728 protected-resource metadata；支持 JWT、SSO/OIDC、外部 IdP access token；支持 GitHub、Google、Entra、Keycloak、Okta、通用 OIDC | 内置用户、团队、RBAC、token scope；可从 JWT/SSO 提取身份；支持向下游传播用户上下文 | 支持每用户 OAuth token、刷新、加密存储、HMAC 签名身份头、RFC 8693 token exchange | Python 3.11+；可 PyPI + SQLite 单机，也可 Compose + PostgreSQL + Redis + Nginx | **最贴近全量需求，但明显过重** |
| [agentgateway](https://github.com/agentgateway/agentgateway) | MCP tool federation，支持 stdio、HTTP、SSE、Streamable HTTP 和 OpenAPI | 作为 resource server 校验 JWT；可为 Keycloak、Auth0、Okta、Descope、authentik、Entra 提供 MCP OAuth facade，补齐 discovery/DCR 差异 | JWT claims 可进入 CEL；能按 `jwt.sub`、角色、工具名和目标做授权，并从 `tools/list` 过滤无权工具 | 支持 backend auth、OAuth token exchange；也可用 transformation 把已验证 JWT claim 写入下游请求 | Rust 单二进制 + YAML；有 standalone 和 Kubernetes 模式 | **最适合直接采用为网关层；不适合按 Python 库嵌入** |
| [Cortex Gateway](https://github.com/wellknownmcp/cortex-gateway) | 将普通 HTTP backend contract 和原生 MCP server 聚合为一个入口 | OAuth 2.1 Resource Server，支持 JWKS、RFC 8707、RFC 9728；生产环境仍需要外部授权服务器 | 将同一个最终用户 JWT 传播到第一方后端；按 scope 过滤和校验工具 | 第一方后端接收用户 JWT；第三方 MCP 通过每用户加密 token vault | TypeScript/Next.js；MIT；调研时约 67 次提交、12 stars | **定位最接近，但成熟度风险明显** |
| [MetaMCP](https://github.com/metatool-ai/metamcp) | 多 MCP server 聚合成 namespace/endpoint，支持中间件、SSE、Streamable HTTP、OpenAPI | 暴露端点可选 MCP 2025-06-18 OAuth；支持 DCR、PKCE；UI 登录支持通用 OIDC | 有用户、多租户、公私资源和按用户限流，但官方文档未说明按 JWT `sub` 对工具做 RBAC，也未说明向下游传播可信用户身份 | 可保存上游 MCP OAuth 会话/凭据；未找到签名身份头、OBO/token exchange 等官方能力说明 | TypeScript/Node monorepo；推荐 Docker Compose，依赖 PostgreSQL | **适合聚合/UI 原型，不足以确认满足企业身份链路** |
| [Docker MCP Gateway](https://github.com/docker/mcp-gateway) | 多 MCP server 的统一入口、工具发现/allowlist、容器隔离与生命周期管理 | 内置的是连接下游 MCP 服务所需的 OAuth；HTTP 入站默认使用单个 bearer token | 官方文档未说明外部 IdP 用户登录、按最终用户 RBAC 或身份传播 | 擅长 secrets/OAuth 凭据注入到容器化 MCP server | Go CLI/Docker Desktop 或 Docker CE；运行简单但强绑定 Docker 运行模型 | **本地开发和工具运行平台，不是本项目要做的多用户公网网关** |

## 逐项说明

### 1. IBM ContextForge

与当前定位的重合度最高：官方 README 明确说明它能把多个 MCP 和 REST/gRPC 服务联邦到统一端点，并把传统 API 虚拟化为 MCP 工具；还提供认证、限流、重试和管理 UI。[项目 README](https://github.com/IBM/mcp-context-forge#readme)

认证与最终用户能力比较完整：

- 支持 OAuth2/OIDC SSO，覆盖 Entra ID、Keycloak、Okta 和通用 OIDC，并可直接接受可信外部 IdP 签发的 access token 访问 MCP/API。[SSO 文档](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/manage/sso.md)
- RBAC 同时区分资源可见范围和操作权限，身份来自 JWT、SSO 或代理认证，支持用户、团队和角色。[RBAC 文档](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/manage/rbac.md)
- RFC 9728 元数据可以让 MCP Client 自动发现真正的授权服务器并发起浏览器登录。[RFC 9728 文档](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/architecture/rfc9728-compliance.md)
- 下游身份传播支持 HTTP headers、MCP `_meta`、敏感字段过滤、可选 HMAC-SHA256 签名；需要下游用户 token 时支持 RFC 8693 token exchange。[身份传播 ADR](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/architecture/adr/041-identity-propagation.md)、[身份传播运维文档](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/manage/identity-propagation.md)
- 上游 OAuth token 按“gateway + app user”隔离并加密存储，能刷新 access token；DCR 和 PKCE 已实现。[OAuth 设计](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/architecture/oauth-design.md)、[安全特性](https://github.com/IBM/mcp-context-forge/blob/main/docs/docs/architecture/security-features.md)

主要问题是复杂度。开发模式可以用 Python/PyPI + SQLite，但完整部署包含 PostgreSQL、Redis、Nginx，配置项超过 300 个；这远超当前最小 Gateway。[安装与运行说明](https://github.com/IBM/mcp-context-forge#quick-start---pypi)

需要验证的点：它能通过 RFC 9728 把 MCP Client 引导到外部授权服务器，也能验证外部 token；但对选定办公套件 IdP 的 audience、DCR 支持和 Client 兼容性仍需做一次实际联调，不能只凭功能列表判断。

### 2. agentgateway

这是更专注于代理数据面的方案。官方项目说明支持 MCP federation、OAuth、OpenAPI，以及 JWT/API key/OAuth、CEL RBAC、TLS 和 OpenTelemetry。[项目仓库](https://github.com/agentgateway/agentgateway)

它对当前遇到的 OAuth 兼容问题尤其有价值：

- 可以直接配置标准外部 Authorization Server 的 issuer、audience 和 JWKS，并暴露 MCP protected-resource metadata。[MCP authentication 示例](https://github.com/agentgateway/agentgateway/blob/main/examples/mcp-authentication/README.md)
- 对 Keycloak、Auth0、Okta、Descope、authentik、Entra 等不完全贴合 MCP 发现/DCR 的 IdP，Gateway 可以作为 authorization-server facade，改写元数据并适配注册流程。[同一示例的 provider 场景](https://github.com/agentgateway/agentgateway/blob/main/examples/mcp-authentication/README.md#scenario-c-adapting-a-vendor-authorization-server-eg-keycloak)
- 验证后的 JWT claims 可用于 MCP CEL 规则，例如按 `jwt.sub` 和工具名授权；无权工具会从 `tools/list` 中过滤。[MCP authorization 文档](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/mcp-authz/)
- 下游可使用固定 backend credential、OAuth token exchange，或把可信 JWT claim 变换成内部 header。[Backend authentication 文档](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/backend-authn/)、[JWT claim transformation](https://agentgateway.dev/docs/kubernetes/latest/documentation/security/jwt/setup/)

部署上可直接使用 standalone 二进制和 YAML，也支持 Kubernetes；比完整 ContextForge 轻。缺点是核心为 Rust，若团队坚持 Python 内嵌扩展，就只能把它当独立基础设施组件。它也不提供 ContextForge 那样完整的用户/团队数据库；通常以企业 IdP 的 claims 为授权事实来源。

### 3. Cortex Gateway

Cortex Gateway 的目标与当前设计几乎一致：MCP Client 只连接一个 OAuth 2.1 入口，Gateway 将工具请求路由到普通 HTTP 业务后端，并把最终用户 JWT 继续传递给后端，由业务服务保留自己的权限模型。[项目 README](https://github.com/wellknownmcp/cortex-gateway#readme)

它也支持 scope 过滤、审计、RFC 9728、RFC 8707，以及通过 adapter 联邦原生 MCP server。但它自身只是 OAuth Resource Server，生产环境仍要接入能签发 JWT 的外部授权服务器；同时项目规模尚小，应视为架构参考或验证候选，而不是直接认定为成熟生产底座。

### 4. MetaMCP

MetaMCP 是成熟度较高的聚合/UI 方案：把多个 MCP server 组成 namespace，再发布成单一 endpoint，并支持工具筛选和中间件。[项目 README](https://github.com/metatool-ai/metamcp#readme)

它已经提供 MCP 2025-06-18 OAuth、DCR、PKCE，并把浏览器登录用户绑定到 access token；同时支持通用 OIDC 登录和自动创建用户。[OAuth 流程](https://github.com/metatool-ai/metamcp/blob/ai-dev/README-oauth.md)、[认证与 OIDC 配置](https://github.com/metatool-ai/metamcp#-authentication)

但在当前关键需求上，官方资料只明确了多租户、公私资源、API key 和按用户限流。未找到以下能力的官方说明：

- 根据最终用户身份和工具名实施细粒度 RBAC；
- 把经过验证的最终用户身份以签名 header 或 MCP `_meta` 传给下游；
- 使用 OBO/RFC 8693 把入站用户 token 换成下游用户 token。

因此它更适合快速获得聚合、管理 UI 和 OAuth 登录，不能直接假定已满足完整身份链路。部署为 Docker Compose，应用是 TypeScript/Node monorepo并依赖 PostgreSQL。[Quick Start](https://github.com/metatool-ai/metamcp#-quick-start)

### 5. Docker MCP Gateway

Docker MCP Gateway 提供单一客户端入口、多 server 工具发现、tool allowlist、容器隔离、secret 管理和访问下游服务的 OAuth。[项目 README](https://github.com/docker/mcp-gateway#readme)、[server entry OAuth 配置](https://github.com/docker/mcp-gateway/blob/main/docs/server-entry-spec.md)

它的主要目标是安全运行和配置本地/容器化 MCP servers。HTTP transport 的入站保护默认是一个 `MCP_GATEWAY_AUTH_TOKEN`，官方安全文档没有描述外部 IdP 的 MCP OAuth 登录、最终用户 `sub`、按用户 RBAC 或可信身份下传。[安全边界](https://github.com/docker/mcp-gateway/blob/main/docs/security.md)

因此它可作为本地开发工具或下游 MCP 运行层，但不应当作当前公网、多用户 Gateway 的直接替代。

## 建议的下一步

只做两个小型验证，不同时引入多个平台：

1. 用 **agentgateway standalone** 配置当前临时授权服务器或企业 IdP，验证 MCP Client 能自动完成 discovery、DCR/预注册、登录和 token 调用，再加一条基于 `jwt.sub` 的工具授权规则。
2. 用 **ContextForge 的 PyPI + SQLite 模式** 验证一个 REST 业务接口包装为 MCP tool，并用签名 header 把最终用户身份传到下游。

验证后再选型：agentgateway 成功则可以删除当前项目中大部分通用 OAuth 适配代码，只保留业务工具/路由配置；ContextForge 成功且确实需要用户、团队、管理 UI、token vault，再考虑直接采用它。MetaMCP 和 Docker MCP Gateway 暂不投入验证。
