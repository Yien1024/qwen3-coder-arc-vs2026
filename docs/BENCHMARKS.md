# 实测数据和测法

README 里那些数字是怎么测出来的，以及哪些数字不能当规格用，都写在这里。
所有测试都在同一台机器上完成。

## 测试环境

| 项目 | 配置 |
|---|---|
| CPU | Intel i5-13500 |
| 推理 GPU | Intel Arc Pro B60 24G，不接显示器 |
| 显示 GPU | Intel Arc A750 8G |
| 内存 | 48 GB |
| 系统 | Windows 11 |
| Ollama | 0.35.1，官方版（下面所有数字都是这个版本上测的） |
| 模型 | `qwen3-coder:30b`，Qwen3-Coder-30B-A3B-Instruct，IQ4_NL，17.3 GB |
| 上下文 | `OLLAMA_CONTEXT_LENGTH=65536`，`OLLAMA_KV_CACHE_TYPE=q8_0` |
| 常驻 | `OLLAMA_KEEP_ALIVE=-1` |

这套配置下 `ollama ps` 报的是 20 GB / 100% GPU / CONTEXT 65536 / Forever，
也就是 17.3 GB 权重加上 64K 的 q8_0 KV cache。

## 速度

测法是直连 `127.0.0.1:11434/v1/chat/completions`，`stream=false`，固定 `max_tokens`，
用 `usage.completion_tokens` 除以墙钟耗时算 tok/s。冷热对比用连续多次相同请求。

| 场景 | 耗时 | prompt | completion | 等效速度 |
|---|---:|---:|---:|---:|
| 第 1 次（冷） | 12.17 s | 14 | 46 | 3.8 tok/s |
| 第 2 次 | 0.68 s | 14 | 45 | 66.3 tok/s |
| 第 3 次 | 0.81 s | 14 | 54 | 66.9 tok/s |
| 长文，238 tokens | 3.66 s | 14 | 238 | 65.0 tok/s |
| 长文，7758 tokens，一次写完整 CLI 程序 | 190.8 s | 22 | 7758 | 40.7 tok/s |

能看出来三件事。热态短请求稳定在 65 到 70 tok/s。每个"第一发"请求要付 10 到 12 秒，
即使 `KEEP_ALIVE=-1` 让权重常驻显存也一样（我怀疑是前缀缓存冷，加上测试期间加载过 4B
影响了驻留状态，但没定位到根因）。长文生成会掉到 40 tok/s 左右，
所以做长输出的时间预期要按 40 算，不是 70。

我最初记下来的 69.7 tok/s 生成、303.7 tok/s prompt 处理、46.2 秒首次加载，
和上表是同一量级，可以互相印证。

## 联网要花多少时间

测法是同一个问题分别走代理（联网）和加 `[no-search]` 走代理，对比墙钟耗时和 `prompt_tokens`。

| 项目 | 耗时 | prompt tokens |
|---|---:|---:|
| 搜索后端本身，Bing 抓取加解析 | 0.17–0.30 s | 无 |
| 端到端，联网 | 5.92 s | 1633 |
| 端到端，加 `[no-search]` | 3.00–3.11 s | 16 |

搜索后端不是瓶颈，它只花 0.2 到 0.3 秒。时间花在抓正文页和注入撑大的 prompt 上。
注入把 prompt 从 16 顶到 1600 多 tokens，这才是延迟的大头。
所以"少搜、搜准"比"搜得多"重要得多。

同一道题在改造前后对比是 14.11 秒变成 5.92 秒，而 `[no-search]` 的基线基本没动
（3.00 到 3.11 秒），说明省下来的是"搜索加抓取"那一段，不是模型变快了。

## 注入块有多大

测法是同一道题，`Python报错 ModuleNotFoundError: No module named requests 怎么解决`，
分别用旧的注入逻辑和新的注入逻辑拼出请求，直接打给模型，读 `usage.prompt_tokens`。

| 变体 | 耗时 | prompt tokens | 回答质量 |
|---|---:|---:|---|
| 不搜 | 6.73 s | 22 | 完整正确，模型本来就会 |
| 旧注入 | 9.47 s | 1721 | 正确，但末尾被诱导推荐了一篇《如何安装 Python》的 CSDN 教程 |
| 新注入 | 3.54 s | 421 | 结构化，带 `[S1]` 引用，干净 |

旧方案花了四倍的 prompt，换来一个错误推荐。另外这道题还说明"不搜也能答对"，
真正的优化方向是该搜才搜。

## 检索词抽取的效果

测法是同一时刻、同一引擎（`cn.bing.com`），只换检索词。

| 检索词 | 返回结果 |
|---|---|
| `Python报错 ModuleNotFoundError: No module named requests 怎么解决`，整句 | Python 官网、Python 下载页、菜鸟教程、CSDN 安装教程，0/5 相关 |
| `ModuleNotFoundError: No module named requests`，抽原文 | CSDN 专项解决、geek-blogs 解析、腾讯云已解决、CSDN 已安装仍报错、ask.csdn，5/5 正解 |
| `ModuleNotFoundError: No module named 'cv2'` | 3/3 全是 cv2 专项解决方法 |

边界也得说清楚。另外测了 6 条历史查询，4 条抽对就大幅改善，剩下 2 条
（`TypeError: NoneType...` 和 `ImportError: cannot import name getargspec from`）
检索词抽对了，Bing 依然返回泛化页或者完全无关页。所以抽原文是必要条件，不是充分条件，
后面必须有相关性闸门兜底。

