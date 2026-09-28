# 交接文档 — PageIndex × AIA 财报检索项目

> 面向接手同事。读完这份文档 + `README.md` + `nav/README.md`，
> 应该能独立跑起来、看懂每个设计决策的理由、并知道坑在哪。

**代码仓库**：https://github.com/yongsoft/SuperIndex （私有，需授权访问）

```bash
git clone https://github.com/yongsoft/SuperIndex.git
cd SuperIndex
bash data/aia_reports/download.sh     # 唯一还需要单独获取的东西（10 份 PDF）
```

> `PageIndex/` **已随仓库分发**（vendored，锁定 upstream commit `71714e8`），
> 所以一次 clone 就能跑，不需要 `--recursive` 也不需要手动 clone 上游。
> 仓库里**不包含** `.env`（含密钥）、`data/*.pdf`（27MB）、`index/` 与 `results/`
> 下的生成物。
> `PageIndex/` 里被剥掉了 examples/assets/cookbook/tests（约 56MB），
> 详见 `PageIndex/UPSTREAM.md`。

---

## 一、这个项目是什么

一个**验证「结构导航式检索」是否优于向量检索**的本地实验项目，以及从中长出的两个产物：

1. **`webapp/`** — 一个能用的 AIA 财报问答界面（流式 + 可追溯检索过程）
2. **`nav/`** — 两级导航检索包（多级目录 → 文档 → 章节），面向「上千文件 + 多级目录」的语料

最初的动机：验证 PageIndex（无向量库的树检索）在财报场景下相对向量 RAG 的表现。
过程中把结论扩展到了 Dify 知识库的改造方案（`docs/dify-improvement-plan.md`）。

---

## 二、当前状态

### 能跑通的

| 能力 | 状态 | 位置 |
|---|---|---|
| PDF 建树 + 索引（PageIndex） | ✅ | `scripts/01_build_trees.py`、`02_qa_test.py` |
| 财报问答（命令行） | ✅ | `scripts/02_qa_test.py` |
| 目录驱动的 Web 界面（加目录/看状态/按目录提问） | ✅ | `webapp/server.py` + `nav/registry.py`（端口 8787） |
| 目录变化自动监控 + 增量重建 | ✅ | `nav/registry.py` 的 watcher |
| 两级导航检索（目录 → 文档 → 章节） | ✅ | `nav/` |
| PDF → Markdown（Azure Document Intelligence） | ⚠️ 逻辑已验证，**未用真实凭据跑过** | `extractors/azure_di.py`、`scripts/06_azure_extract.py` |
| 查询延迟诊断 | ✅ | `scripts/profile_query.py` |
| Markdown 格式审计 | ✅ | `scripts/04_md_audit.py` |
| 跨文档重复度诊断 | ✅ | `scripts/05_similarity_probe.py` |

### 未完成的

- **10 份 AIA 报告只索引了 3 份**（1H2021 / FY2021 / FY2022），
  其余 7 份未索引。原因见第五节「为什么中途停了」。
- **Azure DI 抽取器没有用真实 Azure 凭据端到端验证过** —— 它是「配了就默认生效」
  的路径（见 5.6），离线逻辑有 53 条断言覆盖（下载样例 PDF 后为 55 条），但真实调用需接手人配 key 后跑一次
  `--check --only FY2021`。
- **`nav/` 只用合成语料验证过**，未在真实公司语料上跑过。
- **Dify 方案只有设计文档，未落地**。
- **没有评测集**（这是最大的缺口，见第八节）。

---

## 三、环境搭建（照着做就能跑）

### 3.1 Python 环境

用项目内的虚拟环境，Python 3.10+：

```bash
cd SuperIndex
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # 含 PageIndex 的 pinned 依赖
pip install -e PageIndex --no-deps
```

后续命令里的 `$PY` 都指这个解释器：

```bash
PY=.venv/bin/python              # 激活 venv 后直接用 python 也可以
```

> 为什么分两步装：直接装 `PageIndex/pyproject.toml` 会让 pip 在未 pin 的
> `litellm` / `openai-agents` 上疯狂回溯；`requirements.txt` 里引用的
> `PageIndex/requirements.txt` 是 pin 过的，几秒解完，再用 `--no-deps`
> 把包本身装进去。

### 3.2 PageIndex 的安装方式（重要）

`PageIndex/` 是**上游引擎的 vendored 副本**，不是本项目代码：

```
来源:   https://github.com/VectifyAI/PageIndex.git
commit: 71714e8   （已锁定，随仓库分发）
许可:   MIT — Copyright (c) 2025 Vectify AI
状态:   未做任何本地修改
```

⚠️ 完整说明（剥掉了什么、怎么更新到新版本）见 **`PageIndex/UPSTREAM.md`**。

它是**以 editable 模式装进 venv** 的（`pip install -e PageIndex`），
所以改 `PageIndex/` 里的代码会立即生效。**但请不要改它** ——
我们所有定制都通过 monkeypatch 或外层脚本完成，保持与上游的 diff 干净。

两处运行时定制（都不碰源码，更新上游也不会丢）：

| 定制 | 位置 | 原因 |
|---|---|---|
| `SUMMARY_CONCURRENCY` 64 → 8 | `scripts/02_qa_test.py` 的 `apply_concurrency` | 64 路并发会触发 DeepSeek 的假性余额拒绝 |
| `reasoning_effort` 逐次传参 | `webapp/server.py`、`nav/llm.py` | 不写进引擎 |

⚠️ venv 的 `.pth` 里存的是**绝对路径**，所以新机器上 clone 之后**必须重装**：

```bash
$PY -m pip install -r PageIndex/requirements.txt
$PY -m pip install -e PageIndex --no-deps
```

验证安装指向（`pip show` 的 `Location` 显示 site-packages 是正常的，
editable 安装靠 `.pth` 重定向）：

```bash
cat .venv/lib/python3.*/site-packages/*pageindex*.pth
# 应输出当前仓库里 PageIndex/ 的绝对路径
```

⚠️ 这个 `.pth` 里是**绝对路径**。换机器或换目录后需要重装：

```bash
$PY -m pip install -e ./PageIndex
```

### 3.3 依赖

venv 里已装好。关键版本：

```
pageindex      0.2.10  (editable，指向 ./PageIndex)
litellm        1.97.0
openai-agents  0.20.0
pypdfium2      5.13.0
PyPDF2         3.0.1
fastembed      0.8.0   ← 后加的，只用于 05_similarity_probe.py
onnxruntime    1.30.0  ← fastembed 的依赖
numpy          2.5.3
python-dotenv  1.2.2
```

### 3.4 模型配置

```bash
cp .env.example .env    # 然后填入 key
```

当前 `.env` 配的是 **DeepSeek**：

```
DEEPSEEK_API_KEY=sk-...            ← ⚠️ 这是真实 key，见第十节
PAGEINDEX_INDEX_MODEL=deepseek/deepseek-flash
PAGEINDEX_CHAT_MODEL=deepseek/deepseek-flash
```

