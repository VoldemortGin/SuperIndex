# nav — 两级导航检索

面向「多级目录 + 上千文件」语料的检索层。参考 PageIndex 的思路，但把树的粒度
从「文档内部章节」扩展到「语料目录 → 文档 → 章节」三层。

**核心流程**

```
问题
 ├─ 第 1 级：在目录树里定位文件（一个或多个）
 └─ 第 2 级：在每个候选文件的章节树里定位章节
        ↓
      返回章节原文 + 来源路径
```

## 快速开始

```bash
PY="uv run python"   # 仓库内；pip 安装后直接用 python

# 1. 建结构索引 —— 免费、无 LLM，上千文件也是秒级
$PY -m superindex.nav.build /path/to/reports --out index/

# 2. 生成路由用摘要（文件 + 目录级）      [LLM]
$PY -m superindex.nav.build /path/to/reports --out index/ --summarize-files

# 3. 生成章节级摘要                        [LLM]
$PY -m superindex.nav.build /path/to/reports --out index/ --summarize-chapters

# 查询
$PY -m superindex.nav.route index/ "港湾人寿 2024 年全年的每股股息是多少？"
$PY -m superindex.nav.route index/ "..." --show-content      # 附章节原文
$PY -m superindex.nav.route index/ "..." --json              # 机器可读
```

> `--out` 可以是任意目录（CLI 单语料，例如上面的 `index/`）。`superindex nav-serve` 注册的语料
> 索引统一放在 `<store>/nav/corpora/<id>/`（默认 `superindex_store/nav/`，`SUPERINDEX_INDEX_DIR` 可改），
> 源目录保持只读。

## 模型

nav 自带一套模型取值，**不走** `superindex` 命令的 `--chat-model` / `--base-url` 那套配置：

| 项 | 取值顺序 |
|---|---|
| 模型 | `--model`（build / route 都有）> `NAV_MODEL` > `SUPERINDEX_CHAT_MODEL` > 兜底 `deepseek/deepseek-flash` |
| 推理强度 | `route --effort` > `NAV_REASONING_EFFORT` > 默认 `none`；`build` 没有 `--effort`，只读环境变量。设为空值则不发送该参数（网关 / Ollama 上的非推理模型必须这样设，否则 LiteLLM 报 `UnsupportedParamsError`） |

- **不读** `SUPERINDEX_BASE_URL` / `SUPERINDEX_API_KEY_OVERRIDE`。网关地址与 key 用 LiteLLM 自己的环境变量，
  如 `openai/<模型名>` 配 `OPENAI_API_BASE`（带 `/v1`）与 `OPENAI_API_KEY`；`.env` 同样会被读取。
- 没配任何模型时会用兜底的 `deepseek/deepseek-flash`（需要 `DEEPSEEK_API_KEY`），这与 `superindex` 命令"未配模型即报错"不同。
- 只有步骤 1（结构索引）不调用模型；`route` 每次查询都会调用模型。

步骤 2/3 是**增量**的：文件大小与 mtime 未变且已有对应摘要时直接跳过。
内容变了才会重建该文件的章节树（其旧章节摘要随之失效）。
新增文件自动挂进索引，删除的文件自动移除。

## 索引结构

单个语料的索引目录（CLI 的 `--out`，或 nav-serve 的 `<store>/nav/corpora/<id>`）内部：

```
<index-dir>/
├── manifest.json         目录树 + 文件元数据 + 摘要   ← 小，常驻内存
└── trees/<key>.json      每份文档的章节树             ← 大，按需加载
```

刻意拆成两份：**路由索引要小到能整棵读进 prompt，章节树只在少数文件上按需取**。

结构索引只解析目录与标题、不调用 LLM；`manifest.json` 只存目录树、文件元数据与摘要，
章节树留在 `trees/` 里按需读取，所以路由阶段的 prompt 只随目录数增长，而不随章节数增长。

## 数据模型

```json
// 目录节点
{ "rel_path": "港湾人寿/2024/annual", "name": "annual", "parent": "港湾人寿/2024",
  "child_dirs": [], "files": ["港湾人寿/2024/annual/HarbourLife_AR2024.md"],
  "summary": "港湾人寿 2024 年年报，含新业务价值、营运利润、股息...",
  "n_files": 1, "n_dirs": 0 }

// 文件节点
{ "rel_path": "港湾人寿/2024/annual/HarbourLife_AR2024.md", "name": "HarbourLife_AR2024.md",
  "parent": "港湾人寿/2024/annual", "ext": ".md", "size": 4096, "mtime": 1.7e9,
  "summary": "港湾人寿 2024 年全年股息及派息政策...",
  "n_chapters": 15, "max_depth": 3, "tree_key": "a1b2c3d4e5f6a7b8" }

// 章节节点
{ "title": "股息", "level": 3, "start": 11, "end": 21,
  "summary": "2024 年全年股息（中期 + 末期）及同比变化",
  "children": [] }
```

