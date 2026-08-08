<p align="center">
  <img src="app/static/brand/logo-mark.svg" width="112" alt="proxy2openconnect logo">
</p>

<h1 align="center">proxy2openconnect</h1>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-9bef4d.svg" alt="License: MIT"></a>
</p>

`proxy2openconnect` 是一个运行在 Docker 中的代理网关：客户端连接 Xray 提供的 SOCKS5 或 HTTP 代理，选中的出站流量再通过 OpenConnect 建立的 Cisco AnyConnect 兼容 VPN 隧道转发。项目同时提供一个用于配置 VPN、Xray 和查看运行日志的 Web 控制台。

> 本项目使用开源 OpenConnect 实现协议兼容，不包含 Cisco 专有客户端，与 Cisco Systems, Inc. 无隶属、授权或支持关系。使用前请确认你的组织政策允许第三方 VPN 客户端和代理转发。

## 功能

- SOCKS5 和 HTTP 代理入口，默认启用用户名/密码认证。
- Cisco AnyConnect 兼容 VPN，支持认证组、密码、OTP/MFA、DTLS、客户端证书和企业 CA。
- 服务端结束会话后自动重连，并支持按网址和时间间隔发送 VPN 保活请求。
- 展示 VPN 时长、实时/累计流量、重试状态和目标连接，并按 VPN 配置保留高频目标历史。
- 三种路由模式：全部接管、使用 VPN 下发路由、手动 CIDR 网段。
- 可切换容器默认、VPN 下发或手动指定的全局 DNS，支持内网域名解析。
- Xray outbound 标签级分流，只为指定 outbound 注入 Linux `SO_MARK`。
- VPN 断开时撤销策略路由并回落普通网络，不主动阻断代理流量。
- Web 登录保护、失败登录限流、安全响应头和 HttpOnly 会话 Cookie。
- 服务器证书公钥指纹确认、配置持久化和最近 500 行内存日志。
- `linux/amd64`、`linux/arm64`、`linux/arm/v7` 容器镜像。

## 流量模型

```text
客户端 ── SOCKS5 :1080 ─┐
                        ├──► Xray ── SO_MARK 255 ──► 策略路由表 200
客户端 ─── HTTP :8080 ──┘                                  │
                                             ┌──────────────┴──────────────┐
                                        当前模式包含的目标              其他目标
                                             │                            │
                                          tun0 / VPN                  普通网络
```

只有 `XRAY_VPN_OUTBOUND_TAGS` 选中的 Xray outbound 会被应用注入 mark。Web 控制台、OpenConnect 控制连接、容器自身流量和未选中的 outbound 继续使用普通路由，避免 VPN 控制连接递归进入隧道。

## 运行要求

- Linux Docker Engine 24+ 和 Docker Compose v2。
- 可用的 `/dev/net/tun`。
- 容器运行时允许 `NET_ADMIN` capability。
- 获得授权的 AnyConnect/ASA VPN 账号。

Windows 用户建议使用 Docker Desktop 的 WSL2 后端。部分托管容器平台不提供 TUN 或 `NET_ADMIN`，无法运行本项目。

## 快速开始

### 1. 获取项目

```bash
git clone https://github.com/fortressme/proxy2openconnect.git
cd proxy2openconnect
cp .env.example .env
```

编辑 `.env`，至少设置两个互不相同的随机值：

```dotenv
ADMIN_PASSWORD=replace-with-a-long-random-password
SESSION_SECRET=replace-with-at-least-32-random-characters
```

可以使用 OpenSSL 生成：

```bash
openssl rand -base64 24
openssl rand -hex 32
```

不要提交 `.env` 或 `data/`。

### 2. 启动

使用 GHCR 已发布镜像：

```bash
docker compose pull
docker compose up -d
```

从当前源代码构建：

```bash
docker compose up -d --build
```

默认端点：