**LiteLLM 的模型名必须是 `deepseek/deepseek-flash`。**
用 `openai/deepseek-flash` + `api_base` 会报 `Missing credentials`，别试。

### 3.5 网络（容易卡住的地方）

| 服务 | 直连 | 说明 |
|---|---|---|
| DeepSeek API | ✅ 可直连 | 无需代理 |
| `huggingface.co` | ❌ **超时** | 用镜像：`export HF_ENDPOINT=https://hf-mirror.com` |

只有 `05_similarity_probe.py` 需要 HF（下载 embedding 模型）。
首次运行前记得设镜像，否则会卡在超时。

---

## 四、代码地图

```
.
├── PageIndex/                 上游引擎 vendored 副本，editable 安装，勿改
│   └── UPSTREAM.md            来源/commit/剥掉了什么/怎么更新 ★ 必读
├── data/                      ★ 默认文档根目录（**投放区**，只读）
│   ├── aia_reports/             10 份 AIA 报告 PDF（27 MB，download.sh 拉取）
│   └── <任意子目录>/            ★ 每个直接子目录自动成为语料并索引
├── README.md                  项目主文档
│
├── scripts/                   实验与诊断脚本
│   ├── 01_build_trees.py      ★ 阶段1：离线建树（无 LLM，免费）
│   ├── 02_qa_test.py          ★ 阶段2：建索引 + 问答测试（要 LLM）
│   ├── extract_text.py          ground truth 抽取（pypdfium2 直读文本层）
│   ├── profile_query.py        查询延迟诊断（抓 token 用量）
│   ├── 03_md_tree_probe.py     markdown 建树探测
│   ├── 04_md_audit.py          markdown 语料结构审计（不跑 LLM 就能预测索引质量）
│   ├── 05_similarity_probe.py  跨文档重复度 + 检索区分度诊断
│   ├── 06_azure_extract.py     ★ PDF → Markdown（Azure Document Intelligence）
│   ├── monitor.sh              长任务的进度记录
│   ├── questions.json          20 题，覆盖全部 10 份
│   └── questions_3docs.json    18 题，只覆盖已索引的 3 份
│   └── ⚠️ 01_build_index.py / 02_qa.py 是早期版本，已被上面两个取代，可删
│
├── extractors/                ★ 文档抽取后端
│   ├── backend.py             ★ 后端解析：配了 Azure 就默认用它（并接管 PageIndex）
│   └── azure_di.py            Azure Document Intelligence 客户端（纯 REST，无 SDK 依赖）
│
├── tests/
│   ├── test_azure_di.py       azure_di 的离线测试（28 断言，不联网）
│   ├── test_backend.py        后端解析的离线测试（25 断言，不联网）
│   ├── test_registry.py       语料注册表/watcher 的离线测试（135 断言，不联网）
│   ├── test_llm_retry.py      LLM 预算升级、流式回退与每次调用埋点（78 断言，stub 掉 litellm）
│   ├── test_debuglog.py       日志记录/过滤/轮转（44 断言，写临时目录）
│   ├── test_policy.py         路由策略（135 断言，stub 掉 LLM，只写临时目录）
│   └── test_suggest.py        示例问题生成/缓存（75 断言，stub 掉 LLM，只写临时目录）
│
├── config/                    ★ 业务配置（唯一需要业务方改的地方）
│   └── routing_policy.yaml    路由策略：目录权重/排除/业务域/期间/别名
│
├── webapp/                    ★ 目录驱动的 Web 界面
│   ├── server.py              标准库 http.server + SSE，端口 8787
│   └── static/index.html      单文件前端，原生 JS
│
├── nav/                       ★ 两级导航检索包（本次主要产出）
│   ├── README.md              包文档，务必读
│   ├── llm.py                 LLM 调用封装（JSON 提取+修复+重试 / 流式）
│   ├── store.py               数据模型与持久化
│   ├── registry.py            ★ 语料注册表：多目录、后台索引、变化监控
│   ├── debuglog.py            ★ 结构化调试日志（queries.jsonl / errors.jsonl）
│   ├── policy.py              ★ 业务路由策略（目录权重/排除/业务域/别名）
│   ├── suggest.py             ★ 语料示例问题生成（输入框上方的 chips）
│   ├── build.py               CLI：建索引
│   └── route.py               CLI：两级导航查询
│
├── docs/
│   ├── HANDOVER.md                本文档
│   ├── ArchitectureIntro.html     ★ 系统架构说明（5 张内联 SVG，自包含，浏览器直接打开）
│   └── dify-improvement-plan.md   Dify 知识库改造方案（设计文档）
│
├── samples/                   测试素材
│   ├── aia_ar2021_excerpt.md      规范的 markdown 样例
│   ├── bad_no_headings.md         无标题的坏样例
│   ├── bad_ppt_derived.md         PPT 转出的坏样例
│   └── test_index/                已建好的 nav 索引（带摘要，可直接查）
│
├── index/                     ★ 产品索引库（构建产物，gitignore）
│   ├── README.md                  布局说明（**提交**）
│   ├── registry.json              语料注册表：源路径 + 状态 + 统计
│   └── corpora/<id>/              UI 注册的语料，一个语料一个目录
│       ├── manifest.json            目录树 + 文件描述 + 检索别名 topic
│       └── trees/<key>.json         章节树 + 正文行
│
└── results/                   实验产物（日志与问答结果）
    ├── logs/                      各种运行日志（webapp.log 144KB，可清）
    └── qa_results*.json           问答结果
```

---

## 五、我们做的变更（重点）

### 5.1 环境与配置

| 变更 | 说明 |
|---|---|
| 克隆并 editable 安装 PageIndex | commit `71714e8`，保持上游干净 |
| 新增 `.env` / `.env.example` | DeepSeek 配置 + `PAGEINDEX_REASONING_EFFORT` 说明 |
| 新增依赖 `fastembed` + `onnxruntime` | 仅用于 `05_similarity_probe.py` |

### 5.2 脚本（新增）

| 文件 | 作用 |
|---|---|
| `01_build_trees.py` | 阶段 1：离线建树，`summary=False, optimize=False`，零 LLM 调用 |
| `02_qa_test.py` | 阶段 2：建索引 + 跑问答。**本次改动最多** |
| `extract_text.py` | ground truth 抽取，独立于 PageIndex（避免自证） |
| `profile_query.py` | 抓 litellm 每次调用的 prompt/cached/completion token 与耗时 |
| `03_md_tree_probe.py` | 给一个 md，预测 PageIndex 会建出什么树 |
| `04_md_audit.py` | 批量审计 md 语料的结构健康度并分流 |
| `05_similarity_probe.py` | 量化跨文档重复率 + 检索区分度 |

### 5.3 `scripts/02_qa_test.py` 的具体改动