`start` / `end` 是源文本的**行号**（PDF 是抽取出的文本的行号；文本层路径下章节标题为 `Page N`）。取正文时按这个区间切片。

## 四个关键设计

### 1. 第 1 级读完整目录树，而不是逐层下钻

典型的报告语料按「公司 / 年份 / 报告类型」分目录，完整目录树通常塞得下一次 prompt。
一次调用让模型选目录，再一次选文件 —— **两次调用，与目录深度无关**。

只有在目录数超过 `DIR_TREE_BUDGET`（默认 240）时才退回逐层下钻。

### 2. 提示词强制「必须选择」

早期版本的提示词写了「都不相关时两个列表都留空」。结果模型**不可预测地**返回空
列表，在明明可回答的问题上直接中止导航。实测同一个问题连跑两次，
一次选中一次不选。

改成「**必须至少选 1 个**」之后稳定了。模型的默认倾向是保守，不要给它
「留空」这个出口 —— 真要表达不相关，交给下游的阈值判断。

### 3. 每一级都有确定性回退

模型仍可能返回空（尤其在没有摘要的索引上）。所以每一级都有回退：

| 位置 | 回退策略 |
|---|---|
| 目录选择 | 按问题里的**期间**（权重 3）+ 中英文词元匹配目录路径 + 策略权重 |
| 文件选择 | 同上，匹配文件路径 |
| 逐层下钻中的目录 | 优先回退到**本层文件**，其次才是子目录 |
| 章节选择 | 词元匹配标题（权重 1）+ 摘要（权重 0.5） |

回退优先在本层解决 —— 回退到全语料是很粗的手段，语料越大越糟。

**期间是最强的路由信号。** 财务问答的跨期混淆是最大的错误来源，
而目录路径里通常带年份，所以抽出的期间同时用于提示词和回退打分。
`year_hints()` 是内置的 4 位年份提取器；`RoutingPolicy.periods_in()` 在它
之上补业务写法（`FY24`、`2024H1`、`Q3`），空策略时两者完全等价。

回退是**唯一**能让策略真正改变结果的地方（模型在场时策略只是「更倾向」），
所以别名扩展和目录权重都接在这里 —— 这里的排序错了就没有第二道防线。

### 4. 业务知识外置到 `policy.py`

上面三条是「面对未知语料」的合理默认，但在企业内部，语料并不是未知的 ——
有人知道法定年报在 `annual/` 下、`_drafts/` 永远不该被搜、"太保" 和 "中国太保"
是同一家公司。这部分知识属于业务方，所以放在
[`config/routing_policy.yaml`](../../config/routing_policy.yaml)，改它不用碰
Python，每次提问重新读，不用重启。查找顺序：`SUPERINDEX_ROUTING_POLICY` →
工作目录 `config/routing_policy.yaml` → exe 所在目录 `config/` → 随代码发布的那份
（源码仓库根 / PyInstaller 包内）。

`RoutingPolicy` 在四个位置注入，每个位置对应 route.py 里的一个假设：

| 注入点 | 原本的假设 | 策略提供 |
|---|---|---|
| `periods_in()` | 报告期就是 4 位年份 | `periods.patterns` —— `FY24` 归一成 `2024`，好和目录对上 |
| `weight_for()` | 每个目录同等值得看 | `directories.weights` —— 加分，并在提示词里标注 |
| `prompt_block()` | 只有问题本身的词 | `directories.scopes` / `instructions` —— 纯提示 |
| `is_excluded()` / `alias_terms()` | 问题里有什么词就用什么 | `directories.exclude` 硬过滤、`aliases` 同义词扩展 |

**两条设计红线**

1. **空策略 = 内置行为。** `RoutingPolicy()` 对所有方法都返回和改造前一样的
   结果。删掉配置文件，路由行为和这个功能不存在时完全一致 ——
   `tests/test_policy.py` 逐条断言了这一点，因为这是这个功能敢上线的唯一理由。
2. **配置排序，不设闸门。** 权重和业务域只是让模型更倾向于某些目录；
   只有 `exclude` 真的把目录从候选里拿掉，因为 `_drafts/` 被搜到永远不是好事。
   而且 `exclude` 按**路径整段**匹配（不是子串），所以 `exclude: [draft]`
   不会误伤 `drafting-guidelines/`。

**单语料覆盖**：`corpora:` 段按语料**显示名**写（配置文件是给人看的），
`MultiNavigator` 构造时一次性绑定到内部 id。覆盖项与全局**合并**而不是替换 ——
否则每个语料都要把整份策略抄一遍，正是全局层要避免的漂移。

