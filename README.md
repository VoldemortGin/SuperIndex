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
superindex index|search|ask|serve|batch|nav-serve --help
```

## 最小配置（`.env`）

在**当前工作目录**放一个 `.env`（已存在的环境变量优先）。完整模板见
[`.env.example`](https://github.com/VoldemortGin/SuperIndex/blob/main/.env.example)。

公司内网 OpenAI 兼容网关（vLLM、各类代理等，注意 `/v1` 后缀）。最短写法是下面三个变量（模型名写端点上的名字，自动加 `openai/` 前缀）：

```ini
OPENAI_MODEL=your-model-name
OPENAI_BASE_URL=https://your-gateway.example.com/v1
OPENAI_API_KEY=your-gateway-key
```

等价的 `SUPERINDEX_*` 写法：

```ini
SUPERINDEX_BASE_URL=https://your-gateway.example.com/v1
SUPERINDEX_API_KEY_OVERRIDE=your-gateway-key
SUPERINDEX_INDEX_MODEL=openai/your-model-name
SUPERINDEX_CHAT_MODEL=openai/your-model-name
SUPERINDEX_REASONING_EFFORT=
```

`OPENAI_*` 的规则：**只有 `OPENAI_MODEL` 非空才启用**（很多机器的环境里本来就有 `OPENAI_API_KEY` / `OPENAI_BASE_URL`，单独存在不会改变行为）；启用后 `OPENAI_BASE_URL` → `SUPERINDEX_BASE_URL`、`OPENAI_API_KEY` → `SUPERINDEX_API_KEY_OVERRIDE`，`OPENAI_MODEL` 同时作为问答模型和（没设 `SUPERINDEX_INDEX_MODEL` 时的）建库摘要模型。优先级逐变量判断：命令行参数 > `SUPERINDEX_*` > 旧名 `PAGEINDEX_*` > `OPENAI_*`，即 `SUPERINDEX_*` 已设为非空时不被覆盖。模型名没有 `openai/` 前缀时自动补上（含 `/` 的名字如 `Qwen/Qwen2.5-72B` 也会变成 `openai/Qwen/Qwen2.5-72B`）。`SUPERINDEX_REASONING_EFFORT` 不受影响，非推理模型仍要留空。两级导航 `superindex.nav` 不认这组变量（见文末「两级导航」）。

本机 Ollama（先 `ollama pull qwen2.5:7b`，并调大上下文 `OLLAMA_CONTEXT_LENGTH=32768`）：

```ini
SUPERINDEX_INDEX_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_CHAT_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_BASE_URL=http://localhost:11434
SUPERINDEX_API_KEY_OVERRIDE=ollama
# 非推理模型必须留空，否则报 "does not support thinking"
SUPERINDEX_REASONING_EFFORT=
```

`superindex` 的各子命令在没有配置模型时不会回落到任何云端模型，会直接报错并提示该设置哪个变量。例外是仓库内的两级导航 `superindex.nav`（见文末「两级导航」）：它有自己的默认模型 `deepseek/deepseek-flash`。

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

另有一个面向"整目录语料"的网页 `superindex nav-serve`（注册目录、自动建索引、按目录提问），见文末「目录驱动 Web UI：`nav-serve`」。

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

### Notebook：从原始 PDF 一路跑到答案表

`notebooks/batch_qa.ipynb` 把"一批原始 PDF → Markdown（文字层 + flash 补标题，或 Azure DI）→ 建库 → 用同一条 agent 链路回答 JSON/JSONL 题集 → `summary.md` / `answers.csv` / `answers.xlsx`"串成一次运行；中间产物（`md/`、`store/`、`runs/<时间戳>/`）落在 `<ROOT_DIR>/results/notebook/<题集名>/`（`ROOT_DIR` 是「环境」cell 打印的绝对路径：`SI_NB_ROOT_DIR` > 从当前目录向上找到的仓库根 > clone 副本 > 当前目录；相对路径配置都以它为基准，不依赖 cwd），重跑时已转换的 PDF、已建好的库、已成功的题都会跳过，`results.jsonl` 与 `superindex batch` 兼容。默认只读 PDF 文字层（绝不调用 Azure，需显式设 `PDF_EXTRACTOR="azure-di"`）；无文字层的扫描件会被标记跳过（`skipped_pdfs.json`），指向它的题不问模型、在结果里标 `skipped_no_text_layer`。

```bash
uv sync --group notebook                  # ipykernel / pandas / openpyxl（不在主依赖里）
# 用 Jupyter 或 VS Code 打开 notebooks/batch_qa.ipynb，改配置 cell（题集、PDF 目录、字段映射等）后依次运行（不要运行带 databricks-setup 标签的「环境安装」两个 cell）；或命令行：
uv run --group notebook jupyter nbconvert --to notebook --execute notebooks/batch_qa.ipynb \
  --TagRemovePreprocessor.enabled=True --TagRemovePreprocessor.remove_cell_tags databricks-setup \
  --output-dir results/notebook
