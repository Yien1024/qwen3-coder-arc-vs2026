# 受限网络下的实测：校园网为什么用不了 Copilot

这篇记录一次完整的排查。场景很具体：国内某高校校园网，想把 VS2026 的 Copilot 接到本机的
Qwen3-Coder 上，结果发现真正卡住的不是模型，是网络。所有数字都是实测，测法也写在里面，
包括我一开始测错的地方。

## 一句话结论

校园网对境外 IP 是整段封锁（不是只封 Copilot），而 GitHub 加速器（Watt Toolkit / 原名 Steam++）
的中继服务不了 Copilot 的域名。所以在这样的网络里，Copilot 没有可行的绕法。

## 网络环境

| 项目 | 值 |
|---|---|
| 网络 | 某高校校园网（教育网） |
| 本机 IP | `10.250.32.45`，网关 `10.255.255.254`（都是内网地址） |
| DNS | 学校自己的 DNS 服务器 |
| 加速工具 | Watt Toolkit 已开 GitHub 加速，运行 1 小时以上 |

## 实测数据

### 对照组：先确认测法本身没问题

| 目标 | DNS | TCP 握手 |
|---|---|---|
| `www.baidu.com` | 110.242.70.57 | **16 ms** |
| `mirrors.aliyun.com` | 119.249.48.37 | **15 ms** |
| `www.youtube.com` | 174.132.167.252 | **超时** |

国内站点 15 毫秒，youtube 超时。说明校园网是**按境外 IP 整段封**，不是针对 Copilot。

这也解释了一件事：只要某个站点走不了加速器，它在这个网络里就是不可用的，
`api.githubcopilot.com` 只是其中之一。

### DNS 是通的，TCP 被切

用 `nslookup` 直接查，Copilot 的域名都能正常解析：

```
api.githubcopilot.com                → 140.82.112.22
proxy.individual.githubcopilot.com   → 4.249.131.160
copilot-proxy.githubusercontent.com  → 4.249.131.160
githubcopilot.com                    → 140.82.112.17 / 18
```

但连不上：

```
curl https://www.baidu.com/          → 退出码 0（通）
curl https://api.githubcopilot.com/  → 退出码 28：Connection timed out after 20011 ms
```

所以是 TCP 层被切断，不是 DNS 污染。

### Watt Toolkit 两条模式都试了

**Hosts 模式（默认）**：它的加速列表里有 Github Dev / Api / Assets / Education / Resources /
Uploads / Archiveprogram / UserContent / 网站(Git Push) / App / Docker Hub / greasyfork / github.io，
**没有 GitHub Copilot 这一项**。没列到的域名照旧直连，照旧被墙。

**System 模式（系统代理）**：它会在 `HKCU\...\Internet Settings` 里设
`ProxyEnable=1`、`ProxyServer=127.0.0.1:26561`，本地代理确实在工作（走它访问百度
`HTTP 200 / 161 ms`）。但对 GitHub 和 Copilot 的域名，它的中继直接返回错误：

| 走 System 代理访问 | 结果 |
|---|---|
| `www.baidu.com` | HTTP 200，161 ms |
| `api.githubcopilot.com` | **HTTP 502，20210 ms** |
| `proxy.individual.githubcopilot.com` | **HTTP 502，20242 ms** |
| `copilot-proxy.githubusercontent.com` | HTTP 500，30 ms |
| `api.github.com` | **HTTP 502，101 ms** |
| `github.com` | **HTTP 502，20125 ms** |

502 是 Watt Toolkit 自己的中继回的（它接受了连接、转发出去、上游回报错误）。
**注意最后两行**：切到 System 模式后，连平时（时好时坏的）普通 GitHub 访问也变成 502 了。
所以在 Hosts 模式本来能用的东西，切到 System 反而更糟。真要试，试完记得切回去。

## 我测错过的两个地方

这两条比结论更值得记下来。

**第一，用 curl 测代理是错的。**
curl 用自己的证书库，不读 Windows 证书存储，所以不认代理的 MITM 证书，会直接报
`CRYPT_E_NO_REVOCATION_CHECK`，看起来像"代理坏了"，其实代理是好的。

VS 是 .NET 程序，读 Windows 证书存储。要测出 VS 的真实体验，得用 .NET：

