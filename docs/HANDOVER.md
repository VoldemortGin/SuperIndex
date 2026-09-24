# 交接文档 — PageIndex × AIA 财报检索项目

> 面向接手同事。读完这份文档 + `README.md` + `nav/README.md`，
> 应该能独立跑起来、看懂每个设计决策的理由、并知道坑在哪。

**代码仓库**：https://github.com/VoldemortGin/SuperIndex

```bash
git clone https://github.com/VoldemortGin/SuperIndex.git
cd SuperIndex
bash data/aia_reports/download.sh     # 唯一还需要单独获取的东西（10 份 PDF）
```

> `PageIndex/` **已随仓库分发**（vendored，锁定 upstream commit `71714e8`），
> 所以一次 clone 就能跑，不需要 `--recursive` 也不需要手动 clone 上游。
> 仓库里**不包含** `.env`（含密钥）、`data/*.pdf`（27MB）、`results/` 下的生成物。
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
| 财报问答（Web 界面，流式） | ✅ | `webapp/server.py`（**当前正在运行**，端口 8787） |
| 两级导航检索（目录 → 文档 → 章节） | ✅ | `nav/` |
| PDF → Markdown（Azure Document Intelligence） | ⚠️ 逻辑已验证，**未用真实凭据跑过** | `extractors/azure_di.py`、`scripts/06_azure_extract.py` |
| 查询延迟诊断 | ✅ | `scripts/profile_query.py` |
| Markdown 格式审计 | ✅ | `scripts/04_md_audit.py` |
| 跨文档重复度诊断 | ✅ | `scripts/05_similarity_probe.py` |

### 未完成的

- **10 份 AIA 报告只索引了 3 份**（1H2021 / FY2021 / FY2022），
  其余 7 份未索引。原因见第五节「为什么中途停了」。
- **Azure DI 抽取器没有用真实 Azure 凭据端到端验证过** —— 它是「配了就默认生效」
  的路径（见 5.6），离线逻辑有 55 条断言覆盖，但真实调用需接手人配 key 后跑一次
  `--check --only FY2021`。
- **`nav/` 只用合成语料验证过**，未在真实公司语料上跑过。
- **Dify 方案只有设计文档，未落地**。
- **没有评测集**（这是最大的缺口，见第八节）。

---

## 三、环境搭建（照着做就能跑）

### 3.1 Python 环境

项目用一个**独立 venv**，路径不在项目目录内：

```bash
# venv 位置（已存在）
/Users/yong/.workbuddy/binaries/python/envs/pageindex
```

后续命令统一用这个解释器。为方便，建议先设个别名：