**坏配置不致命**：YAML 写错会抛 `PolicyError`，由 `superindex.nav.route._bind_policy`
接住、退回默认行为、打一行 stderr，并记进 `results/logs/errors.jsonl`
（`where: policy.load`）。服务器不会因此起不来。

```bash
$PY -m superindex.nav.route index/ "..." --show-policy        # 看当前生效的策略
$PY -m superindex.nav.route index/ "..." --policy /path/x.yaml --corpus 中国太保
SUPERINDEX_ROUTING_POLICY=/path/x.yaml $PY -m superindex.nav.route ...   # 换文件位置
```

### 5. 示例问题生成 `suggest.py`

输入框上方那排 chips 不是手写的 —— 手写示例只对写它的那份语料成立，换一份语料
就是在教用户问一个索引答不上来的问题，看起来像**检索失败**，其实是**示例失败**。
所以示例问题从语料自身推导，用的是路由已经在看的同一批证据：语料描述、目录主题、
文件摘要、章节标题。**章节标题是最强的信号** —— 「股息」「新业务价值」「分市场表现」
字面上就是可以被提问的东西。

- 每个语料一次调用，结果缓存在注册表里，和语料摘要共用同一个内容指纹做失效判断。
- **任何失败都返回空列表**，UI 直接不显示 chips。宁可没有示例，也不要一个错的示例：
  错的会浪费用户的第一次点击，让索引显得比实际更差。
- 生成逻辑只写临时目录的测试见 `tests/test_suggest.py`（75 断言）。

## 如何验证

本目录不附带实测数字：路由效果取决于所用模型、摘要质量与语料目录结构，
换一套条件数字就不成立。自己的语料上可以这样核对：

1. 建结构索引（无 LLM）后，用 `--json` 跑一组带年份、答案已知的问题，
   检查每个问题命中的目录 / 文件 / 章节是否是预期路径。
2. 生成文件级与章节级摘要后再跑同一组问题，对比命中路径的变化，
   重点看跨年份、跨报告类型（年报 / 中期报告）的问题是否仍选对年份。
3. 去掉摘要或让模型返回空列表，确认回退路径（年份 + 词元匹配）仍能定位到
   合理的目录与文件。

## 延迟：思考必须转发，不能丢

一次问答的耗时分布（21 条真实记录统计）：**答案生成 17.9s（77%）**、
路由 4.2s、章节选择 1.2s，端到端平均 23.3s、最慢 114s。

瓶颈不在检索也不在上下文 —— 最慢那次只用了 3176 字符的上下文，却花了 93 秒。
瓶颈是**模型的思考**。实测 `deepseek-flash` 的流式返回：

```
269 个 chunk 中：reasoning_content 占 240 个，content 只有 27 个
```

而原来的 `chat_stream()` 只读 `delta.content`，**把思考全部丢掉** ——
于是用户对着空白气泡等十几秒，其中大部分时间模型其实正在吐字。

`reasoning_effort` 不是可用的刹车：同一提示词下把它整个关掉，
思考量只从 804 字降到 722 字。**既然缩短不了，就该让它可见。**

- `superindex/nav/llm.py::chat_stream_events()` 产出 `("reasoning", text)` / `("content", text)`，
  供 UI 分流；`chat_stream()` / `chat_stream_text()` 语义不变（仍产出纯字符串）。
- 只有思考、没有正文也算「没到」，会走非流式回退（预算翻倍）——
  否则会流一大段思考然后静默给出空答案。
- 每次查询把 `thinking_chars` 写进 `queries.jsonl`：
  「这次为什么慢」因此有确定答案 —— 该值大 = 模型在思考上打转，
  而不是检索慢或上下文大。

另外，`run()` 里逐文件的章节选择是**并行**的（`ThreadPoolExecutor` + `map` 保序）。
保序是硬要求：`build_context` 按相关性顺序截断，顺序错了会让边缘章节挤掉最好的来源。
线程安全的前提是 `_load_tree()` 只读 JSON 文件、`policy` 是 frozen、`m` 加载后只读。

## 多语料：`registry.py`

上面的 CLI 是一次性建索引 + 查询。要让 UI 驱动（注册目录、看状态、自动跟进
文件变化），用 `superindex/nav/registry.py`；`superindex nav-serve` 就是在它之上的 Web UI。