顺便记一个反例：`site:stackoverflow.com` 这个操作符没用，
cn.bing 直接忽略它，返回的结果和不用时一模一样。

## 相关性闸门的 keep 率

测法是拿 14 条真实 query 各取 Bing 前 8 条，跑完整的排序加闸门逻辑，统计保留条数。
抓取步骤打了桩跳过，这里只测闸门，不测网络。

| 阈值设定 | ok | weak | empty |
|---|---:|---:|---:|
| 单一 `RELEVANCE_MIN=0.30`，第一版 | 9 | 0 | 5 |
| 加了时效放宽和永不滤空，当前版本 | 12 | 1 | 1 |

剩下那 1 条 empty 是 B站 查询，属于设计内。Bing 返回的 8 条确实是垃圾
（`apps.microsoft.com`、`symbolsdb.com/bitcoin-symbol` 之类），
但 B站 官方 API 的真实数据照常注入了。

## 检索块缓存的效果

测法是模拟 VS agent 模式的三轮往返，第 2 轮和第 3 轮带上 `assistant.tool_calls`
和 `role=tool` 的结果，数代理日志里的 `search query (extracted)` 和 `search cache HIT` 行数。

| 轮次 | 改造前 | 改造后 |
|---|---|---|
| turn 1，用户消息 | 11.5 s，1 次搜索 | 11.55 s，1 次搜索 |
| turn 2，工具结果回灌 | 约 11 s，1 次搜索 | 2.61 s，缓存命中 |
| turn 3，再来一轮 | 约 11 s，1 次搜索 | 3.06 s，缓存命中 |
| 合计 | 3 次搜索 | 1 次搜索加 2 次命中 |

turn 2 和 turn 3 的回答仍然引用了 `[S1]`，说明复用的缓存块照样注入了，质量没丢。

## prompt 越大越慢

| prompt tokens | 一次请求耗时 | 备注 |
|---:|---:|---|
| 约 9,381 | 18.4 s | 同一个 prompt 复测是 2.3 到 6.1 s，前缀缓存生效 |
| 38,131 | 248 s | |
| 64,948 | 694 s，将近 12 分钟 | |

这批数字不能当规格用。同一轮测试跑完之后，我复测那个 9K prompt，
耗时从 18.39 秒变成了 6.10 秒，然后变成 2.30 秒和 2.42 秒。
也就是说前面那种"随上下文长度单调下降"的观感里，混进了连续重载导致的降频，
以及前缀缓存这两个因素。

要一条干净的"耗时对 prompt 长度"曲线，需要冷机、随机顺序、每次之间冷却。
这个实验我没做。唯一可信的结论是量级：VS 里对话越长越慢，该开新会话就开。

## 上下文超限的真实行为

测法是用 `/api/chat` 显式指定一个小 `num_ctx`，发一个明显超出它的 prompt。

```
HTTP 400 {"error":{"code":400,
  "message":"request (9381 tokens) exceeds the available context size (4096 tokens), try increasing it",
  "type":"exceed_context_size_error","n_prompt_tokens":9381,"n_ctx":4096}}
```

38131 tokens 对 `num_ctx=32768` 也是同样的 `exceed_context_size_error`。

所以 Ollama 0.35.1 是报错，不是静默截断，不会悄悄丢数据。但长会话会直接失败。
这就是 README 里建议把 VS 的 Max Input Tokens 设成 61440 而不是 65536 的原因：
代理是在 VS 算完输入预算之后才追加 400 到 1600 tokens 检索块的，填满 65536 就会被顶出去。

## 模型本身

测法是直连 11434，发 OpenAI 风格的请求。

| 项目 | 结果 |
|---|---|
| 工具调用，2 个工具 | `finish_reason=tool_calls`，`arguments` 是正确的 JSON |
| 流式加 26 个工具 | `finish_reason=tool_calls`，`tool_call.index` 正确递增到 [0, 1] |
| 思考模式 | 没有。`ollama show` 报的是 `tools, completion`，没有 `thinking` |
| 多步推理，数学应用题 | 分步推理加回代验证，完全正确 |
| 长上下文捞针，16738 tokens，埋点在中段 | 精确命中，代号和人名都对 |
| 输出上限 | 一次写完整 CLI 用掉 7758 / 8192 tokens 后 `finish_reason=stop`，自然结束没被截断，余量只剩 5% |
| 4B 的思考陷阱 | `max_tokens=400` 时 `content` 为空、`reasoning` 有 620 字符；给到 2500 才出答案 |

## 几个容易骗人的地方

写这份文档的过程中被这几件事坑过，记下来：

凡"改前对改后"的对比，我都确保是同一台机器、同一天、同一个模型实例。
网络相关的对比全部在同一时刻用同一引擎，因为搜索结果本身会随时间漂移。

基线一定要一起测。只说"联网慢 X 秒"而不给不联网的数字，很容易把机器状态的变化算成联网开销。

前缀缓存会骗人。同一个 prompt 第二次跑会快好几倍，要测真实成本就用没跑过的 prompt。

热状态也会骗人。连续重载之后整机会降频，看到"性能随某个变量单调下降"时先怀疑这个。

单位别混。tok/s、秒、tokens、字符，这四个东西写在一起的时候要标清楚。