| 服务 | 地址 |
|---|---|
| Web 控制台 | `http://127.0.0.1:8000` |
| SOCKS5 | `127.0.0.1:1080` |
| HTTP Proxy | `127.0.0.1:8080` |

Web 用户名默认为 `admin`，密码来自 `.env` 中的 `ADMIN_PASSWORD`。

### 3. 完成首次配置

1. 登录 Web 控制台。
2. 打开“Xray 配置”，把 SOCKS5 和 HTTP inbound 中的默认代理密码 `change-me` 替换为强密码。
3. 打开“VPN 配置”，填写服务器、用户名和需要的认证组。
4. 选择路由模式，保存后连接 VPN。
5. 如果网关证书不受系统 CA 信任，先通过可信渠道核对页面显示的公钥指纹，再选择信任。

VPN 密码默认只用于本次连接。只有勾选保存密码时，密码才会写入 `/data/vpn/config.json`。

## 自动重连与网址保活

“重连窗口”是 OpenConnect 自身处理 SSL/DTLS 短暂掉线的最长时间。若服务端以 `Idle Timeout` 结束整个会话，应用层的“会话退出后自动重连”会在隧道曾成功建立的前提下，按“自动重连间隔”重新启动 OpenConnect。连续失败 5 次后停止，避免无效凭据持续触发认证；点击“断开连接”也会立即取消待执行的重连。

启用网址保活后，应用会按配置间隔向指定的 HTTP/HTTPS 地址发送一个很小的请求。请求套接字使用与 Xray VPN 出站相同的 Mark 255，并绑定到 `tun0`，因此不会在隧道断开时回落普通网络；目标地址需要被当前路由模式包含。成功或失败结果会显示在 VPN 实时日志中。建议选择稳定、允许周期访问且确实位于企业 VPN 内的健康检查地址，间隔不要短于组织允许的值。

自动重连会在容器进程内存中暂存本次连接使用的密码，手动断开后清除。OTP/MFA 验证码不会重放；如果网关要求每次新会话输入新的验证码，自动重连仍会因缺少新验证码而失败，需要手动重新连接。

## 运行统计与目标历史

“运行总览”展示 `tun0` 的累计字节数、实时收发速率、数据包与错误数，以及 VPN/Xray 运行时间、路由、DNS、保活和自动重试状态。当前连接区域区分连接代理的客户端地址与 Xray 实际连接的远端目标地址。

目标历史只在 VPN 隧道在线时统计 Xray 新建的目标 TCP 连接。每条新建连接计为一次访问；它不是 HTTP 请求数，也不包含 UDP。历史按 VPN 服务器、用户名和认证组生成的不可逆标识独立归档，因此切换 VPN 配置后只会看到对应配置的记录。

记录会先按目标聚合，再每 10 秒追加到数据卷中的每日 JSON Lines 日志：

```text
/data/statistics/<vpn-id>/targets-YYYY-MM-DD.log
```

在 VPN 配置页可将“目标统计保留天数”设为 1–365 天，默认 30 天。早于保留范围的每日日志会自动删除；调整为更短周期后，超期文件不可恢复。

## 全局 DNS

VPN 配置页面提供三种全局 DNS 来源：

- “保持容器默认 DNS”不会修改 Docker 提供的系统解析器。
- “使用 VPN 网关下发的 DNS”读取 OpenConnect 收到的 DNS 地址和搜索域，适合常见的企业内网环境。
- “手动指定 DNS 服务器”允许填写最多 3 个 IPv4/IPv6 DNS 服务器地址。

全局 DNS 在下一次 VPN 连接时生效，供网址保活以及容器内其他使用系统解析器的进程使用。应用会将选中的 DNS 写入容器 `/etc/resolv.conf`，并为每个 DNS 服务器建立优先级更高的目标路由，使查询明确通过 `tun0`，不受 Xray 自身 DNS 设置影响。VPN 断开或启动失败时会恢复原始解析器并移除这些专用路由。