1. **新增 `--questions`** — 可切换题集（`questions.json` / `questions_3docs.json`）
2. **新增 `--concurrency`**（默认 8）— 猴补丁 `pageindex.utils.SUMMARY_CONCURRENCY`
3. **新增 `--docs`** — 只索引匹配的文件名
4. **索引改为逐份重试 3 次 + 退避（30s/60s），失败则跳过继续**
   —— 原来单份文档失败会整轮崩溃
5. **日志加 `flush=True`**，配合 `python -u` 实时观察
6. **修复 `--out` 传绝对路径时的崩溃** — 结尾的
   `out_file.relative_to(ROOT)` 在路径不在项目内时抛 `ValueError`，
   已改成 try/except 兜底。**注意：`--out` 建议用项目内相对路径**

### 5.4 `webapp/` —— 目录驱动的 Web 界面（已重写）

**注意：这一版把范围模型从「已索引的 PDF 文档」换成了「注册的目录」。**
原 PageIndex 文档流仍可通过 `scripts/02_qa_test.py` 命令行使用。

三个关键点：

1. **零额外依赖** —— 标准库 `http.server` + `ThreadingHTTPServer`，
   没有 FastAPI/uvicorn，前端是一个 HTML 文件、无构建步骤。端口 **8787**
2. **提问走 `nav/` 而不是 PageIndexClient** —— 因为功能核心是「目录」。
   服务端用 `nav` 的公开方法编排（`find_files` → `find_sections`），
   每完成一步就发一个 SSE 事件，所以 UI 能实时显示模型在收窄范围
3. **`reasoning_effort` 可配** —— 见下

API 一览（全部可脚本化）：

| | |
|---|---|
| `GET /api/state` | 语料列表、状态、watcher 状态、模型、示例问题 |
| `GET /api/browse?path=` | 列子目录（**只读、只给名字，不读文件内容**） |
| `GET /api/logs?kind=queries\|errors` | 结构化日志（见 5.x） |
| `GET /api/recent-questions` | 去重后的历史问题，给输入框聚焦下拉用 |
| `GET /api/corpora/<id>/tree` | 语料目录树（UI 展开卡片时拉取） |
| `POST /api/corpora` | 注册目录并开始后台索引 |
| `PATCH /api/corpora/<id>` | 改名 |
| `DELETE /api/corpora/<id>` | 移除并删除索引 |
| `POST /api/corpora/<id>/reindex` | 强制重建（`force` / `deep_index`） |
| `POST /api/ask` | SSE：`policy` / `stage` / `nav` / `sources` / **`thinking`** / `answer` / `error` / `done` |

`thinking` 与 `answer` 分开两个事件：推理模型绝大部分输出是思考
（实测 `deepseek-flash` 269 个 chunk 里 240 个是 `reasoning_content`、只有 27 个是正文），
丢掉它只会让用户对着空白等。

**前端把检索路径和思考过程放在同一个框里**（`.trace` 内：`.steps` → 虚线 → `.think` 分区）。
它们是同一次「模型怎么找到、又想出了什么」的前后两段，拆成两个折叠框只是逼用户开两次。
思考区默认 `hidden`，首个 `thinking` 事件才显示；首个 `answer` 事件时**整框自动收起**，
summary 留下总结句 `检索过程 · 思考 N 字`。`.trace.live` 让 summary 的红点脉动，表示还在跑。

**这个框的高度是写死的**（`.trace-body`，`clamp(210px, 30vh, 300px)`），`.steps` 与
`.think` 各自内部滚动。原因：高度一旦跟着内容走，每来一条 `nav` 事件、思考区由隐藏变
显示，整页就被往下顶一次 —— 答案还在流式输出，页面却一直在动，读不下去。高度恒定 =
零位移；思考区没出现时 `.steps` 自动占满，不留空白。

### 5.4.1 界面上的「零位移」约定

用户明确提过「页面老是跳动」。约定：**凡是会在流式过程中变化的内容，都不能改变它
所在的布局高度。** 目前三处：

| 位置 | 会变的东西 | 处理 |
|---|---|---|
| `.trace-body` | 检索步骤逐条增加、思考区出现 | 高度写死，内部各自 `overflow-y:auto` |
| `.srcs` 引用来源 | 来源陆续到达 | 按文档合并，行数由文档数决定（见下） |
| 侧栏 `.card` | 操作按钮 | 由 `:hover` 展开整行改为**常驻图标按钮**放进首行 |

侧栏那条是重点：原先是 `.card:hover .acts{display:flex}` —— hover 时插入一整行，
把下方所有卡片一起顶下去，手一抖就点错。改成三个常驻图标按钮后删掉 `display:none`
与 `:hover` 规则，卡片高度恒定。**图标用内联 SVG 而不是 `✎` 这类字符**：字符的实际
字形由系统字体决定，粗细参差（`✎` 明显比 `↻`/`✕` 细一截），换个系统还可能缺字。

**引用来源按文档合并**：一份文档常被命中多个章节，逐条列会把
「中国平安 / 2025/annual/PingAn_AR2025.md」这样的长路径重复十几遍。`srcHTML()`
按 `display` 分组，一份文档一行，章节并排成小标签，路径只留「语料 / 文件名」
（中间目录层级进 `title`），单文档超过 6 个章节折成 `+N`。

顺带修掉一个类名撞车：侧栏卡片的索引进度和对话区的状态行都叫 `.stage`，
互相覆盖，导致侧栏进度文案的琥珀色被盖成灰色、还凭空多出 7px 外边距。
侧栏那个改名 `.cstage`。

**取消语义**：客户端中途断开（关标签页 / 点「停止」）**不算错误** ——
`emit()` 写失败抛 `ClientGone`，`_ask` 捕获后走 `trace.abort("client disconnected")`，
只写 `queries.jsonl` 一条，不写 `errors.jsonl`。
（此前断开会被当成异常落到 `trace.fail()`，于是同一次查询在日志里留下
`finish` 和 `fail` 两条同 id 记录，把错误日志撑得比实际难看。）
断开后 litellm 的流随生成器回收而关闭，请求随之停止。

```python
REASONING_EFFORT = os.getenv("PAGEINDEX_REASONING_EFFORT", "low").strip() or None
...
stream = client.chat(question, doc_id=scope, stream=True, reasoning_effort=REASONING_EFFORT)
```

### 5.5 `nav/` 包（本次主要产出）

两级导航检索。**设计细节见 `nav/README.md`**，这里只列要点：

- 索引拆两份：`manifest.json`（小，常驻）+ `trees/<key>.json`（大，按需）
- 第 1 级读**完整目录树**（目录数远小于文件数），两次调用搞定，与深度无关
- 提示词**强制「至少选 1 个」**（原因见第六节）
- 每一级都有**确定性回退**（年份权重最高）
- 增量更新：mtime + size 未变则跳过

### 5.6 `extractors/` + `scripts/06_azure_extract.py`（新增）

用 **Azure AI Document Intelligence** 把 PDF 转成 Markdown。