```python
from superindex.nav.registry import Registry

reg = Registry()                     # 索引集中存 <store>/nav/（默认 superindex_store/nav/）
c = reg.add("/data/reports", name="年报库")        # 每个语料一个独立索引目录
reg.index_async(c.id)                             # 后台建索引，状态可轮询
reg.start_watcher(interval=30)                    # 轮询文件变化并自动增量重建

nav = reg.navigator([c.id])                       # 或多语料：reg.navigator()
res = nav.run("2024 年全年股息是多少？")
```

**投放区**：`sync_data_root()` 会把投放区下每个直接子目录自动注册并索引，
所以「往投放区丢一个目录」就是完整的工作流。watcher 每轮也会调用它，
新目录自动进来。投放区默认是源码仓库里的 `samples/test_corpus/`（pip / 打包环境下不存在则不发现任何东西），
用 `SUPERINDEX_DATA_DIR` 指向自己的目录。

**多语料查询**走 `MultiNavigator`：把 N 个语料的 manifest 合并成一棵路由树，
每个语料成为顶层的一个伪目录。这样第 1 级仍然是**一次调用**，而且模型能跨语料
比较分支，而不是分别路由再猜哪个结果更好。

```python
from superindex.nav.route import MultiNavigator, build_context, answer_prompt
nav = MultiNavigator([("id1", "/path/idx1", "语料一", "摘要")])
res = nav.run(question)
context, sources = build_context(res, nav)        # 按相关性截断，预算内取满
```

### 设计要点

| | |
|---|---|
| **一个语料一个索引** | 互不污染；某个语料失败不影响其他；删除就是删目录 |
| **增量重建** | `scan(previous=...)` 跳过 size+mtime 未变的文件，**不重新抽取**。否则 watcher 每次轮询都会把每个 PDF 重发给 Azure DI |
| **只补缺失的摘要** | 有描述的跳过，所以改一个文件只花一个文件的摘要钱 |
| **沿用语料自身设置** | 注册时关掉文件描述，watcher 就不会偷偷开始调 LLM |
| **拒绝项目内目录** | 项目代码树内、投放区之外的目录不允许注册，避免把代码/索引自身当语料 |
| **索引与源文件分离** | 注册 `/data/reports` 只往 `<store>/nav/corpora/<id>/` 写，源目录只读 |
| **目录消失 → error** | 带可读错误信息，而不是静默返回空结果 |

## 调试日志：`debuglog.py`

每次提问都会记一条结构化记录，回答错了能事后查：

```python
from superindex.nav.debuglog import QueryTrace
t = QueryTrace(question, scope_names, model=...)
t.route_step(level="dir", where=..., picked=[...])
t.sources([...])
t.finish(answer)          # 正常
t.abort("no files located")   # 跑完但没找到（只写 queries）
t.fail(exc, stage="ask")      # 抛异常（queries + errors，id 关联）
```

写到 `results/logs/queries.jsonl` 与 `errors.jsonl`（`SUPERINDEX_LOG_DIR` 可改），用
`uv run python scripts/07_logs.py --id <id>` 还原单条全过程。
日志写入失败只打一行 stderr，**绝不影响主流程**。

## 已知限制

- **章节级摘要需要 LLM**，上千文件的语料是一次性成本。没有摘要时章节定位
  明显变弱（回退到词元匹配）。
- **PDF 的章节树取决于抽取器**，顺序：Azure DI → 文本层 + `superindex.engine.flash` → 每页一节点。
  配置了 Azure Document Intelligence（`AZURE_DI_ENDPOINT` + `AZURE_DI_KEY`）时得到带标题的章节树；
  否则读 PDF 文本层，用离线的 flash 引擎从字号/位置/排版统计推导标题（不用 LLM、不联网）；
  flash 不可用或失败时每页一个节点（标题 `Page N`），粗但每页都可达。PyInstaller 打包版不含 flash，直接走每页一节点。
  `--extractor {auto,azure-di,text-layer}` 可强制指定（默认 `auto`）。扫描件没有文本层，只能走 Azure DI。
- **目录结构本身的质量决定上限**。如果语料是一坨平铺的几千个文件（没有子目录），
  第 1 级的目录树退化成一次列几千个文件名 —— 这时应先做一层目录治理，
  或改用 `_descend_dirs` 的批处理策略。
- **回退用词元匹配**，对同义词本身无能为力（问「寿险」不会命中「人身险」）。
  能枚举出来的同义词用 `aliases:` 解决；要泛化就得把回退换成 embedding 预筛，
  但那就把相似度问题引进来了 —— 权衡后当前选择保持确定性。
- **策略只是「更倾向」，不是「会推理」**。权重和业务域是在模型已经看到候选之后
  施加的偏向，救不回答案本身就不存在的问题；而写错的 `exclude` 会真的藏掉目录 ——
  所以排除按路径整段匹配（不是子串），`exclude: [draft]` 不会误伤
  `drafting-guidelines/`。