为避免 VPN 网关本身依赖尚未建立的内网 DNS，每次启动 OpenConnect 前都会先恢复容器原始的系统 DNS，解析 VPN 网关域名，然后通过 OpenConnect 的 `--resolve=HOST:IP` 固定该结果。只有隧道建立成功后才启用上面的全局内网 DNS；自动重连也遵循同样顺序。容器原始 DNS 必须能够通过普通网络解析公网 VPN 网关。

如果 Xray 配置显式指定了自己的 DNS 服务器，Xray 仍会使用其自身配置；需要让 Xray 同样使用全局 DNS 时，应避免在 Xray JSON 中覆盖 DNS，或将相同的内网 DNS 同步到 Xray 配置。

## 路由模式

VPN 配置保存在 `/data/vpn/config.json`：

```json
{
  "route_mode": "all",
  "manual_routes": [],
  "manual_exclude_routes": []
}
```

### `all`：接管全部

默认模式。IPv4 安装 `0.0.0.0/0 → tun0`；VPN 提供 IPv6 地址时同时安装 `::/0 → tun0`。VPN 网关仍可能根据服务端策略拒绝访问某些目标。

### `vpn`：使用 VPN 下发路由

读取 OpenConnect 提供的 IPv4/IPv6 split include/exclude 信息：

- AnyConnect 中的 Secured Routes 进入 `tun0`。
- Non-Secured Routes 使用 `throw` 路由继续查询普通主路由。
- 更具体的 Secured Route 优先于较宽的 Non-Secured Route。

### `manual`：手动配置

在 Web 页面中每行填写一个 IPv4 或 IPv6 CIDR：

```text
10.0.0.0/8
192.168.50.10/32
fd00:1234::/48
```

`manual_routes` 中的目标进入 VPN，`manual_exclude_routes` 中更具体的目标回落普通网络。手动模式至少需要一个包含网段。

修改路由模式后，需要重新连接 VPN 才会应用到 OpenConnect 会话。

## Xray 出站选择

默认只为标签为 `vpn-out` 的 outbound 注入：

```json
{
  "streamSettings": {
    "sockopt": {
      "mark": 255
    }
  }
}
```

多个 VPN outbound 标签可在 `.env` 中用逗号分隔：

```dotenv
XRAY_VPN_OUTBOUND_TAGS=vpn-out,corporate-out
```

未选中的 outbound 保持原配置不变。原始 Xray JSON 不会因运行时注入而被改写。

## 验证代理和路由

```bash
curl --proxy socks5h://127.0.0.1:1080 \
  --proxy-user 'xray:YOUR_PROXY_PASSWORD' \
  https://ifconfig.me
```

查看最终路由策略：

```bash
docker compose exec proxy2openconnect cat /run/proxy2openconnect/split-routes
docker compose exec proxy2openconnect ip rule show
docker compose exec proxy2openconnect ip route show table 200
```

运行时路由文件格式示例：

```text
4 include 0.0.0.0/0
4 exclude 192.0.2.0/24
6 include fd00:1234::/48
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Web 管理用户名 |
| `ADMIN_PASSWORD` | 必填 | Web 管理密码，至少 12 个字符 |
| `SESSION_SECRET` | 必填 | 会话签名密钥，至少 32 个字符 |
| `BIND_ADDRESS` | `127.0.0.1` | 宿主机监听地址 |
| `WEB_PORT` | `8000` | Web 管理端口 |
| `SOCKS_PORT` | `1080` | SOCKS5 端口，TCP/UDP |
| `HTTP_PROXY_PORT` | `8080` | HTTP Proxy 端口 |
| `XRAY_VPN_OUTBOUND_TAGS` | `vpn-out` | 需要 VPN 策略的 outbound 标签 |
| `COOKIE_SECURE` | `false` | Web 使用 HTTPS 时设为 `true` |
| `TRUSTED_ORIGINS` | 空 | 允许执行写操作的公网 Web 来源，多个来源用逗号分隔 |
| `TZ` | `UTC` | 容器时区 |
| `IMAGE_NAME` | GHCR 项目镜像 | Compose 镜像名 |
| `IMAGE_TAG` | `latest` | 镜像标签；生产环境建议固定版本 |

如需从其他主机访问，修改 `BIND_ADDRESS` 前必须配置主机防火墙、强代理密码和 Web HTTPS 反向代理。

### HTTPS 反向代理

通过反向代理公开控制台时，在 `.env` 中填写浏览器实际访问的来源。来源只包含协议、域名和可选端口，不能包含路径：

```dotenv
COOKIE_SECURE=true
TRUSTED_ORIGINS=https://vpn.example.com
```

多个入口可使用逗号分隔，例如 `https://vpn.example.com,https://vpn-admin.example.com`。修改后重新创建容器。反向代理仍应传递原始 `Host`、`X-Forwarded-Proto` 和客户端地址；`TRUSTED_ORIGINS` 用于写操作的来源校验，不会允许通配来源。