**★ 关键行为：配了就是默认。** `.env` 里同时设好 `AZURE_DI_ENDPOINT` 和
`AZURE_DI_KEY` 之后，**所有入口自动使用 Azure DI**，不需要任何命令行参数：

| 入口 | 接管方式 |
|---|---|
| `scripts/02_qa_test.py` | 猴补丁 `pageindex.local_api.LocalAPI._extract_page_texts` |
| `nav/build.py` | `read_document()` 里对 PDF 走 `extractor.document_text()` |

没配则完全退回原来的文本层路径，行为不变。两个入口启动时都会打印
`提取后端: azure-di` 或 `提取后端: text-layer` 并说明原因 —— **绝不静默切换**。

可以用 `--extractor {auto,azure-di,text-layer}` 强制指定，用于对比两种后端的
实际差异。

**失败策略**：配了 Azure 但调用失败时**直接中止**，不静默退回文本层
（否则 key 写错会在无人察觉的情况下改变索引质量）。设 `AZURE_DI_FALLBACK=1`
可开启软退回。`nav.build` 会给出干净的中止提示而非 traceback。

它解决的是第七节里那两个默认路径的硬伤：

| | 默认（PyPDF2 读文本层） | Azure DI |
|---|---|---|
| 表格 | **糊掉** —— 图表页抽成 `175230`，两个数粘一起，标签与数值的关联丢失 | 真正的 Markdown 表格 |
| 扫描件 / 纯图片 PDF | **直接拒绝**（无文本层，无 OCR） | OCR 识别 |
| 正文 | 好 | 好 |
| 页码锚点 | 原生（页码区间） | 注入 `<!-- page: N -->` |

**两条路都落到「带 `#` 标题的 Markdown」，所以 `nav.build` 和 PageIndex 的
Markdown 路径都能直接吃，不用改代码。**

实现要点：

- **纯 REST over httpx**，没引入 Azure SDK 依赖
- 异步模型：`POST ...:analyze` 返回 `202` + `Operation-Location`，
  然后轮询直到 `status == "succeeded"`；尊重 `Retry-After`
- **页标记注入**：Azure 返回的是一整串 content，页边界在
  `analyzeResult.pages[].spans[].offset`。按 offset **倒序**插入
  `<!-- page: N -->`，就补回了 Markdown 路径本来会丢掉的页码引用
  —— 这正是之前 `nav/README.md` 里建议的「页码锚点」
- ⚠️ `AZURE_DI_STRING_INDEX_TYPE` 必须是 `unicodeCodePoint`（默认已设），
  否则 offset 与 Python 字符串下标对不上，页标记会插错位置
- 错误映射成人话：401 → 检查 key；404 → 检查 endpoint/model；
  429 → 免费版 F0 限流很严，降低 `--workers`
- 免费版 F0 限流极严，所以 `--workers` 默认只有 **2**，且提供 `--pages` 做便宜试跑

配置全部走 `.env`（见 `.env.example` 的 Azure 段）：

```
AZURE_DI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
AZURE_DI_KEY=<key-1>
AZURE_DI_MODEL=prebuilt-layout        # read / layout / document
AZURE_DI_OUTPUT_FORMAT=markdown       # nav.build 需要 markdown
AZURE_DI_FEATURES=formulas            # 可选
AZURE_DI_LOCALE=en-US                 # 可选
```

用法：

```bash
# 校验配置 + 只分析 1 页做试跑（便宜）
python scripts/06_azure_extract.py data/aia_reports --check --only FY2021

# 全量转换
python scripts/06_azure_extract.py data/aia_reports --out corpus_md

# 用两级导航索引这份 Markdown
python -m nav.build corpus_md --out corpus_index --summarize-files
```

已实测：带页标记 + 表格的 Markdown 经 `nav.build` 建树正常（页标记不影响
标题解析），取回章节正文时**表格完整保留、页标记作为引用锚点保留**。

⚠️ **尚未用真实 Azure 凭据端到端跑过** —— 逻辑层有 28 条离线断言覆盖
（`tests/test_azure_di.py`，不联网），但真实调用需要接手人配好 key 后验证。

### 5.7 文档（新增/更新）

- `README.md` — 新增「两种抽取方式」对比、Web UI、当前语料状态、题集说明
- `nav/README.md` — nav 包完整文档
- `docs/dify-improvement-plan.md` — Dify 改造方案
- `.env.example` — 新增 Azure DI 完整配置段（含各项取值说明）

### 5.8 测试素材（新增）

- `data/` 下的合成语料 — 16 文件 / 29 目录 / 228 章节节点（已从 `samples/test_corpus/`
  移入 `data/`，作为投放区的实际用例；`samples/test_index/` 是它的预建索引）
- `samples/test_index/` — 已建好且**带 LLM 摘要**的索引，可直接查询验证

### 5.9 `nav/registry.py` —— 多语料注册表与监控（新增）

把 `nav` 从「一次性 CLI」变成 UI 能驱动的东西。