```powershell
# 这才等于 VS 走的路
Invoke-WebRequest -Uri "https://api.githubcopilot.com/" `
                  -Proxy "http://127.0.0.1:26561" -TimeoutSec 30
```

**第二，超时别设太短。**
我一开始用 3 秒超时，把"慢"误判成"被挡"。换成 12 到 15 秒才看得清真实情况。
用手机热点时这些域名 300 毫秒就通，回到校园网就整段超时，这个对比才说明问题。

## 顺带测出来的：Copilot 每次请求有多重

Copilot 的 BYOM 路径一旦连通，请求的样子是这样的（代理日志实测）：

```
REQ stream=True msgs=4 chars=26103 tools=29
    [system:25493 user:179 assistant:234 user:197] max_tokens=None
```

| 项目 | 数值 |
|---|---|
| 系统提示 | 25,493 字符 |
| 工具定义 | 29 个 |
| 消息总字符 | 26,103 |
| Ollama 实际 prompt | **17,693 tokens** |

耗时拆解：

| 阶段 | 耗时 |
|---|---|
| prompt 处理（冷启动，无缓存） | 74.6 s |
| prompt 处理（热缓存，只重算 4,692 tokens） | 27.1 s |
| 生成 198 个 token | 11.7 s |
| **第一次请求合计** | **约 88 s** |
| **后续请求合计** | **约 38–48 s** |

这里有两个数容易被看漏。

**第一个，Ollama 的前缀缓存救了一半**：17,693 tokens 里 13,001 走缓存，只重算 4,692。
所以同一个 VS 会话里第一条最慢，后面会快一些。

**第二个，长上下文会把生成也拖慢**：这次生成速度只有 16.9 tok/s，而短上下文时是 65 tok/s。
17K 上下文意味着每生成一个字都要读一遍更长的 KV cache。所以"读题慢"和"写字慢"是两笔账，
只看 prompt 那一项会低估。

想再快，唯一的办法是关掉 BYOM 里的 Tool Calling（那 29 个工具定义大概占 4,700 tokens，
而且是每次都要重算的部分）。但关掉之后 Copilot 就不能自己读文件、改代码、跑构建了，
等于丢掉用它的主要理由。值不值，自己权衡。

## 对比：本地聊天客户端有多轻

同一个模型，换成一个只做问答的客户端（不带 Copilot 那套 agent 提示词和工具），
请求长这样：

```
REQ stream=True msgs=2 chars=240 tools=0 [system:227 user:13] max_tokens=8192
prompt eval time = 223.93 ms / 63 tokens
total time = 949.73 ms
```

系统提示 227 字符，prompt 63 个 token，模型算完不到 1 秒。

**这就是 17,693 和 63 的差别。** Copilot 慢不是模型的锅，是它每次要塞两万五千字符的
系统提示加 29 个工具定义。这个设计换什么模型都一样。

## 所以该怎么分工

| 场景 | 用什么 | 为什么 |
|---|---|---|
| 解释代码、问概念、写小段代码 | 本地模型 + 轻量客户端 | 一两秒出结果，不依赖外网，不耗流量 |
| 行内补全 | Copilot 或国内同类助手 | 这是高频低延迟场景，本地扩展做不到 |
| 多步改代码、跑构建验证、看编译错误 | Copilot | 只有它拿得到编译器、调试器和解决方案上下文 |
| 校园网下想用 AI 助手 | 国内厂商的编程助手 | 它们的服务器在国内，校园网直接可达 |
| 校园网下**一定要**用 Copilot | 手机热点，或者自备能转发任意流量的代理 | 目前没有第三条路 |

## 一个没验证的想法

Copilot 的面板在云连不上时**一个请求都不会发**（实测：整个白天 0 次 POST，只有偶尔一条
查模型列表）。但我不确定它是**每次请求**都要连云，还是**只在会话初始化时**需要。

如果是后者，有个省流量的办法值得试：先用手机热点启动 VS 并发出第一条消息（让会话建立起来），
然后切回校园网，继续用。这样热点只用在开头一分钟。

**这条我没验证过**，因为会话状态、token 刷新周期都可能让它在几分钟后失效。
想省流量的话可以自己试一下，判断标准很简单：切回校园网后再发一条，看代理日志里有没有新的
`REQ` 行。