```

**开头会先更新代码**：Notebook 最前面的「更新代码（git pull）」cell 在仓库里运行时执行 `git pull --ff-only`（只快进）；不在仓库里（如 Databricks 上单独导入）则 clone 到 `REPO_DIR`（默认 `<当前目录>/SuperIndex`），之后「环境」cell 优先用这份代码（插到 `sys.path` 最前面，优先于 `%pip` 装的 PyPI 版，也不会切换工作目录）。`SI_NB_GIT_PULL=0` 关闭；没装 git、网络不通、认证失败等任何失败都只提示、不中断，继续用现有代码；本地有改动（如在 Jupyter 里保存过带输出的 Notebook）导致无法快进时只提示处理办法，**不会自动覆盖或 stash 你的改动**。pull 更新的是磁盘上的代码：Notebook 文件自身被更新后需重新打开再运行。命令行 `nbconvert` 执行同样会先 pull，所以工作区有未提交改动时可能看到上述提示（不影响运行）。

命令行执行**必须带** `--TagRemovePreprocessor...` 这两个参数：Notebook 开头的「环境安装」cell（`%pip install superindex==0.1.2 ...`）只给 Databricks 用，不跳过的话它会把 PyPI 上的 `superindex` 装进当前环境，覆盖本地的可编辑安装。

问答需要在 `.env` 配好支持 tool calling 的 `SUPERINDEX_CHAT_MODEL`（OpenAI 兼容端点也可以只写 `OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY`，见下文「配置 LLM」）；配置项也可用 `SI_NB_DATASET` / `SI_NB_PDF_DIR` / `SI_NB_LIMIT` 等环境变量覆盖。

#### 在 Databricks 上运行

同一个 Notebook 可以导入 Databricks 直接运行（Python 3.11+，按 Databricks 官方的 Runtime 与 Python 版本对应关系，对应 Runtime 15.x 及以上）。步骤：

1. 导入 `notebooks/batch_qa.ipynb`，从上到下运行（最前面的「更新代码」cell 会把仓库 clone 到 `REPO_DIR`——要求集群有 git、能访问 `REPO_URL`、目录可写；做不到只提示、不中断，继续用 `%pip` 装的版本；不需要可设 `GIT_PULL = False`）：先运行「环境安装」两个 cell（`%pip install superindex==0.1.2 pandas openpyxl pycryptodome`，随后重启 Python）。默认从 PyPI 装；公司内部 PyPI 镜像 / Git URL / Repos 路径 / Volumes 里的 wheel 的写法见 Notebook 里「环境安装」的说明，用其中一行替换该 cell 的内容。
2. 在「模型配置」cell 的 `LLM_CONFIG` 里填模型（密钥用 `dbutils.secrets.get(scope=..., key=...)`，不要明文写进 Notebook）：
   - OpenAI 兼容端点：`OPENAI_MODEL="<模型名>"` + `OPENAI_BASE_URL="https://<网关>/v1"` + `OPENAI_API_KEY`（Notebook 的「环境」cell 会把它们映射成 `SUPERINDEX_*`，所以 PyPI 上的版本也适用）；等价写法 `SUPERINDEX_CHAT_MODEL="openai/<模型名>"` + `SUPERINDEX_BASE_URL` + `SUPERINDEX_API_KEY_OVERRIDE`
   - Databricks Model Serving：`"databricks/<endpoint 名>"` + `SUPERINDEX_BASE_URL="https://<workspace>/serving-endpoints"` + token
   - Azure OpenAI：`"azure/<deployment>"` + `AZURE_API_BASE` / `AZURE_API_KEY` / `AZURE_API_VERSION`
   - 公司网关要求自定义请求头：填 `LLM_EXTRA_HEADERS`（只加在问答请求上，建库摘要 `INDEX_SUMMARY=True` 不带）。
   - 公司只提供非 OpenAI 协议的 Python SDK：把「公司 SDK 适配器」cell 里的 `USE_CUSTOM_LLM` 改 `True`，在 `company_chat(messages, tools)` 里调用 SDK（返回 `{"text", "tool_calls"}`，约定见函数文档），模型名写 `company/<任意名>`。适配器是 litellm 的 `CustomLLM`，问答和建库摘要都能走；流式是整段一次性吐出的假流式。
3. 在配置 cell 填 `DATASET_PATH`、`PDF_DIR`（如 `/Volumes/<catalog>/<schema>/<volume>/...`）和 `PERSIST_DIR`，其余默认即可。

**工作目录与 `PERSIST_DIR`。** `WORK_DIR`（`md/`、`store/`、`runs/`）在 Databricks 上也与本地一致，默认 `<ROOT_DIR>/results/notebook/<题集名>`；存储层用 `fcntl.flock`、`os.replace` 和逐行追加，`/Workspace`、`/dbfs`、`/Volumes` 这类 FUSE 路径对它们的支持不确定，需要时设环境变量 `NB_WORK_DIR`（可写进 `.env`）为集群本地盘（如 `/local_disk0/...`）上的绝对路径。本地盘随集群消失，此时设 `PERSIST_DIR`（如 `/Volumes/<catalog>/<schema>/<volume>/superindex_nb/<题集名>`）：开始时若里面有上次的 `md/`、`store/`、`runs/`、`skipped_pdfs.json` 而本地没有，就复制回来（集群重启后不必重抽取、重建库，已答的题可续跑）；建库后、全部完成后再把 `WORK_DIR` 复制回去，复制失败只警告不中断。不设则全部跳过。

**本机用 nbconvert 执行时**，带上面的跳过参数即可，不会触发 `%pip`。

> Databricks 专属的部分（`%pip` 安装、`restartPython`、`dbutils.secrets`、Volumes 读写、Model Serving 端点）没有在真实 Databricks 环境测过，首次使用请先用 `LIMIT=1` 跑一题确认。

#### 答案评测：`notebooks/eval_deepeval.ipynb`

用 deepeval 的 `GEval`（LLM-as-judge）对照 `expected` 逐题判定 `batch_qa` 的答案是否正确，输出准确率。输入默认是 `<ROOT_DIR>/results/notebook/<题集名>/runs/<最新时间戳>/results.jsonl`（可用 `SI_NB_RESULTS` 指定；设了 `NB_WORK_DIR` 时从它下面的 `runs/` 找），结果写在同目录的 `eval/` 下（`eval_results.jsonl`、`eval_summary.md`、`eval.csv`、`eval.xlsx`），已评过的题重跑时跳过；没有 `expected` 或 `batch_qa` 未成功的题标为 `not_evaluated`。裁判模型配置复用 `batch_qa`（`.env` 或「模型配置」cell 的 `OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY`，`SI_NB_JUDGE_MODEL` 可换成别的裁判模型），deepeval **不在项目依赖里**（它会拉低 `click` 等包的版本）：Databricks 上由 notebook 的 `%pip install` 安装，本地用 `uv run --group notebook --with deepeval jupyter ...` 临时叠加（不改 `uv.lock`），`nbconvert` 的跳过参数与 `batch_qa` 相同。

#### 配置 LLM

**哪些步骤用到模型。** PDF 抽取（文字层或 Azure DI）、补标题（`ADD_HEADINGS`，离线版面分析）、默认建库（`INDEX_SUMMARY=False`）都**不调模型**；只有步骤 4 问答一定调用 `SUPERINDEX_CHAT_MODEL`，`INDEX_SUMMARY=True` 时建库摘要还会用 `SUPERINDEX_INDEX_MODEL`（没设则报错提示）。所以没配模型也能先跑完前几步，检查 `md/`、`store/` 等中间产物。

**这个项目怎么调用大模型。** 问答是 openai-agents 的工具循环（模型调用 `get_document_structure` / `get_page_content` / `search_pages` / `calculate`，直到给出回答），模型调用由 litellm 路由到具体厂商。模型名带 `openai/` 前缀时，发的是 OpenAI **Chat Completions** 流式请求（`POST {base}/chat/completions`，`stream=true`，带 `tools` 和 `stream_options.include_usage`）；**不走 Responses API**。`databricks/<endpoint>`、`azure/<deployment>` 等前缀由 litellm 按各自协议发。走代理或自定义 CA 靠标准环境变量：`HTTPS_PROXY`、`NO_PROXY`、`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`。

**在哪里配。** 在**仓库根目录**放 `.env`；Notebook 启动时会自动切到仓库根并加载它（第一个 cell 的输出里会打印读到的 `.env` 路径）。已存在的系统环境变量优先于 `.env`（`.env` 不覆盖它们）。`.env` 只在 Notebook 内核启动时读一次，改完后要**重启内核**再运行。

```bash
cp .env.example .env              # macOS / Linux
# Windows PowerShell：Copy-Item .env.example .env
```

`.env.example` 里默认就是本机 Ollama 的那一组；保持**一组**配置生效，其余留注释。变量含义见上文「最小配置」和「配置变量」，下面是三种常用写法（`SUPERINDEX_INDEX_MODEL` 只在 `INDEX_SUMMARY=True` 时才用到，不开摘要可不写）。

本机 Ollama（离线）：先 `ollama pull qwen2.5:7b`，并保持 Ollama 服务在运行；上下文建议调大（`OLLAMA_CONTEXT_LENGTH=32768`，见上文）。

```ini
SUPERINDEX_CHAT_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_INDEX_MODEL=ollama_chat/qwen2.5:7b
SUPERINDEX_BASE_URL=http://localhost:11434
SUPERINDEX_API_KEY_OVERRIDE=ollama
SUPERINDEX_REASONING_EFFORT=
```

OpenAI 兼容的自建 / 内网端点（vLLM、LM Studio、公司网关等）：地址带 `/v1`。推荐写法是 `OPENAI_*` 三个变量（`OPENAI_MODEL` 非空才启用，模型名自动加 `openai/` 前缀；优先级见上文「最小配置」，Notebook 里 `LLM_CONFIG` 填这三个也可以）：

```ini
OPENAI_MODEL=your-model-name
OPENAI_BASE_URL=https://your-gateway.example.com/v1
OPENAI_API_KEY=your-gateway-key
SUPERINDEX_REASONING_EFFORT=
```

等价的 `SUPERINDEX_*` 写法（模型名要自己写 `openai/` 前缀）：

```ini
SUPERINDEX_CHAT_MODEL=openai/your-model-name
SUPERINDEX_INDEX_MODEL=openai/your-model-name
SUPERINDEX_BASE_URL=https://your-gateway.example.com/v1
SUPERINDEX_API_KEY_OVERRIDE=your-gateway-key
SUPERINDEX_REASONING_EFFORT=
```

云端（以 DeepSeek / OpenAI 为例）：key 用 LiteLLM 约定的变量名，不必设 `SUPERINDEX_BASE_URL`。

```ini
# DeepSeek
DEEPSEEK_API_KEY=sk-...
SUPERINDEX_CHAT_MODEL=deepseek/deepseek-chat
# OpenAI
# OPENAI_API_KEY=sk-...
# SUPERINDEX_CHAT_MODEL=gpt-5
```

Azure OpenAI、Anthropic 等写法见 `.env.example`。使用云端模型时，题目和检索到的文档片段会发送给模型服务商；要求数据不出本机，请用 Ollama 或内网端点。

**对模型的要求。** 必须支持 tool calling（function calling），问答 agent 靠它调用 `get_document_structure` / `get_page_content` / `search_pages` / `calculate`。仓库里实际用 `ollama_chat/qwen2.5:7b` 在本机 Ollama 上验证过；qwen3、llama3.1 等是 Ollama 里带工具支持的模型，未在本仓库逐一验证。7B 级小模型常不主动调用 `search_pages`，只靠目录树导航（默认开启的检索预取会把候选页放在题目前面，缓解这一点）。

**`SUPERINDEX_REASONING_EFFORT`。** 非推理模型和 Ollama 必须**留空**（或不写），否则报 `does not support thinking`（`ollama_chat`）/ `UnsupportedParamsError`（Ollama 的 `/v1` 路由）。只有确定会推理的云端模型才设，如 `low`。

**确认配好了。** 最快的自检：用仓库自带样例建一个不调模型的库，再问一题（命令从仓库根目录运行；`.env` 已配好时不需要前缀）。

```bash
uv run superindex index samples/aia_ar2021_excerpt.md --no-summary --store results/check_store
uv run superindex ask "2021 年的全年股息是多少？" --store results/check_store
# 不想写 .env、只想临时试一下云端模型：
#   macOS / Linux：SUPERINDEX_CHAT_MODEL=deepseek/deepseek-chat uv run superindex ask "..." --store results/check_store
#   Windows PowerShell：$env:SUPERINDEX_CHAT_MODEL="deepseek/deepseek-chat"; uv run superindex ask "..." --store results/check_store
```

能流式输出带 `146.00` 的回答就说明模型、地址和 tool calling 都通了。也可以在 Notebook 里设 `LIMIT=1`（或环境变量 `SI_NB_LIMIT=1`）只跑一题。

**常见报错。**

- Notebook 步骤 4 报 `RuntimeError: 未配置可用的问答模型：No chat model configured. Set SUPERINDEX_CHAT_MODEL in .env (looked in the working directory …) …`：没读到 `SUPERINDEX_CHAT_MODEL`（或 `OPENAI_MODEL`；只设 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 不算）。检查 `.env` 是否在仓库根、变量名是否拼对，改完重启内核。
- `INDEX_SUMMARY=True` 时步骤 2 报 `No index model configured. Set SUPERINDEX_INDEX_MODEL …`：补上 `SUPERINDEX_INDEX_MODEL`（用 `OPENAI_MODEL` 时不必，会同用它），或把 `INDEX_SUMMARY` 改回 `False`。
- `does not support thinking` / `UnsupportedParamsError`：`SUPERINDEX_REASONING_EFFORT` 留空。
- Ollama 回答很怪、像没看到文档：先用 `ollama ps` 看 CONTEXT 列，默认上下文过小时长提示会被截断，只在 Ollama 服务端日志里有警告。

**Notebook 配置项速查**（都在第一个配置 cell，也可用 `SI_NB_<名字>` 环境变量覆盖，布尔值写 `1/true/yes/on`）：

| 配置项 | 环境变量 | 默认 | 含义 |
|---|---|---|---|
| `GIT_PULL` | `SI_NB_GIT_PULL` | `True` | 开头是否 `git pull` / clone；`0` / `false` / `no` / `off` 关闭（不执行任何 git 命令） |
| `REPO_URL` | `SI_NB_REPO_URL` | `https://github.com/VoldemortGin/SuperIndex.git` | 不在仓库里时 clone 的地址（公司内网有镜像时改这里；输出里 `user:token@` 会打码） |
| `REPO_BRANCH` | `SI_NB_REPO_BRANCH` | `main` | clone 的分支 |
| `REPO_DIR` | `SI_NB_REPO_DIR` | `<当前目录>/SuperIndex` | 不在仓库里时 clone 到哪里；已存在但不是 SuperIndex 仓库则跳过、不会动它 |
| `DATASET_PATH` | `DATASET_PATH`（写在 `.env`，优先）/ `SI_NB_DATASET` | `data/questions.jsonl` | 题集（`.json` / `.jsonl`） |
| `PDF_DIR` | `SI_NB_PDF_DIR` | `data/pdfs` | 原始 PDF 目录（递归） |
| `FIELD_MAP` | `SI_NB_FIELD_MAP` | `id/question/expected/doc` 同名 | 数据集字段名映射，值为 JSON 对象；支持嵌套路径，见下 |
| `PDF_EXTRACTOR` | `SI_NB_PDF_EXTRACTOR` | `text-layer` | `text-layer`（离线）或 `azure-di` |
| `ADD_HEADINGS` | `SI_NB_ADD_HEADINGS` | `True` | 仅 `text-layer`：离线补 `#` 章节标题，不调 LLM |
| （无） | `PDF_INGEST_PASSWORD` | 空 | 需要密码才能打开的 PDF 的**主密码**，写在 `.env` 里，原样当作一个密码，总是第一个尝试 |
| `PDF_PASSWORDS` | `SI_NB_PDF_PASSWORDS` | `[]` | 其它候选密码（排在主密码之后按顺序尝试）；环境变量值写 JSON 数组 `'["pw1", "pw,2"]'`，非 JSON 时整串当单个密码 |
| `INDEX_SUMMARY` | `SI_NB_INDEX_SUMMARY` | `False` | `True`：建库时用 `SUPERINDEX_INDEX_MODEL` 写摘要 |
| `LIMIT` | `SI_NB_LIMIT` | 全部 | 只跑前 N 题 |
| `TIMEOUT` | `SI_NB_TIMEOUT` | `300` | 每题超时（秒） |
| `CONCURRENCY` | `SI_NB_CONCURRENCY` | `1` | 并发题数 |
| `RESUME` | `SI_NB_RESUME` | `True` | 续跑最近一次运行目录，跳过已成功的题 |