| 能力 | 实现 |
|---|---|
| 注册目录 | `add()`，校验绝对路径/存在性/重复/**拒绝项目内目录** |
| 后台索引 | `index_async()` + 线程；`status`/`stage` 可轮询 |
| 变化监控 | `start_watcher()` 轮询，stat-only 比对，**只重建变化的文件** |
| 多语料查询 | `navigator(ids)` → `MultiNavigator`（见 5.9） |
| 状态持久化 | `index/registry.json`；索引在 `index/corpora/<id>/`（构建产物，已 gitignore） |

**四个关键设计（都是踩出来的）**：

1. **增量扫描必须跳过未变文件** —— `scan()` 新增 `previous` 参数。
   否则 watcher 每次轮询都会把**每个 PDF 重新发给 Azure DI**，成本爆炸。
   同时新增 `stats_only` 用于「只问有没有变化」。
2. **变更文件的旧摘要必须丢掉** —— 顺手修了 `_build()` 里的 bug：原来无条件
   `fe.summary = old.summary`，改过的文件会带着**描述旧内容的摘要**参与路由。
3. **watcher 沿用语料自身的设置** —— 注册时关掉文件描述，watcher 不会偷偷
   开始调 LLM（`Corpus.summarize_files`）。测试靠这个保持离线。
4. **`auto_index=False` 是「只检测不动作」** —— 明确成 dry-run 语义，
   而不是原来的「不索引 pending」（那个语义含糊且实测容易误解）。

其他：目录消失 → `error` + 可读信息；`_prune_orphan_trees()` 清理已删文件的
章节树，否则删除不回收磁盘。

### 5.10 `data/` 投放区（新增）

**往 `data/` 下丢目录就会被自动索引**，不需要注册步骤。

| 环节 | 实现 |
|---|---|
| 发现 | `Registry.discover_data_root()`：扫 `data/` 的直接子目录 |
| 注册+索引 | `Registry.sync_data_root()`：未注册的自动 `add()` + `index_async()` |
| 启动时同步 | `webapp/server.py` 的 `main()` 里调用一次 |
| 运行中同步 | watcher 每轮调用一次 → 新目录一个周期内自动进来 |
| 默认浏览起点 | `/api/browse` 与 UI 目录浏览器默认打开 `DATA_ROOT` |

**规则**：
- 每个**直接子目录**是一个语料（不是整个 `data/` 当一个语料）
- 没有任何可索引文件（空目录、只有 `.sh`）→ `_has_indexable()` 跳过，
  不产生空语料
- 隐藏目录与 `DEFAULT_EXCLUDES` 跳过
- `data/` 只读 —— 索引写到 `index/corpora/`
- `SUPERINDEX_DATA_DIR` 可换位置
- **入库规则**：只有 PDF 被 gitignore；仓库自带合成语料（16 个 .md，76 KB）
  让新克隆开箱可演示。其余内容会入库，所以私密文档不该放 `data/`

**项目内注册规则的例外**：原来 `add()` 拒绝一切项目内路径。现在放行
`DATA_ROOT` 及其子目录（因为索引不写回 `data/`），其余仍拒绝 ——
`_inside(p, ROOT) and not _inside(p, DATA_ROOT)`。

**顺手修掉的两件事**：
1. `download.sh` 原来写**当前工作目录**，从仓库根跑就会把 PDF 撒在 `data/` 下
   —— 已加 `cd "$(dirname "$0")"`，现在总是落在 `data/aia_reports/`
2. 两份游离在 `data/` 根下的 PDF 已移回 `data/aia_reports/`，
   5 个脚本的 `DATA_DIR` 现在能找到它们了

### 5.11 `nav/route.py` 的多语料支持（新增）

- `merge_manifests()` —— 把 N 个语料合并成一棵路由树，每个语料是顶层伪目录，
  rel_path 加 `<corpus_id>/` 前缀。**第 1 级仍是一次调用**，且模型能跨语料
  比较分支
- `MultiNavigator` —— 只覆写 `_load_tree()`，靠前缀反查该从哪个索引目录加载
- `build_context()` / `answer_prompt()` —— 按相关性截断（预算内取满），
  避免长尾章节挤掉最相关的那个
- `Navigator._load_tree()` 抽成可覆写方法（原来两处硬编码 `self.m.load_tree`）

### 5.12 LLM 重试与 token 预算升级（修 bug）

**症状**：`RuntimeError: LLM call failed after 3 attempts: (empty content,
finish_reason=length)` —— 提问时回答阶段直接报错。

**根因（三个叠在一起）**：

1. **重试是「原样重试」** —— `finish_reason=length` 说明预算不够，
   拿同样的 `max_tokens` 再试一次**必然同样失败**。三次重试全废。
2. **reasoning token 占预算** —— 开了 `reasoning_effort` 时，思考 token
   也算进 `max_tokens`。预算小时思考完就没额度输出内容了，
   于是「正常结束但内容为空」。
3. **答案预算太小** —— 服务器给回答阶段 1500，对开 reasoning 的模型不够。

**修法**（`nav/llm.py`）：

- 区分「截断」和「传输错误」：
  - 截断 → 每次重试 **`max_tokens` 翻倍**（上限 `NAV_MAX_TOKEN_CEILING`，
    默认 16384），同时**去掉 `reasoning_effort`** 把预算让给内容
  - 传输错误 → 原样重试（预算不变）
- `chat_stream_text()` 的回退也用**翻倍后的预算**（原来复用同一个，
  所以流式失败后非流式必然同样失败）
- 服务器答案预算 1500 → **4096**（`SUPERINDEX_ANSWER_MAX_TOKENS` 可覆盖）
- 报错信息改成**逐次列出每次尝试**的预算与 reasoning 状态，便于诊断

覆盖测试：`tests/test_llm_retry.py`（78 条，stub 掉 litellm，不联网）。

### 5.13 `nav/debuglog.py` —— 结构化调试日志（新增）

目的：**回答错了能事后查，不用猜**。

两条 append-only JSONL（`results/logs/`，已 gitignore）：

| 文件 | 内容 |
|---|---|
| `queries.jsonl` | 每次提问一条：范围、每一步路由决策、读到的来源、回答、分阶段耗时 |
| `errors.jsonl` | 每次异常一条：类型、消息、完整 traceback、当时的上下文 |

两者**共享 id**，异常能 join 回它所属的查询。

**三种终态**（区分开很重要）：

- `finish()` —— 正常回答
- `abort(reason)` —— 跑完了但没找到（没定位到文件/章节）。**只写 queries**，
  因为「空结果」是质量信号，不是 bug
- `fail(exc)` —— 抛异常。**写 queries（ok=false）+ errors**，id 关联

**读取**：

```bash
python scripts/07_logs.py                  # 列表
python scripts/07_logs.py --failed         # 只看失败的
python scripts/07_logs.py --id q-xxxxxxxx  # 单条完整还原 + 关联异常
python scripts/07_logs.py --kind errors    # 异常列表
python scripts/07_logs.py --stats
```

服务端：`GET /api/logs?kind=queries|errors&limit=N&failed=1`；
`/api/state` 里带 `logs` 统计。

**工程约束**：

- **日志失败绝不能影响主流程** —— 每次写入都包了 try，失败只在 stderr
  打一行然后静默（有测试专门验证）
- 超过 `SUPERINDEX_LOG_MAX_BYTES`（默认 16MB）自动轮转成 `.1`
- `SUPERINDEX_DEBUG_LOG=0` 可关闭，`SUPERINDEX_LOG_DIR` 可换位置

覆盖测试：`tests/test_debuglog.py`（44 条，全部写临时目录）。

### 5.14 `nav/policy.py` + `config/routing_policy.yaml` —— 业务路由策略（新增）

**要解决的问题**：`nav/route.py` 对「语料长什么样」有四个内置假设 ——
报告期就是 4 位年份、目录名不可信、每个目录同等值得看、问题里有什么词就用什么词。
面对未知语料这是对的，但在企业内部语料不是未知的：业务方知道法定年报在
`annual/` 下、`_drafts/` 不该被搜、「友邦」和「AIA」是同一家公司。

这部分知识原来只能写死在 Python 里。现在它属于 `config/routing_policy.yaml`。

**四个注入点**（每个对应 route.py 里的一个假设）：

| 方法 | 位置 | 作用 |
|---|---|---|
| `periods_in()` | `_pick_dirs_from_tree` / `_pick_files` / 两个 fallback | 内置 4 位年份 + 业务写法；`FY24` 归一成 `2024` |
| `weight_for()` / `annotate()` | `_score_candidate` 的 `weight=`、提示词每行末尾 | 目录权重加分 + `[优先+3 法定年报]` 标注 |
| `prompt_block()` | 目录/文件选择提示词 | `scopes` / `instructions`，纯提示 |
| `is_excluded()` / `alias_terms()` | `_visible_dirs/_files`、两个 fallback | 硬过滤 + 同义词扩展 |

**两条红线**（改动前请先看 `tests/test_policy.py`）：

1. **空策略 = 内置行为。** `RoutingPolicy()` 对所有方法都返回改造前的结果。
   删掉配置文件，行为与这个功能不存在时完全一致。测试里逐条断言了这一点，
   这是这个功能敢上线的唯一理由 —— 任何让「空策略 != 原行为」的改动都是回归。
2. **配置排序，不设闸门。** 权重和业务域只影响模型的倾向；只有 `exclude`
   真的移除候选。而且 `exclude` 按**路径整段**匹配（不是子串），
   所以 `exclude: [draft]` 不会误伤 `drafting-guidelines/` —— 见
   `_pattern_hits_segment()` 与 `_pattern_hits_path()` 的区别。

**单语料覆盖**：`corpora:` 段按语料**显示名**写（配置文件给人看），
`MultiNavigator.__init__` 里调一次 `bind_corpora()` 绑定到内部 id。
覆盖项与全局**合并**（`flatten_for`），不是替换。

**坏配置不致命**：`PolicyError` 由 `nav/route._bind_policy` 接住 → 退回空策略
→ 打一行 stderr → 记进 `errors.jsonl`（`where: policy.load`）。服务器照常起。

**可观测性**：`QueryTrace.policy()` 把当次生效的策略写进 `queries.jsonl`；
`/api/state` 的 `policy` 字段给 UI；`scripts/07_logs.py --id` 会打印出来。
「我加了权重但没效果」因此有确定答案：要么文件没找到（`source` 为空），
要么加载了但权重没匹配上。

```bash
$PY -m nav.route index/demo "..." --show-policy
$PY -m nav.route index/demo "..." --policy /path/x.yaml --corpus 友邦保险
```

覆盖测试：`tests/test_policy.py`（135 条，stub 掉 LLM，只写临时目录）。

### 5.15 每次 LLM 调用的埋点（新增）

**要解决的问题**：日志里只有**阶段总耗时**（`stages.route_ms`），而重试发生在
这个总耗时**内部**。于是一次「内部重试了两次才成功」的调用，和一次「单纯就是
慢」的调用，在事后看**完全一样**。之前为了解释一次慢查询，只能拿日志做相关性
推断——而那恰恰是错的（见 §7 的「冷启动」一节，我据此推错了一次结论）。

现在每次 `chat()` 都留一条记录：

| 字段 | 含义 |
|---|---|
| `label` | 调用阶段，可聚合：`route.dirs` / `route.files` / `route.sections` / `route.descend` |
| `tag` | 具体是哪一个：章节定位记「语料 / 文件」，路由调用为空 |
| `ms` / `attempt_ms` | 本次总耗时 / **每一次尝试**的耗时 |
| `attempts` / `retried` | 尝试了几次 / 是否发生过重试 |
| `prompt_chars` | 提示词长度，用来解释「为什么这次调用变贵了」 |

汇总字段落在 `queries.jsonl` 的每条记录上：`llm_calls`（原始列表）、
`llm_n_calls`、`llm_n_retried`、`llm_ms`。**第一个该看的是 `llm_n_retried`** ——
非 0 就说明这次查询有一部分时间花在「恢复」而不是「干活」上。

界面上对应检索框里的一行（`llmstats` 事件）：

```
模型调用 5 次 · 3.1s · 最慢 0.8s route.dirs
模型调用 3 次 · 4.2s · 重试 2 次 · 最慢 4.0s route.sections 友邦保险 / AIA_AR2024.md
```

**两个实现上的坑**（都在注释里写明了，改的时候别踩）：

1. **`ThreadPoolExecutor` 不继承 contextvars。** 采集桶放在 `ContextVar` 里
   （不是模块全局，否则后台建索引的调用会混进用户的查询记录）。但并行章节
   定位是提交到线程池的，**默认看不到这个桶** —— 而它们恰恰是「页面多」时
   最该被记账的那几次调用。所以 `sections_for_all()` 显式
   `contextvars.copy_context().run` 逐个任务拷贝上下文。
   注意必须**一个任务一份拷贝**：`Context.run` 不允许同一个 Context 被并发进入，
   四份共用一个会直接抛异常；拷贝也必须发生在**提交线程**上，在 worker 里拷贝
   只会拷到 worker 自己那个空上下文。
2. **`llm_ms` 是各次调用耗时之和，不是墙钟。** 章节定位是并行的，所以
   `llm_ms` 会大于 `stages.sections`。要比墙钟请对 `stages`，别对 `ms`。

覆盖测试：`tests/test_llm_retry.py`（78 条）里的 6 个埋点用例 +
`tests/test_policy.py` 的 `test_sections_for_all_carries_the_call_bucket`
（专门钉住上面第 1 个坑：如果谁把上下文拷贝去掉，并行调用会**静默地**不进
记录，读起来就像「检索很便宜」——比没有埋点更糟）。

### 5.16 首问冷启动 4~9s（修 bug）

上面那套埋点上线后，第一次真实查询就自己解释了自己：

```
total=7646ms  route=5159ms  sections=673ms  answer=1692ms
llm_n_calls=4  llm_ms=2276        ← route 阶段里只有 1222ms 是路由调用
```

**`stages.route` 和它内部的调用耗时对不上**，差 3.9s。热态复现则完全吻合
（1454 vs 1453、1152 vs 1150、1423 vs 1421）。所以这是一次性的、每进程
一次的开销，而且**不在 `litellm.completion()` 里**（那次调用实测只有 448ms）。

逐层量下来是两个叠加的原因，都在 `import litellm` 这一步（它是**懒加载**的，
所以这笔账会算在**碰巧第一个调用 `chat()` 的请求**头上，在服务器里就是重启后
的第一个问题）：

1. **litellm 在 import 时联网拉模型价格表**，拉不到才回退到内置副本。本机
   `HTTPS_PROXY` 指向本地代理，这次请求**超时 9s**。实测冷进程
   `_pick_dirs_from_tree` 9604ms，其中只有 ~700ms 是真的模型调用。
   → 修法：`nav/llm.py` 在 import litellm 之前
   `os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")`。
   PageIndex 自己也有这道防线（`pageindex/utils.py:16`），但 **`nav/` 在查询
   路径上从不 import 它**，所以那道防线从来没生效过。改完这一条：冷态未解释
   开销 8874ms → 2967ms。
2. 剩下的 ~2.9s 就是 **`import litellm` 本身**。
   → 修法：`llm.warmup()`，服务器启动时先付掉，并把秒数**打印出来**
   （`warmup : 2.9s (litellm import)`）——启动时的一段沉默也仍然是沉默。

改完实测：重启后**第一个**查询 `route=1454ms` vs 路由调用 `1453ms`，差 1ms；
总耗时 7646ms → 4834ms。

`litellm.model_cost` 仍然可用（从内置副本填充），全仓库只有
`PageIndex/pageindex/local_chat.py:1347` 读它，`nav/` 与 `webapp/` 都不用。

---

## 六、关键设计决策（**接手后请不要轻易改**）

这些决策都有实测依据，改动前请先复现对应的实验。

### 6.1 `reasoning_effort` 默认设成 `low`

实测同一道深层问题：默认 **10.29s** → `low` **5.80s**（−44%），
输出 token 1,646 → 629（−62%），答案不变。

在 `nav/` 里默认用 **`none`**（完全关闭）—— 因为路由任务不需要推理，
开着反而会让模型陷入自我辩论（见 6.4）。

### 6.2 索引并发降到 8（默认 64）

PageIndex 的 `SUMMARY_CONCURRENCY` 默认 **64**，这个并发突发会触发
DeepSeek 的**假性余额拒绝**（报 `Insufficient Balance`，但余额其实充足）。
降到 8 之后稳定。

### 6.3 提示词不给「留空」这个出口

`nav/route.py` 的提示词里写的是「**必须至少选 1 个**」，而不是
「都不相关时留空」。原因：早期版本给了留空出口，模型**不可预测地**返回空列表，
在明明可回答的问题上直接中止导航（同一问题连跑两次，一次选中一次不选）。

**教训：模型的默认倾向是保守，不要给它弃权的出口。**
真要表达不相关，交给下游阈值判断。

### 6.4 让模型只出「锚点」，不出「内容」

`nav/` 和 `04_md_audit.py` 都遵循这个原则：模型只判断「哪里是边界」
（给行号），标题和摘要由程序或后续独立调用生成。

原因：如果让模型同时「定位」和「撰写标题」，任务会**自相矛盾** ——
无标题文档里，生成标题必然等于改写内容。实测模型会在这个矛盾里打转，
把输出预算全烧在推理上，最后返回空字符串。

### 6.5 财务数字必须走确定性通道

这是最重要的结论，也是 `docs/dify-improvement-plan.md` 的核心：

> **PageIndex 解决「找对地方」（召回），不解决「算对数字」（精确）。**

即使检索到了正确的段落，最后仍是让模型**从文本里读数字** —— 依然是概率性的。
表格和数字必须走结构化查询 + 代码执行。

### 6.6 「报告期」是最强的元数据

实测：跨文档 chunk 相似度 ≥ 0.97 的占 **57.1%**；
问「2024 年 VONB」的榜首是 **FY2023** 的段落。

**任何 embedding 模型都解决不了这个问题**（有些重复段落字符级完全相同，
相似度恒等于 1.000000）。只有 `doc + period` 元数据过滤能解决。

---

## 七、踩过的坑（能省你几小时）

### 7.1 PageIndex flash 的进程模型

PDF 解析用 `ProcessPoolExecutor(mp_context=spawn)`。因此：

- 调用脚本**必须是真实文件**且带 `if __name__ == "__main__":` 保护
- **绝对不要用 heredoc 跑**（`python - <<'PY'`）—— spawn 的子进程无法重新导入
  stdin 的 `__main__`，表现为**进程挂死、无任何网络请求**

**判断依据**：`lsof -nP -iTCP | grep ESTABLISHED` 看不到该进程的连接，
说明卡在 spawn 而不是在调 LLM。

### 7.2 `md_to_tree()` 有个真 bug

`pageindex/page_index_md.py` 的 `md_to_tree()`，`summary_token_threshold`
默认 `None`，而 `get_node_summary()` 里做 `num_tokens < summary_token_threshold`
→ **TypeError**。必须显式传值（CLI 默认是 200）。

说明 markdown 路径比 PDF 路径**测试得少**，接手时要有心理预期。

### 7.3 markdown 路径默认不保留正文

`if_add_node_text='no'` 是默认值，会在生成摘要后把 `text` 剥掉。
**要检索必须显式开 `if_add_node_text='yes'`**，否则树里只有标题+摘要，
没有任何办法取回正文。

### 7.4 本地 client 只收 PDF

`local_api.py:105` 明确拒绝非 PDF。markdown 进不了 `PageIndexClient` 的检索链路，
必须走 CLI 或自己搭（`nav/` 就是这么做的）。

### 7.5 本地 I/O 不是瓶颈（别在这里优化）

实测 `tree.json` read+parse **0.8 ms**，`pages.json` **1.8 ms**，
`get_page_content(3 页)` 仅 **3 ms**。瓶颈全在 LLM 往返。

### 7.6 `litellm.success_callback` 不触发

在 openai-agents 路径下不会触发。要抓 token 用量**必须直接包
`litellm.acompletion`**（`scripts/profile_query.py` 就是这么做的）。

### 7.7 `nav/` 开发中修掉的四个 bug

留个记录，避免重蹈：

1. `rel()` 对根目录返回 `'.'` 而非 `''` → 顶层目录 parent 错了，导航直接失败
2. `ch.walk()` 返回 `(node, depth)` 元组，`summarize_chapters` 里忘了解包
3. 增量路径把 `trees` 写成 `[]`，清空了已生成的章节树
4. 验证脚本只数了顶层节点，误以为树是扁平的（脚本错，代码对）

---

## 八、已知限制与待办

### 8.1 最大缺口：没有评测集

**这是接手后最该先做的事。**

现在所有改动都无法量化评估。建议从真实业务问题里挑 **50–100 个**，每个标注：

- 问题
- 精确答案
- 来源（文件 / 页码 / 表名）
- 问题类型（单值查找 / 对比 / 计算 / 叙述）

测出基线后，每一步改动都跑一遍。**这一步的 ROI 高于任何技术改动。**

现成可用的起点：`scripts/questions_3docs.json`（18 题，含 ground truth + 页码）。

### 8.2 其他待办

| 项 | 说明 |
|---|---|
| 补齐 7 份报告索引 | 直接重跑 `02_qa_test.py` 即可（按文件名复用，不会重做前 3 份） |
| 缩短节点摘要 | 摘要平均 **1,167 字符**，占树的 **89%**。压到 300 字符可让树从 113K → ~35K tokens，9 片分页 → 1–2 片 |
| `nav/` 上真实语料验证 | 目前只用合成语料测过 |
| Dify 方案落地 | 见 `docs/dify-improvement-plan.md`，建议从「加 period 元数据」开始 |
| 清理 `results/logs/` | `webapp.log` 144KB、`qa_run.crashed.log` 17KB 等可删 |
| 删除遗留脚本 | `scripts/01_build_index.py`、`scripts/02_qa.py` |

### 8.3 `nav/` 的已知限制

- 章节级摘要需要 LLM，上千文件是一次性成本
- PDF 建树顺序：**Azure DI → flash（离线排版统计）→ 每页一节点 → 书签**。
  flash 让没有书签、没配 Azure 的 PDF 也能拿到真实章节树
  （AIA FY2022: 312 个 'Page N' → 469 个节点、5 层、真实标题）
- 目录结构质量决定上限：如果是平铺的几千个文件，第 1 级会退化
- 回退用词元匹配，同义词靠 `aliases:` 枚举；泛化需要 embedding 预筛，
  会把相似度问题引回来，权衡后保持确定性
- 策略只是「更倾向」，不是「会推理」：救不回答案本身不存在的问题；
  写错的 `exclude` 会真的藏掉目录（所以按路径整段匹配，不按子串）

---

## 九、快速验证清单

接手后按顺序跑一遍，确认环境没问题：

```bash
cd SuperIndex
PY=.venv/bin/python          # 或先 source .venv/bin/activate

# 1. 确认 PageIndex 装好了（应打印 0.2.10 和仓库路径）
$PY -c "import pageindex; print(pageindex.__version__ if hasattr(pageindex,'__version__') else 'ok')"
$PY -m pip show pageindex | grep -E "Version|Location"

# 2. 确认索引还在（应打印 3 份）
$PY -c "
import json; from pathlib import Path
d=json.loads(Path('results/pageindex/manifest.json').read_text())['docs']
print(len(d), '份已索引')"

# 3. 跑一道题验证端到端（约 5-10 秒；--out 用项目内相对路径）
$PY -u scripts/02_qa_test.py --skip-index --questions questions_3docs.json \
    --out qa_handover_check.json --only A05

# 4. 验证 nav 索引可用（应定位到 友邦保险/2024/annual/ + 股息章节）
$PY -u -m nav.route samples/test_index "友邦保险 2024 年全年的每股股息是多少？"

# 5. 跑离线测试（520 条断言，不联网，约 30 秒）
$PY -u tests/test_azure_di.py     # 28 条：配置/页标记/错误映射
$PY -u tests/test_backend.py      # 25 条：后端解析/按页切分/PageIndex 接管
$PY -u tests/test_registry.py     # 135 条：注册表/变更检测/watcher/投放区
$PY -u tests/test_llm_retry.py    # 78 条：token 预算升级/流式回退/思考与正文分流/每次尝试埋点
$PY -u tests/test_debuglog.py     # 44 条：日志记录/过滤/轮转/思考量字段
$PY -u tests/test_policy.py       # 135 条：路由策略（含「空策略 = 原行为」、并行顺序一致、调用记录跨线程传递）
$PY -u tests/test_suggest.py      # 75 条：示例问题生成/缓存/失效
#   注：下载样例 PDF 后 test_backend 会多 2 条（27 条）

# 5b. 看当前生效的路由策略（应指向 config/routing_policy.yaml）
$PY -u -m nav.route samples/test_index "x" --show-policy

# 6. 检查 Azure DI 配置（未配 key 会给出可操作的报错，这是预期的）
$PY -u scripts/06_azure_extract.py data/aia_reports --check
#   配好 key 后再加 --only FY2021 做一次 1 页试跑（会真实调用 Azure）

# 7. 启动 Web 界面
$PY webapp/server.py     # → http://127.0.0.1:8787
```

**当前 Web 服务已在运行**（端口 8787）。如需重启：
```bash
pkill -f "webapp/server.py" && nohup $PY -u webapp/server.py > results/logs/webapp.log 2>&1 &
```

---

## 十、安全与交接注意事项

### ⚠️ `.env` 里有真实的 DeepSeek API key

- **不要提交到版本库**
- 打包交给同事时，**建议把 `.env` 排除**，只给 `.env.example`，让对方填自己的 key
- 如果这个 key 已经流出过，建议在 DeepSeek 后台**轮换**

### 仓库包含/排除清单

| 目录 | 是否随仓库 | 说明 |
|---|---|---|
| `PageIndex/` | ✅ **包含**（2.1 MB / 113 文件） | vendored，锁定 `71714e8`；剥掉了 examples/assets/cookbook/tests |
| `data/aia_reports/*.pdf` | ❌ 排除（27 MB） | 用 `download.sh` 拉取，URL 有效 |
| `index/*` | ❌ 排除 | **集中索引库**：registry + 各语料的 manifest/trees。纯生成物，可重建 |
| `results/*` | ❌ 排除 | 日志与问答结果，纯生成物 |
| `.env` | ❌ 排除 | **含真实密钥** |
| `config/routing_policy.yaml` | ✅ **包含** | 业务路由策略。随包给一份保守默认（只启用 `_drafts`/`templates` 这类排除），
业务方在**原地改**，或用 `SUPERINDEX_ROUTING_POLICY` 指到仓库外 |
| `__pycache__` / `.DS_Store` | ❌ 排除 | 缓存与垃圾 |
| `samples/test_index/` | ✅ 包含（164 KB） | 虽是生成物，但让 `nav/` 能立刻演示 |

### 已完成 / 待办

✅ **已建 git 仓库并推送到 GitHub**：https://github.com/yongsoft/SuperIndex（私有）
✅ **已加 `.gitignore`**：排除 `.env`、`PageIndex/`、`data/**/*.pdf`、`index/*`、`results/*`、
`__pycache__`、`.DS_Store`
✅ **`PageIndex/` 不 vendor，README §0 给了 clone 步骤**
✅ **业务路由逻辑已外置**到 `config/routing_policy.yaml`（见 5.14）——
route.py 里不再写死「法定年报在哪个目录」这类业务知识

待办：

1. **加 CI**（可选）—— 至少跑一下 `python -m compileall` 和 `nav` 的导入检查
2. **`scripts/01_build_index.py` 和 `scripts/02_qa.py` 是早期版本**，
   已被 `01_build_trees.py` / `02_qa_test.py` 取代，可删
3. **考虑把 `samples/test_index/` 也纳入生成物** —— 目前保留是因为
   它让 `nav/` 能立刻演示（164 KB，带 LLM 摘要）。如果不想要，加进 `.gitignore`
   然后重跑 `nav.build --summarize-files --summarize-chapters` 即可重建

---

## 附：核心结论速查

接手后如果需要快速理解「为什么这么设计」，看这张表：

| 结论 | 实测依据 |
|---|---|
| 查询瓶颈在 LLM 往返，不在本地 I/O | tree.json 0.8ms / pages.json 1.8ms |
| 树太大是因为摘要太长 | 摘要占树体积 **89%**（388,929 / 435,687 字符） |
| 树被切成 9 片 | `total_parts=9`，全树 452,593 字符 ≈ 113K tokens |
| 多文档成本线性增长 | 每加一份文档 **+19K 冷 token**，且缓存基本失效 |
| 跨文档重复严重 | 相似度 ≥0.97 的 chunk 占 **57.1%**；部分段落字符级完全相同 |
| 重复文本吃掉 top-k | top_k=3 里可能 2 个槽位放同一段文字 |
| 推理是最大的延迟杠杆 | `reasoning_effort=low` 降 **44%** 墙钟 |
| 元数据是唯一解 | 重复段落相似度恒等 1.0，任何 embedding 都区分不了 |
