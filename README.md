# SuperIndex

Vectorless, page-cited question answering over long financial reports:
Azure Document Intelligence Markdown → tree index + BM25 → LLM agent.

`superindex` 面向上市公司年报、中期报告这类**长篇、表格密集**的财报问答：

- **建库**：把 Azure Document Intelligence（DI）产出的 Markdown（或任意带 `#` 标题的 Markdown）按标题建成层级**目录树**，可选用 LLM 为节点写摘要；同时建 **BM25** 关键词索引。
- **问答**：LLM agent 先拿到 BM25 预取的候选页，再沿目录树决定打开哪些节点、读哪几页，最后作答。
- **无向量库**：不做 embedding，不需要向量数据库；每个答案都能**追溯到页码**（DI 注入的 `<!-- page: N -->` 页标记）。
- 模型通过 [LiteLLM](https://docs.litellm.ai/) 接入：OpenAI 兼容网关、Azure OpenAI、Ollama、OpenAI / Anthropic / DeepSeek 等均可，**模型需支持 tool calling**。

## 安装

需要 Python 3.11–3.13。

```bash
pip install superindex
# 或者作为独立命令行工具安装（推荐，自动隔离环境）
uv tool install superindex
```

装好后直接使用 `superindex` 命令：

```bash
superindex --help
superindex index|search|ask|serve|batch --help
```

## 最小配置（`.env`）

在**当前工作目录**放一个 `.env`（已存在的环境变量优先）。完整模板见
[`.env.example`](https://github.com/VoldemortGin/SuperIndex/blob/main/.env.example)。

公司内网 OpenAI 兼容网关（vLLM、各类代理等，注意 `/v1` 后缀）：

```ini
SUPERINDEX_BASE_URL=https://your-gateway.example.com/v1
SUPERINDEX_API_KEY_OVERRIDE=your-gateway-key
SUPERINDEX_INDEX_MODEL=openai/your-model-name
SUPERINDEX_CHAT_MODEL=openai/your-model-name
SUPERINDEX_REASONING_EFFORT=
```

本机 Ollama（先 `ollama pull qwen2.5:7b`，并调大上下文 `OLLAMA_CONTEXT_LENGTH=32768`）：

```ini
SUPERINDEX_INDEX_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_CHAT_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_BASE_URL=http://localhost:11434
SUPERINDEX_API_KEY_OVERRIDE=ollama
# 非推理模型必须留空，否则报 "does not support thinking"
SUPERINDEX_REASONING_EFFORT=
```

没有配置模型时不会回落到任何云端模型，命令会直接报错并提示该设置哪个变量。

## 用法

### 建库：`index`

```bash
superindex index report.md --no-summary        # 单个文件，不调 LLM，几秒建完
superindex index ./corpus_md --no-summary      # 整个目录（递归查找 .md）
superindex index ./corpus_md                   # 带 LLM 节点摘要（--concurrency 默认 8）
superindex index ./corpus_md --force           # 内容未变也强制重建
superindex index ./corpus_md --store ./my_store
```

- 文档库默认在**当前工作目录下的 `superindex_store/`**；用 `--store` 或 `SUPERINDEX_STORE` 覆盖。之后的 `search` / `ask` / `serve` / `batch` 要用同一个 store。
- 建议先 `--no-summary` + `search` 自检（不花钱），跑通问答后再带摘要重建。
- Windows 路径同理，如 `superindex index D:\corpus_md --store D:\si_store`。

### 检索自检：`search`（BM25，不调 LLM）

```bash
superindex search "final dividend" --top-k 3
superindex search "末期股息 2022" --doc HarbourLife --match passage --json
```

`--match page`（默认）按整页打分；`passage` 按页内小段（约 300–800 字符，表格按行切分并重复表头）打分，仍返回整页，长页多主题时可能更好。

### 问答：`ask`

```bash
superindex ask "港湾人寿 2022 年新加坡的新业务价值是多少？" -v
superindex ask "..." --doc HarbourLife          # 限定文档（名称、id 或名称片段，可重复）
superindex ask "..." --instructions "只用中文回答，数字保留原单位。"
superindex ask "..." --instructions-file ./instructions.txt
```

- `-v` 把预取的候选页和每次工具调用打印到 stderr，便于排查"答非所问"。
- **检索预取**（默认开）：问题进入 agent 前先跑 BM25，把 top-k 候选页（文档、页码、章节、片段）作为线索附在问题前，模型即使不主动调用 `search_pages` 也能从可能的页开始。`--no-prefetch` 关闭，`--prefetch-k N` 调整条数（默认 5）。

### 网页：`serve`

```bash
superindex serve --port 8787                   # 浏览器打开 http://127.0.0.1:8787
superindex serve --host 0.0.0.0 --port 8787    # 局域网访问（注意防火墙）
```

「SuperIndex 财报问答」网页：勾选可用文档范围、流式输出答案，每条答案可展开查看模型的思考过程与全部工具调用（读了哪棵树、哪几页）。`serve` 与 `ask` 接受相同的 `--instructions` / `--match` / `--prefetch` / `--page-image` / 模型参数。

### 批量问答：`batch`

```bash
superindex batch questions.jsonl
superindex batch questions.csv --concurrency 2 --timeout 300
superindex batch questions.jsonl --doc HarbourLife --limit 5
superindex batch questions.csv --resume                       # 续跑最近一次，跳过已完成的题
superindex batch questions.jsonl --retrieval-only --match page    # 纯检索评测，不调 LLM
superindex batch questions.jsonl --retrieval-only --match passage
```

题集格式：

| 扩展名 | 格式 |
|---|---|
| `.txt` | 每行一题，`#` 开头为注释 |
| `.jsonl` | 每行 `{"id": "Q1", "question": "...", "expected": "...", "doc": "文档名片段"}`，只有 `question` 必填 |
| `.csv` | UTF-8，表头至少有 `question`，可选 `expected`、`doc`、`id` |
| `.json` | 题目列表（仓库 `scripts/questions.json` 的格式） |

结果默认写到 `<当前目录>/results/batch/<时间戳>/`（`--out` 可改）：`summary.md`（总览表 + 逐题问答与工具调用）和 `results.jsonl`（逐题完整记录）。"命中"是**粗评分**（期望答案里的数字全部出现在回答中），需要人工复核。
`--retrieval-only` 只跑每题的 BM25 检索，按 top-k（默认 5）页是否含期望答案计算 recall@k、MRR，秒级完成，适合比较 `--match page|passage`。

### 回答指令（ask / serve / batch 通用）

三个命令使用同一套"常驻指令"，**替换**内置默认。优先级从高到低：

1. `--instructions "文本"`
2. `--instructions-file 路径`（UTF-8 文本文件）
3. 环境变量 `SUPERINDEX_INSTRUCTIONS`（直接文本）
4. 环境变量 `SUPERINDEX_INSTRUCTIONS_FILE`（文件路径）
5. 内置中性默认：财务分析助手，按文档给出准确的数字、单位与报告期，文档没有答案时明确说明而不是猜测。

## 页码与附图

- **页码引用**：DI Markdown 中的 `<!-- page: N -->` 页标记在建库时保留，答案引用具体页码；没有页标记的 Markdown 按 `--page-chars`（默认 4000 字符）切伪页。
- **关联源 PDF**：`superindex index ./corpus_md --pdf-dir ./corpus_pdf`（或 `SUPERINDEX_PDF_DIR`）按同名文件（`年报.md` ↔ `年报.pdf`，子目录递归）关联 PDF；已有的库再跑一次只补关联，不重建文本。
- **多模态附图**（默认关，仅适用于能看图的模型）：`--page-image off|auto|always`（`SUPERINDEX_PAGE_IMAGE`）。
  - `auto`：预取候选页中含表格、图或文字很少（扫描页、图表页）的页附截图；`always`：候选页都附。
  - 两种模式下 agent 还能调用 `get_page_image(doc_name, page)` 按需取图。
  - 每题最多 `SUPERINDEX_PAGE_IMAGE_MAX` 张（默认 3），长边 `SUPERINDEX_PAGE_IMAGE_MAX_SIDE` 像素（默认 1600）。每张约 1–2.5K 输入 token，建议先在题集上对比 `off` 的命中率和成本。

## 数值计算：`calculate`

agent 带一个 `calculate` 工具，基于 [avada-eval](https://pypi.org/project/avada-eval/) 0.1.1+（安全的算式求值，不执行任意代码；Decimal 精确计算，含 `pct_change` / `cagr` / `ratio` 等财务函数与具名变量）。系统提示要求增长率、占比、差额、单位换算等一律经由它计算。千分位写作 `1,234`（逗号两侧无空格），函数参数的逗号后加空格。

## 配置变量

| 变量 | 说明 |
|---|---|
| `SUPERINDEX_CHAT_MODEL` | 问答模型（LiteLLM 模型名），`ask` / `serve` / `batch` 必填 |
| `SUPERINDEX_INDEX_MODEL` | 节点摘要模型；`index --no-summary` 时不用 |
| `SUPERINDEX_BASE_URL` | OpenAI 兼容网关或 Ollama 地址 |
| `SUPERINDEX_API_KEY_OVERRIDE` | 上面地址对应的 key |
| `SUPERINDEX_REASONING_EFFORT` | 推理强度（如 `low`）；不设或留空则不发送，非推理模型必须留空 |
| `SUPERINDEX_STORE` | 文档库目录，默认 `./superindex_store` |
| `SUPERINDEX_INSTRUCTIONS` / `SUPERINDEX_INSTRUCTIONS_FILE` | 回答指令（见上文） |
| `SUPERINDEX_BM25_MATCH` | `page`（默认）/ `passage` |
| `SUPERINDEX_PREFETCH` / `SUPERINDEX_PREFETCH_K` | 检索预取开关（默认 1）/ 条数（默认 5） |
| `SUPERINDEX_PDF_DIR` | 源 PDF 目录 |
| `SUPERINDEX_PAGE_IMAGE` / `_MAX` / `_MAX_SIDE` | 附图模式 / 每题张数 / 长边像素 |

命令行参数（`--chat-model`、`--index-model`、`--base-url`、`--api-key`、`--reasoning-effort` 等）优先于环境变量。Azure OpenAI、代理与自签证书等写法见 `.env.example`。

**旧名兼容**：早期版本的 `PAGEINDEX_INDEX_MODEL` / `PAGEINDEX_CHAT_MODEL` / `PAGEINDEX_BASE_URL` / `PAGEINDEX_API_KEY_OVERRIDE` / `PAGEINDEX_REASONING_EFFORT` 仍然有效（新名优先），使用旧名时会在 stderr 提示一次已更名，请改为对应的 `SUPERINDEX_*`。

## 许可与来源

MIT 许可。检索引擎（`superindex.engine`）衍生自 [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex)（MIT，v0.2.10，commit `71714e8`），并在此基础上改名、裁剪与扩展。版权与来源说明见
[LICENSE](https://github.com/VoldemortGin/SuperIndex/blob/main/LICENSE)、
[NOTICE](https://github.com/VoldemortGin/SuperIndex/blob/main/NOTICE) 与
[docs/engine/UPSTREAM.md](https://github.com/VoldemortGin/SuperIndex/blob/main/docs/engine/UPSTREAM.md)。

---

## 开发者 / 仓库用法

源码：<https://github.com/VoldemortGin/SuperIndex>

### 环境

用 [uv](https://docs.astral.sh/uv/) 管理 Python 与依赖：

```bash
git clone https://github.com/VoldemortGin/SuperIndex.git
cd SuperIndex
uv sync                                   # 建 .venv，superindex 以 editable 方式安装，并装 dev、build 依赖组
uv run superindex --help                  # 或：uv run scripts/superindex.py --help
uv run pytest tests -q
```

- 依赖组只有 `dev`（pytest、ruff）和 `build`（PyInstaller）；只要运行时依赖：`uv sync --no-default-groups`。
- Windows 上的完整上手步骤（含公司 LLM 网关、代理/证书、Ollama）：[docs/windows-quickstart.md](https://github.com/VoldemortGin/SuperIndex/blob/main/docs/windows-quickstart.md)。
- 仓库自带样例：`uv run superindex index samples/test_corpus_long/HarbourLife_AR2022.md --no-summary`，然后 `uv run superindex ask "港湾人寿董事会建议的末期股息是每股多少？" -v`（港湾人寿 Harbour Life 为虚构公司）。

### 目录结构

```
.
├── superindex/
│   ├── cli.py, __main__.py   # superindex 命令入口
│   ├── engine/               # 树索引 + agent 检索引擎（衍生自 PageIndex）
│   ├── extractors/           # PDF 抽取后端：Azure DI（REST）/ 文本层
│   ├── nav/                  # 两级导航：语料目录 → 文档 → 章节
│   ├── webapp/               # serve 的网页服务与 static/
│   └── bm25.py, prefetch.py, calc.py, page_images.py, batch.py, ...
├── scripts/                  # 实验与辅助脚本（superindex.py、06_azure_extract.py 等）
├── samples/                  # 样例 Markdown 与题集
├── tests/                    # 离线测试
├── packaging/                # PyInstaller 打包脚本
└── docs/                     # quickstart、交接文档、engine 来源说明
```

### 打包为免 Python 可执行程序（PyInstaller）

```bash
bash packaging/build_macos.sh                                        # macOS
```

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1  # Windows
```

冻结版的文档库默认在 exe 同目录的 `superindex_store/`，`.env` 先找当前工作目录再找 exe 目录。离线部署、onedir/onefile 取舍等见 [packaging/README.md](https://github.com/VoldemortGin/SuperIndex/blob/main/packaging/README.md)。

### PDF → Markdown：Azure Document Intelligence

`superindex index` 只读 Markdown。PDF 先用 Azure DI（`prebuilt-layout`）转成 Markdown：表格保留为真正的 Markdown 表格、扫描页做 OCR，并在页边界注入 `<!-- page: N -->` 页标记。在 `.env` 设置 `AZURE_DI_ENDPOINT` 与 `AZURE_DI_KEY` 后：

```bash
# 检查配置（并只分析一份 PDF 的第 1 页做冒烟测试；--only 按文件名片段筛选）
uv run scripts/06_azure_extract.py ./pdfs --check --only AR2022
# 把 PDF 抽成 Markdown 落盘，再建库
uv run scripts/06_azure_extract.py ./pdfs --out ./corpus_md
uv run superindex index ./corpus_md --pdf-dir ./pdfs
```

与 PDF 自带文本层（PyPDF2）相比：

| | 文本层 | Azure Document Intelligence |
|---|---|---|
| 成本 | 免费 | 按页计费 |
| 表格 | **糊掉**——图表页抽成 `175230`，两个数粘在一起，标签与数值的对应丢失 | 真正的 Markdown 表格 |
| 扫描件 / 纯图片 PDF | **直接拒绝**（无文本层、无 OCR） | OCR |
| 正文段落 | 好 | 好 |
| 页码锚点 | 原生（页范围） | 注入 `<!-- page: N -->` |

- 配置了 Azure 但调用失败时**直接报错停下**，不会静默退回文本层（否则写错 key 会悄悄改变索引质量）；设 `AZURE_DI_FALLBACK=1` 才退回。
- `AZURE_DI_STRING_INDEX_TYPE` 必须保持 `unicodeCodePoint`，页标记注入依赖字符偏移与 Python 字符串下标对齐。
- `superindex/extractors/azure_di.py` 是基于 httpx 的纯 REST 调用，不依赖 Azure SDK；`superindex/extractors/backend.py` 决定当前使用哪个抽取器。其余 `AZURE_DI_*` 选项见 `.env.example`。

### 两级导航（nav）

面向"多级目录 + 上千文件"语料的检索层，把树的粒度扩展为「语料目录 → 文档 → 章节」：先在目录树里定位文件，再在文件的章节树里定位章节。

```bash
uv run python -m superindex.nav.build ./corpus_md --out ./corpus_index --summarize-files
uv run python -m superindex.nav.route ./corpus_index "港湾人寿 2022 年的每股股息是多少？" --show-content
```

配置 Azure DI 后 `superindex.nav.build` 也可直接吃 PDF（启动时打印所用抽取器，`--extractor {auto,azure-di,text-layer}` 可强制指定）。详见 [superindex/nav/README.md](https://github.com/VoldemortGin/SuperIndex/blob/main/superindex/nav/README.md)。