**带密码的 PDF。** 主密码写进 `.env`：`PDF_INGEST_PASSWORD='你的密码'`（含 `#`、空格等字符时用单引号括起来）；少数用别的密码的，再在 `PDF_PASSWORDS` 里配候选密码：不加密、或打开密码为空的 PDF 照常处理，需要密码的 PDF 按顺序逐个尝试，密码都不对（或没配密码）的、以及没有文字层的 PDF 会**跳过并标记**（`skipped_wrong_password` / `skipped_no_text_layer`，登记在 `skipped_pdfs.json`），不中断其它文档；指向它们的题不会去问模型。`skipped_wrong_password` 每次运行都会重新尝试，改对密码后直接重跑即可。密码不要写进 Notebook：Databricks 上用 `PDF_PASSWORDS = [dbutils.secrets.get(scope="<scope>", key="<key>")]`，命令行用 `SI_NB_PDF_PASSWORDS`。解密只在内存里进行、不生成解密副本，落盘的只有抽取出的 Markdown（明文，目录权限要按原 PDF 的密级管理）；需要密码的 PDF 不支持页面截图。

另有 `RECORDS_KEY`、`FORCE_EXTRACT`、`PERSIST_DIR`（Databricks 持久目录，见上）、`PREFETCH`、`PREFETCH_K`，同样可用 `SI_NB_*` 覆盖（`WORK_DIR` 例外，读 `NB_WORK_DIR`），见 Notebook 配置 cell。