## 数据与运维

持久化目录：

```text
data/
├── vpn/config.json
└── xray/config.json
```

常用命令：

```bash
docker compose ps
docker compose logs --tail=200
docker compose restart
docker compose down
```

Web 页面中的 OpenConnect/Xray 实时日志只保存在内存中，不会写入数据卷。备份 `data/` 时应使用加密存储，因为其中可能包含 VPN 密码、证书或私钥路径。

## 安全说明

- 默认端口仅绑定 `127.0.0.1`。
- 容器只申请 `/dev/net/tun` 和 `NET_ADMIN`，不需要 `privileged`。
- VPN 密码通过标准输入传给 OpenConnect，不出现在进程参数中。
- 证书和私钥路径只允许位于 `/data/`。
- VPN 断开后流量会回落普通网络；本项目默认不是 fail-closed 防泄漏边界。
- 本项目无法替代组织的设备合规、访问控制和审计策略。

完整安全策略与漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 已知限制

OpenConnect 非交互容器登录通常无法处理：

- 强制打开系统浏览器的 SAML/SSO。
- Cisco Secure Desktop、HostScan 或复杂 DAP 终端合规检查。
- 需要反复选择字段的多阶段交互认证。
- 强制依赖 Cisco Secure Client 专有组件或系统证书存储。

需要解析企业内部域名时，可在 VPN 配置页选择“使用 VPN 网关下发的 DNS”，或手动填写可经 VPN 到达的企业 DNS。

## 故障排查

### 容器或端口不可用

```bash
docker compose ps
docker compose logs --tail=100
curl http://127.0.0.1:8000/health
```

同时确认 `/dev/net/tun` 存在且 Compose 保留了 `NET_ADMIN`。

### 反向代理后提示“请求来源无效”

将公网访问地址加入 `.env` 的 `TRUSTED_ORIGINS`，同时在 HTTPS 部署中设置 `COOKIE_SECURE=true`，然后执行 `docker compose up -d --force-recreate`。

### VPN 已连接但目标没有进入隧道

检查 `/run/proxy2openconnect/split-routes` 和表 200。`all` 模式应至少出现 `4 include 0.0.0.0/0`；`vpn` 模式显示网关下发路由；`manual` 模式显示手动配置路由。

### 内部域名不能解析

先用内部 IP 验证路由，再检查实时日志中的全局 DNS 应用结果。若 Xray JSON 显式配置了 DNS，还需确认它使用相同的企业 DNS。

### 证书显示 `signer not found`

优先配置企业 CA。只有在通过可信渠道核对后，才固定页面显示的服务器公钥指纹。

## 开发与贡献

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
sh -n entrypoint.sh scripts/*.sh
```

贡献流程和代码要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。版本历史见 [CHANGELOG.md](CHANGELOG.md)。维护者发布流程见 [RELEASING.md](RELEASING.md)。

## 许可证与致谢

本项目使用 [MIT License](LICENSE)。第三方组件及商标说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