```bash
PY=/Users/yong/.workbuddy/binaries/python/envs/pageindex/bin/python
```

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
cat /Users/yong/.workbuddy/binaries/python/envs/pageindex/lib/python3.13/site-packages/*pageindex*.pth
# 应输出: /Users/yong/WorkBuddy AI/2026-09-20-22-30-52/PageIndex
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
├── data/aia_reports/          10 份 AIA 报告 PDF（27 MB）
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
│   └── test_backend.py        后端解析的离线测试（27 断言，不联网）
│
├── webapp/                    Web 问答界面
│   ├── server.py              ★ 标准库 http.server，SSE 流式，端口 8787
│   └── static/index.html      单文件前端，原生 JS
│
├── nav/                       ★ 两级导航检索包（本次主要产出）
│   ├── README.md              包文档，务必读
│   ├── llm.py                 LLM 调用封装（JSON 提取+修复+重试）
│   ├── store.py               数据模型与持久化
│   ├── build.py               CLI：建索引
│   └── route.py               CLI：两级导航查询
│
├── docs/
│   ├── HANDOVER.md                本文档
│   └── dify-improvement-plan.md   Dify 知识库改造方案（设计文档）
│
├── samples/                   测试素材
│   ├── aia_ar2021_excerpt.md      规范的 markdown 样例
│   ├── bad_no_headings.md         无标题的坏样例
│   ├── bad_ppt_derived.md         PPT 转出的坏样例
│   ├── test_corpus/               16 文件 / 29 目录的合成语料
│   └── test_index/                已建好的 nav 索引（带摘要，可直接查）
│
└── results/                   产物与日志
    ├── pageindex_store/       ★ PageIndex 索引（3 份文档）
    ├── trees/                 10 份 PDF 的离线结构树（阶段1产物）
    ├── qa_results*.json       问答结果
    └── *.log                  各种运行日志（webapp.log 有 144KB，可清）
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

### 5.4 `webapp/` 新增

从零实现的 Web 问答界面。三个关键点：

1. **零额外依赖** —— 用 Python 标准库 `http.server` + `ThreadingHTTPServer`，
   没有引入 FastAPI/uvicorn。端口 **8787**（8000 被其他项目占用）
2. **必须迭代 `.events` 而不是文本流** —— 只有 events 才带 `tool_call`/`tool_result`，
   这正是「可追溯」卖点的来源
3. **`reasoning_effort` 可配** —— 见下

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

### 5.7 测试素材（新增）

- `samples/test_corpus/` — 16 文件 / 29 目录 / 228 章节节点的合成语料
- `samples/test_index/` — 已建好且**带 LLM 摘要**的索引，可直接查询验证

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
| 清理 `results/` | `webapp.log` 144KB、`qa_run.crashed.log` 17KB 等可删 |
| 删除遗留脚本 | `scripts/01_build_index.py`、`scripts/02_qa.py` |

### 8.3 `nav/` 的已知限制

- 章节级摘要需要 LLM，上千文件是一次性成本
- PDF 只走书签；没有内嵌书签的 PDF 退化成整份文件一个节点
- 目录结构质量决定上限：如果是平铺的几千个文件，第 1 级会退化
- 回退用词元匹配，对同义词无能为力

---

## 九、快速验证清单

接手后按顺序跑一遍，确认环境没问题：

```bash
cd "/Users/yong/WorkBuddy AI/2026-09-20-22-30-52"
PY=/Users/yong/.workbuddy/binaries/python/envs/pageindex/bin/python

# 1. 确认 PageIndex 装好了（应打印 0.2.10 和仓库路径）
$PY -c "import pageindex; print(pageindex.__version__ if hasattr(pageindex,'__version__') else 'ok')"
$PY -m pip show pageindex | grep -E "Version|Location"

# 2. 确认索引还在（应打印 3 份）
$PY -c "
import json; from pathlib import Path
d=json.loads(Path('results/pageindex_store/manifest.json').read_text())['docs']
print(len(d), '份已索引')"

# 3. 跑一道题验证端到端（约 5-10 秒；--out 用项目内相对路径）
$PY -u scripts/02_qa_test.py --skip-index --questions questions_3docs.json \
    --out qa_handover_check.json --only A05

# 4. 验证 nav 索引可用（应定位到 友邦保险/2024/annual/ + 股息章节）
$PY -u -m nav.route samples/test_index "友邦保险 2024 年全年的每股股息是多少？"

# 5. 跑离线测试（55 条断言，不联网，约 2 秒）
$PY -u tests/test_azure_di.py     # 28 条：配置/页标记/错误映射
$PY -u tests/test_backend.py      # 27 条：后端解析/按页切分/PageIndex 接管

# 6. 检查 Azure DI 配置（未配 key 会给出可操作的报错，这是预期的）
$PY -u scripts/06_azure_extract.py data/aia_reports --check
#   配好 key 后再加 --only FY2021 做一次 1 页试跑（会真实调用 Azure）

# 7. 启动 Web 界面
$PY webapp/server.py     # → http://127.0.0.1:8787
```

**当前 Web 服务已在运行**（端口 8787）。如需重启：
```bash
pkill -f "webapp/server.py" && nohup $PY -u webapp/server.py > results/webapp.log 2>&1 &
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
| `results/*` | ❌ 排除（4 MB） | 纯生成物，可重建 |
| `.env` | ❌ 排除 | **含真实密钥** |
| `__pycache__` / `.DS_Store` | ❌ 排除 | 缓存与垃圾 |
| `samples/test_index/` | ✅ 包含（164 KB） | 虽是生成物，但让 `nav/` 能立刻演示 |
| `.workbuddy-ai/` | ✅ 包含（48 KB） | 项目记忆，含大量决策记录 |

### 已完成 / 待办

✅ **已建 git 仓库并推送到 GitHub**：https://github.com/VoldemortGin/SuperIndex（原 yongsoft/SuperIndex，2026-09-23 起迁至 VoldemortGin/SuperIndex）
✅ **已加 `.gitignore`**：排除 `.env`、`PageIndex/`、`data/*.pdf`、`results/*`、
`__pycache__`、`.DS_Store`
✅ **`PageIndex/` 不 vendor，README §0 给了 clone 步骤**

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

---

## 2026-09-23 进展与后续

**本次完成**

- `uv run scripts/superindex.py`：Markdown 建库（`index`）→ `search` / `ask` / `serve` / `batch`；支持 Azure DI 原生 Markdown（页标记、表格）。
- BM25 关键词检索（`search_pages` 工具）：`--match page`（默认）/ `passage`（页内小段打分，仍返回整页）。
- `batch` 批量问答（txt/jsonl/csv/json 题集、`--resume`、粗评分）与 `--retrieval-only` 纯检索评测。
- PyInstaller 打包（`packaging/`）与 Windows 源码运行（`docs/windows-quickstart.md`，含公司 LLM API 配置与上手清单）。

**已知限制**

- DI 样例（`samples/`）为手写/合成，需用真实 DI 年报回归（先 `batch --retrieval-only` 对比 page/passage）。
- 小模型（Ollama 7B 级）常不主动调用 `search_pages`，只靠目录树导航。
- DocStore 的文件锁在 Windows 上无效，勿多进程同时写同一 store。
- 依赖 PageIndex 私有接口 `_tool_specs` / `_resolve_document`，升级 PageIndex 需回归。
- `batch` 命中为粗评分（数字全包含），需人工复核。

**候选后续**

1. ~~检索前置：Agent 启动前先跑 BM25，把候选页号/片段注入提示，不依赖模型主动调用工具。~~ **已完成**（`superindex/prefetch.py`，默认开，`--no-prefetch` / `--prefetch-k`；`batch` 统计候选是否含答案页）。
2. page / passage 两路结果做 RRF 融合。
3. 用公司 embedding API 做向量召回，与 BM25 混合；无 reranker，用 RRF 合并排序。
4. **已完成**：`calculate` 数值计算工具（`superindex/calc.py`，[avada-eval](https://pypi.org/project/avada-eval/)（[GitHub](https://github.com/VoldemortGin/avada-eval)） 0.1.x 的 `evaluate_for_llm`，50 位 ROUND_HALF_UP；千分位按 avada 规则：逗号两侧无空格且分组合法才算千分位，`max(1,234)` 读作 1234，GUIDANCE 要求函数参数逗号后加空格），与 `search_pages` 一起注册，系统提示要求算术一律调用；待用真实题集观察调用率与单位换算正确性。
5. **已完成**：PDF 原页截图附给多模态模型（方案 A，默认关）。
   - `index --pdf-dir`（`SUPERINDEX_PDF_DIR`）按同名 / `.meta.json` 关联 PDF，绝对路径与页数写入 doc metadata（`pdf_path` / `pdf_pages` / `pdf_stamp`）；旧库再跑一次即只补 metadata。伪分页（无页标记）的 Markdown 不关联。
   - 页标签 `docs/<id>/page_tags.json`（`has_table` / `has_figure` / `low_text` < 300 字符），旧库懒生成。
   - 渲染接口 `superindex/page_render.py`（pypdfium2 + Pillow，JPEG q80，长边 1600），缓存 `docs/<id>/images/<长边>/p<N>.jpg`；pypdfium2 / Pillow 已移入主依赖并打进 PyInstaller 包（onedir 91 MB → 106 MB）。
   - 送图方式（`superindex/image_chat.py`）：PageIndex 的 chat 只收文本，故在外层复用 `local_chat._chat_agent` / `_chat_events_agen` 自建流：检索前置附图放进问题那条 user 消息（`input_image` → Chat Completions `image_url` data URL）；`get_page_image` 工具只回文本，图片由 `RunConfig.call_model_input_filter` 作为紧跟 tool 结果之后的 user 消息插入（OpenAI Chat Completions 的 tool 消息不能带图）。用本地假 OpenAI 服务器验证了 `openai/<model>`+base_url 与 `azure/<deployment>` 两种请求体（`tests/test_page_images.py`）。
   - 依赖更多 PageIndex 私有接口（`local_chat._chat_agent` / `_run_kwargs` / `_stream_sync` / `_chat_events_agen`），升级 PageIndex 需回归；尚未在真实多模态模型上评测效果与成本。

---

## 2026-09-24 重构：发布为 PyPI 包 superindex 0.1.0

**目标**：以 `superindex` 发布到 PyPI（发行名、命令名、import 名一致），`pip install superindex` / `uv tool install superindex` 后直接 `superindex index|ask|search|serve|batch`。Python >=3.11（3.11–3.13）。

**目录变化**

| 原位置 | 新位置 |
|---|---|
| `PageIndex/pageindex/` | `superindex/engine/`（仓库不再有 `PageIndex/` 目录） |
| `nav/` | `superindex/nav/`（`python -m superindex.nav.build` / `python -m superindex.nav.route`） |
| `webapp/`（含 `static/`） | `superindex/webapp/`（单独运行 `python -m superindex.webapp.server`，等价于 `superindex serve`） |
| `extractors/` | `superindex/extractors/` |
| `PageIndex/UPSTREAM.md`、`PageIndex/docs/naming-rules.md` | `docs/engine/` |
| `PageIndex/LICENSE` | 根目录 `LICENSE`（lin han 2026 MIT + VectifyAI 原版权全文），另加 `NOTICE` |

- 引擎来源：VectifyAI/PageIndex v0.2.10，commit `71714e8`，MIT；来源、改动与升级方法见 `docs/engine/UPSTREAM.md`。
- 项目本身成为可安装包（hatchling）：`uv sync` 把 superindex 以 editable 装进 `.venv`；uv 依赖组只剩 `dev`、`build`（去掉了 `pageindex` 组）。源码运行 `uv run scripts/superindex.py ...` 与 `uv run superindex ...` 等价。

**import 改写**

- `pageindex` → `superindex.engine`（内部导入一并改写）；产品名 PageIndex → SuperIndex：`PageIndexClient` → `SuperIndexClient`、`PageIndexLocalClient` → `SuperIndexLocalClient`、`PageIndexAPIError` → `SuperIndexAPIError`；`version("pageindex")` → `version("superindex")`。
- VectifyAI 托管云客户端（`PageIndexCloudClient`、`PAGEINDEX_API_KEY`、`api.pageindex.ai` 等）已从引擎删除，见 `docs/engine/UPSTREAM.md`「Trimmed to the local mode」。
- 保留原名（存储/输出兼容）：本地存储目录 `.pageindex`、`pi-` 文档 id、`pageindex-citation-NN` 锚点——旧文档库无需重建。

**环境变量改名**（新名优先；只设旧名时仍生效，并在 stderr 提示一次"已更名"）

| 旧名 | 新名 |
|---|---|
| `PAGEINDEX_INDEX_MODEL` | `SUPERINDEX_INDEX_MODEL` |
| `PAGEINDEX_CHAT_MODEL` | `SUPERINDEX_CHAT_MODEL` |
| `PAGEINDEX_BASE_URL` | `SUPERINDEX_BASE_URL` |
| `PAGEINDEX_API_KEY_OVERRIDE` | `SUPERINDEX_API_KEY_OVERRIDE` |
| `PAGEINDEX_REASONING_EFFORT` | `SUPERINDEX_REASONING_EFFORT` |

映射在 `superindex/runtime.py`。

**可配置指令与去品牌**

- `ask` / `serve` / `batch` 统一使用同一套常驻指令，语义统一为**替换默认**（此前 ask/batch 的 `--instructions` 是附加，serve 的是替换网页默认）。优先级：`--instructions "文本"` > `--instructions-file 路径` > `SUPERINDEX_INSTRUCTIONS` > `SUPERINDEX_INSTRUCTIONS_FILE` > 内置中性默认（英文财务分析助手提示：准确数字、单位、报告期；文档没有就直说，不猜）。
- `superindex serve` 的 reasoning effort 与 `ask` 一致：未设置就不发送（去掉了独立运行 webapp 时默认 `"low"` 的行为）。
- 去品牌：网页标题改为「SuperIndex 财报问答」，示例问题改用虚构公司"港湾人寿 Harbour Life"（`samples/test_corpus_long/HarbourLife_AR2022.md`）；README / `.env.example` / quickstart / packaging 文档不再出现具体公司品牌。

**默认路径变化**

- 文档库：非冻结（pip 安装或源码）时默认**当前工作目录下 `superindex_store/`**（以前源码默认是仓库的 `results/superindex_store`，旧库请用 `--store results/superindex_store` 或 `SUPERINDEX_STORE` 指回）；PyInstaller 冻结 exe 时仍是 exe 同目录 `superindex_store/`。
- `batch` 输出默认 `<cwd>/results/batch/<时间戳>`（冻结时在 exe 目录下）。
- `.env`：先找当前工作目录，冻结时再找 exe 目录；已有环境变量优先。

**依赖约束策略**

- `[project].dependencies` 写版本区间（不钉死），便于与其他工具共存；快速迭代的 0.x 库上限卡到下一个 minor。
- `uv.lock` 锁定实测版本组合；`packaging/requirements-bundle.txt` 由 `uv export --frozen --no-default-groups --group build --no-hashes --no-emit-project -o packaging/requirements-bundle.txt` 生成，不要手改。

**打包 / 发布流程**

```bash
uv lock                      # 改依赖后
uv run pytest tests -q
uv build                     # 产出 dist/superindex-0.1.0.tar.gz 与 .whl
uvx twine check dist/*
uvx twine upload dist/*      # 需要 PyPI token（TWINE_USERNAME=__token__ / TWINE_PASSWORD）
```

- 发布前改 `pyproject.toml` 的 `version`；sdist 只含 `superindex/`、`pyproject.toml`、`README.md`、`LICENSE`、`NOTICE`（tests/samples/scripts 仅在仓库）。
- README 即 PyPI 页面：指向仓库文件的链接必须用绝对 GitHub URL（https://github.com/VoldemortGin/SuperIndex/blob/main/...）。
- PyInstaller 单机包流程不变：`bash packaging/build_macos.sh` / `packaging\build_windows.ps1`（见 `packaging/README.md`）。

---

## 0.1.2 更新（未发布）

- 源码入口薄壳 `scripts/superindex.py` 改名为 `scripts/si.py`：原名与包同名，运行 `scripts/` 下其他脚本时会遮蔽 `superindex` 包（报 `'superindex' is not a package`）。上文各段里的 `uv run scripts/superindex.py ...` 现在一律写作 `uv run python scripts/si.py ...`（依赖 `uv sync` 以 editable 方式安装的包）；其余脚本同样用 `uv run python scripts/<name>.py` 运行。