**`FIELD_MAP` 的嵌套路径。** 值除了顶层字段名，还可以写点号路径（`meta.source`）和 `[]` 列表展开（`evidence[].file` 取每个元素的 `file`，去重保序）。另有可选键 `pages`（如 `evidence[].page`），用于按页码判定检索命中。例如题集顶层是 `{"questions": [...]}`，每题有 `question`、`ground_truth`、`evidence: [{file, page, quote}]`：

```python
FIELD_MAP = {"id": "id", "question": "question", "expected": "ground_truth",
             "doc": "evidence[].file", "pages": "evidence[].page"}
```

命令行执行时用环境变量传 JSON 字符串，如 `SI_NB_FIELD_MAP='{"id": "id", "question": "question", "doc": null}'`（PowerShell 里用 `$env:SI_NB_FIELD_MAP='{"id": "id", "question": "question", "doc": null}'`）。注意：把 `doc` 映射到标准证据文档，会把检索限定在正确文档内，评测成绩偏乐观；想评估全库检索，就把 `doc` 设为 `None`（JSON 里写 `null`）。

**加密 PDF。** 部分 PDF 是 AES 加密的，PyPDF2 需要 `pycryptodome` 才能读文字层；`uv sync --group notebook` 已包含它。缺失时这些 PDF 会在抽取汇总表里标为 failed 并提示安装。

**已知局限（文字层方式）。** 读不到扫描页（如年报附件里的审计报告扫描件，会记为空页并给出警告）；页眉页脚会混入正文；补标题有噪声（表头、脚注序号可能被当成标题）。需要更高质量时把 `PDF_EXTRACTOR` 改成 `azure-di`。

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
| `OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY` | OpenAI 兼容端点的另一种写法：`OPENAI_MODEL` 非空才启用，自动加 `openai/` 前缀并作为问答模型（及未设 `SUPERINDEX_INDEX_MODEL` 时的摘要模型），`OPENAI_BASE_URL` / `OPENAI_API_KEY` 填入未设的 `SUPERINDEX_BASE_URL` / `SUPERINDEX_API_KEY_OVERRIDE`；`SUPERINDEX_*` 优先；nav 不认 |
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
uv run superindex --help                  # 或：uv run python scripts/si.py --help
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
│   │   └── registry.py, policy.py, debuglog.py, suggest.py   # 目录注册/监视、路由策略、调试日志、示例问题
│   ├── webapp/               # 网页服务与 static/：server.py（serve）、nav_server.py（nav-serve）
│   └── bm25.py, prefetch.py, calc.py, page_images.py, batch.py, ...
├── config/routing_policy.yaml  # nav 路由策略（业务知识：目录排除/权重/别名等）
├── scripts/                  # 实验与辅助脚本（si.py 源码入口、06_azure_extract.py、07_logs.py 等；一律 uv run python scripts/<name>.py 运行）
├── samples/                  # 样例 Markdown 与题集（test_corpus/ 为 nav-serve 默认投放区）
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
uv run python scripts/06_azure_extract.py ./pdfs --check --only AR2022
# 把 PDF 抽成 Markdown 落盘，再建库
uv run python scripts/06_azure_extract.py ./pdfs --out ./corpus_md
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

`superindex.nav.build` 也可直接吃 PDF（启动时打印所用抽取器，`--extractor {auto,azure-di,text-layer}` 可强制指定）：配置了 Azure DI 时得到带标题的章节树；否则读 PDF 文本层，用 `superindex.engine.flash`（按字号、位置等版面统计离线识别标题，不调 LLM）建章节树，失败再退到每页一个节点（PyInstaller 打包版不含 flash，直接走每页一节点）。

模型：nav 有自己的一套取值，只借用 `SUPERINDEX_CHAT_MODEL`。模型取 `--model` > `NAV_MODEL` > `SUPERINDEX_CHAT_MODEL` > 兜底 `deepseek/deepseek-flash`；推理强度取 `route --effort` > `NAV_REASONING_EFFORT` > 默认 `none`（`build` 没有 `--effort`，只读环境变量）；用网关 / Ollama 上的非推理模型时要设 `NAV_REASONING_EFFORT=`（空值即不发送），否则 LiteLLM 会报 `UnsupportedParamsError`。它**不读** `OPENAI_MODEL`（也就不认上面那组 `OPENAI_*` 映射）和 `SUPERINDEX_BASE_URL` / `SUPERINDEX_API_KEY_OVERRIDE`，网关地址与 key 要用 LiteLLM 自己的变量（如 `OPENAI_API_BASE` / `OPENAI_API_KEY`）。详见 [superindex/nav/README.md](https://github.com/VoldemortGin/SuperIndex/blob/main/superindex/nav/README.md)。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/VoldemortGin/SuperIndex/main/docs/diagrams/addressing-funnel-dark.png">
  <img alt="一个问题逐级下降：L0 在一个提示词里从整棵目录树中选 1–4 个目录；L1 在这些目录下选 1–5 个文件；L2 对每个候选文件并行选 1–6 个章节；最后按相关度拼接章节原文与出处，上限 20,000 字符。" src="https://raw.githubusercontent.com/VoldemortGin/SuperIndex/main/docs/diagrams/addressing-funnel.png">
</picture>

更完整的图解（问题背景、建库、问答流程、测试）见 [docs/ArchitectureIntro.html](https://github.com/VoldemortGin/SuperIndex/blob/main/docs/ArchitectureIntro.html)（单文件 HTML，克隆仓库后用浏览器本地打开）。

### 目录驱动 Web UI：`nav-serve`

基于两级导航的浏览器界面：注册一个或多个目录，后台建索引，然后按目录提问。标准库 HTTP 服务 + 单个 HTML，无需前端构建。与 `serve`（基于 `superindex_store/` 文档库的 agent 问答）相互独立。

```bash
superindex nav-serve                          # http://127.0.0.1:8787
superindex nav-serve --port 9000 --no-watch   # 换端口、不轮询文件变化
superindex nav-serve --watch-interval 10      # 每 10 秒检查一次（默认 30）
uv run python scripts/si.py nav-serve         # 源码仓库里（或 uv run python -m superindex.webapp.nav_server）
```

**目录与默认位置**

| 内容 | 默认位置 | 覆盖 |
|---|---|---|
| 投放区：每个直接子目录自动注册为一个语料 | 源码仓库的 `samples/test_corpus/`（中国太保 / 中国平安 / 友邦保险 / 行业汇总，开箱即可演示）；pip / 打包环境下不存在则不自动发现 | `SUPERINDEX_DATA_DIR` |
| 注册表与索引 | `<store>/nav/registry.json`、`<store>/nav/corpora/<id>/`（`<store>` 即 `SUPERINDEX_STORE`，默认工作目录下 `superindex_store/`，打包版为 exe 所在目录） | `SUPERINDEX_INDEX_DIR` |
| 调试日志 | `results/logs/queries.jsonl`、`errors.jsonl`（相对工作目录 / exe 目录） | `SUPERINDEX_LOG_DIR` |

- 投放区以外的目录用界面上的「添加目录」注册（只读目录浏览器）；源目录只读，索引只写到上表位置，注册表记录源目录的绝对路径。项目代码树内、投放区之外的目录不允许注册。
- **增量建索引 + 自动跟进**：每个语料独立的索引目录；大小与 mtime 未变的文件不重新抽取，只给缺描述的文件补摘要；watcher 轮询目录，增删改文件后只重做变化的部分。目录消失时语料标为 `error`。
- **按目录提问**：默认在所有已建好的语料中提问，也可勾选子集；多语料合并为一棵树一次路由。支持 `?q=<问题>` 深链接。
- **问答过程可见、可取消**：SSE 流式推送路由策略、各阶段进度、导航路径（选了哪些目录/文件/章节、是否走了兜底）、本次 LLM 调用统计、出处（按文档分组）、模型思考过程与答案；生成中「发送」变为「停止」，取消记为 cancel 而非错误。
- **示例问题与历史问题**：输入框上方的示例问题由语料本身（描述、目录主题、文件摘要、章节标题）生成并缓存；聚焦输入框时下拉显示最近问过的问题。
- **性能与健壮性**：章节选择按候选文件并行；启动时预热 litellm（本地价格表 + `llm.warmup()`），避免首个问题多等数秒；LLM 回复被截断（`finish_reason=length`）或为空时自动加大 token 预算重试、流式失败退回非流式；路由兜底支持别名扩展，并可利用语料级摘要。
- 回答参数：`SUPERINDEX_REASONING_EFFORT`（默认 `low`）、`SUPERINDEX_ANSWER_MAX_TOKENS`（默认 4096）；后台建索引并发 `SUPERINDEX_INDEX_WORKERS`（默认 6）。路由/摘要模型同上文 nav 的取值（`NAV_MODEL` > `SUPERINDEX_CHAT_MODEL` > `deepseek/deepseek-flash`）。

接口（界面能做的都可以脚本化）：`GET /api/state`、`/api/browse?path=`、`/api/logs?kind=queries|errors&limit=N&failed=1`、`/api/recent-questions`、`/api/corpora/<id>/tree`、`/api/health`；`POST /api/corpora`（`{path, name?, deep_index?}`）、`/api/corpora/<id>/reindex`（`{deep_index?, force?}`）、`/api/ask`（`{question, corpus_ids?}` → SSE 事件 `policy` / `stage` / `nav` / `llmstats` / `sources` / `thinking` / `answer` / `error` / `done`）；`PATCH /api/corpora/<id>`（`{name}` 改名）、`DELETE /api/corpora/<id>`（注销并删除索引）。

### 路由策略：`config/routing_policy.yaml`

"年报在 `annual/` 下""`_drafts/` 永远不搜""太保 = 中国太保"这类业务知识不写进代码，而写进 `config/routing_policy.yaml`：`directories.exclude`（唯一的硬过滤，按目录名整段匹配）、`weights`（加分并在提示词中标注）、`scopes`（纯提示）、`periods`（如 `FY24` → 2024）、`aliases`（兜底匹配时的同义词扩展）。

- **空策略 = 内置默认行为**；写错（如 YAML 语法错误）不会中断服务，退回默认并记到 `errors.jsonl`（`where: policy.load`）。
- **每次提问重新读取**，改完无需重启。每条查询日志都记录当时生效的策略。
- 查找顺序：`SUPERINDEX_ROUTING_POLICY` → 工作目录 `config/routing_policy.yaml` → exe 所在目录 `config/` → 随代码发布的那份（源码仓库根 / PyInstaller 包内）。
- 命令行调试：`uv run python -m superindex.nav.route <index_dir> "问题" --policy ./my_policy.yaml --corpus <语料名> --show-policy`（`--show-policy` 打印生效策略后退出）。

### 调试日志

每个问题都会记录，答错之后可以事后排查而不是猜：

| 文件 | 内容 |
|---|---|
| `queries.jsonl` | 每题一条：范围、每一步路由决策、读了哪些出处、答案、各阶段耗时、每次 LLM 调用明细（`llm_calls`：阶段、耗时、每次尝试耗时、是否重试、提示词大小；汇总 `llm_n_calls` / `llm_n_retried` / `llm_ms`） |
| `errors.jsonl` | 每个异常一条：类型、消息、完整 traceback、当时的上下文，与查询共用 id |

`llm_n_retried` 非零说明部分耗时花在了恢复而不是干活上；`llm_ms` 是各调用耗时之和（章节选择并行），应与 `stages` 对照而不是与总耗时对照。

```bash
uv run python scripts/07_logs.py                  # 最近的查询，每条一行
uv run python scripts/07_logs.py --failed         # 只看失败或没找到内容的
uv run python scripts/07_logs.py --id q-1a2b3c4d  # 单个查询全貌及其异常
uv run python scripts/07_logs.py --kind errors    # 最近的异常
uv run python scripts/07_logs.py --stats
```

`SUPERINDEX_DEBUG_LOG=0` 关闭记录，`SUPERINDEX_LOG_MAX_BYTES` 控制轮转大小。写日志失败只在 stderr 打一行，不影响服务。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/VoldemortGin/SuperIndex/main/docs/diagrams/retrieval-sequence-dark.png">
  <img alt="检索流程：读取 config/routing_policy.yaml；L0 选目录、L1 选文件（各一次模型调用，失败时按路径/名称与摘要关键词打分兜底）；L2 每个候选文件一次调用并行选章节（兜底为章节标题关键词打分）；本地拼接上下文；流式生成答案（含思考过程）。每一步都追加记录到 results/logs/queries.jsonl。" src="https://raw.githubusercontent.com/VoldemortGin/SuperIndex/main/docs/diagrams/retrieval-sequence.png">
</picture>
