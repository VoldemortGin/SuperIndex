# Windows 源码运行指南（不用打包的 exe）

直接用 Python 源码运行 `superindex`：建库（`index`）、检索自检（`search`）、问答（`ask`）、网页（`serve`）、批量问答（`batch`）。
以下命令都在 **PowerShell** 里、**仓库根目录**执行。想要免 Python 的可执行程序，见 [`packaging/README.md`](../packaging/README.md)。

## 最快上手（公司电脑）

1. `git clone https://github.com/VoldemortGin/SuperIndex.git`，`cd SuperIndex`
2. `py -3.12 -m venv .venv`，`.\.venv\Scripts\Activate.ps1`
3. `pip install -r packaging\requirements-bundle.txt`
4. `Copy-Item .env.example .env`，按 [§3b](#3b-使用公司云端-llm-api非-ollama) 填公司 API（有企业代理/自签证书时一并配好）
5. 连通自检：§3b 末尾的一行 litellm 调用输出 `OK`
6. 建库（先不调 LLM）：`python -m superindex index D:\corpus_md --no-summary`（DI 产出的 Markdown 目录）
7. 检索自检：`python -m superindex search "final dividend" --top-k 3`
8. 对比两种匹配：`python -m superindex batch D:\q.jsonl --retrieval-only --match page`，再跑一次 `--match passage`，比较两份 `summary.md`
9. 问答：`python -m superindex ask "..." -v`，或 `python -m superindex serve --port 8787`
10. 端到端：`python -m superindex batch D:\q.jsonl --concurrency 2`；满意后再去掉 `--no-summary` 带摘要重建（`--force --concurrency 2`）

## 1. 前置条件

- **Python 3.11 或 3.12**（64 位，python.org 安装包，安装时勾选 *Add python.exe to PATH*；锁定的依赖只在这两个版本验证过，3.10 装不上）。`py -3.12 --version` 能输出版本即可。
- **Git**（`git --version`）。
- **Ollama**（用公司/云端 API 时不需要，见 §3b）已安装并在运行，且已拉好**支持 tool calling** 的模型（qwen2.5 / qwen3 / llama3.1 …）：
  ```powershell
  ollama pull qwen2.5:7b
  setx OLLAMA_CONTEXT_LENGTH 32768     # 调大上下文；执行后退出并重启 Ollama
  ollama ps                            # 问过一次后看 CONTEXT 列是否为 32768
  ```

## 2. 获取代码与安装依赖

```powershell
git clone https://github.com/VoldemortGin/SuperIndex.git
cd SuperIndex
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r packaging\requirements-bundle.txt
python -m superindex --help
```

- `requirements-bundle.txt` 是打包用的锁定版本清单（含 PyInstaller，源码运行用不到但装上无害）。
- `PageIndex/` 已随仓库分发，**不需要** `pip install -e PageIndex`：从源码运行时 `superindex` 会自动使用仓库内的 `PageIndex/`。
- 之后每次打开新的 PowerShell：`cd` 到仓库根目录，再执行 `.\.venv\Scripts\Activate.ps1`。

## 3. 配置 `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

至少保证以下几行（默认模板已是本机 Ollama）：

```
PAGEINDEX_INDEX_MODEL=ollama_chat/qwen2.5:7b
PAGEINDEX_CHAT_MODEL=ollama_chat/qwen2.5:7b
PAGEINDEX_BASE_URL=http://localhost:11434
PAGEINDEX_API_KEY_OVERRIDE=ollama
PAGEINDEX_REASONING_EFFORT=
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `PAGEINDEX_CHAT_MODEL` | 是 | 问答模型（`ask` / `serve` / `batch`） |
| `PAGEINDEX_INDEX_MODEL` | 开摘要建库时必填 | 生成节点摘要的模型；`--no-summary` 时不用 |
| `PAGEINDEX_BASE_URL` | 用 Ollama 时必填 | Ollama 地址 |
| `PAGEINDEX_API_KEY_OVERRIDE` | 否 | Ollama 不校验，随便填 |
| `PAGEINDEX_REASONING_EFFORT` | 必须留空 | 非推理模型收到该参数会报 "does not support thinking" |
| `SUPERINDEX_STORE` | 否 | 文档库位置，默认 `results\superindex_store\` |

## 3b. 使用公司/云端 LLM API（非 Ollama）

只改 `.env` 的模型几行，其余命令不变。`.env.example` 里有同样的注释模板。**模型必须支持 tool calling**（问答靠工具调用读页面）。

**OpenAI 兼容网关**（公司自建网关、vLLM、各类代理；注意 `/v1` 后缀）：

```
PAGEINDEX_INDEX_MODEL=openai/<模型名>
PAGEINDEX_CHAT_MODEL=openai/<模型名>
PAGEINDEX_BASE_URL=https://<网关地址>/v1
PAGEINDEX_API_KEY_OVERRIDE=<你的 key>
PAGEINDEX_REASONING_EFFORT=
```

**Azure OpenAI**（`azure/` 后面是**部署名**，不是模型名；终结点/密钥/版本用 litellm 自己的变量，`PAGEINDEX_BASE_URL` 与 `PAGEINDEX_API_KEY_OVERRIDE` 留空）：

```
PAGEINDEX_INDEX_MODEL=azure/<部署名>
PAGEINDEX_CHAT_MODEL=azure/<部署名>
AZURE_API_BASE=https://<资源名>.openai.azure.com/
AZURE_API_KEY=<你的 key>
AZURE_API_VERSION=<门户里给的 api-version，如 2024-10-21>
```

- 若设置了 `PAGEINDEX_BASE_URL` / `PAGEINDEX_API_KEY_OVERRIDE`，它们会覆盖 `AZURE_API_BASE` / `AZURE_API_KEY`（Azure 两种写法都能用，二选一即可，避免混用）。
- `PAGEINDEX_REASONING_EFFORT`：只对支持的推理模型（如 o 系列、gpt-5 系列）设 `low`；普通模型留空，否则会报参数不支持。
- **企业代理 / 自签证书**：在 `.env` 或 PowerShell 里设置
  `HTTPS_PROXY=http://<代理>:<端口>`（内网网关不走代理时加 `NO_PROXY=<网关域名>`），
  `SSL_CERT_FILE=D:\certs\corp-ca.pem` 与 `REQUESTS_CA_BUNDLE=D:\certs\corp-ca.pem`（公司根证书，PEM 格式）。报 `CERTIFICATE_VERIFY_FAILED` 基本就是这一项。
- **限流**：公司 API 常有 QPS/TPM 限制。带摘要建库用 `--concurrency 2`（默认 8），`batch` 也用 `--concurrency 1~2`；遇到 429 先降并发。
- **顺序**：先 `index --no-summary` + `search` 自检（不调 LLM），再 `ask` 跑通，最后才开摘要建库。
- `.env` 已在 `.gitignore` 中，不要把 key 写进其他会提交的文件。

连通自检（只打印模型回复，不打印 key；应输出 `OK` 之类）：

```powershell
python -c "from superindex.runtime import load_env, configure_litellm, LLMSettings; load_env(); configure_litellm(); import litellm; s = LLMSettings.resolve(); print(litellm.completion(model=s.require('chat'), messages=[{'role': 'user', 'content': 'Reply with OK'}], max_tokens=5, num_retries=0, **(s.index_backend() or {})).choices[0].message.content)"
```

通了之后再用样例走一遍工具调用：`python -m superindex index samples\aia_ar2021_excerpt.md --no-summary`，然后 `python -m superindex ask "2021 年末期股息是多少？" -v`。

## 4. 建库（index）

Markdown（Azure DI 产出，或任意 `.md`）放哪都行，建议放仓库外的短路径，如 `D:\corpus_md\`。

```powershell
python -m superindex index samples\aia_ar2021_excerpt.md --no-summary   # 单个文件
python -m superindex index D:\corpus_md --no-summary                     # 整个目录（递归）
python -m superindex index D:\corpus_md                                  # 带 LLM 摘要
python -m superindex index D:\corpus_md --force                          # 强制重建
python -m superindex index D:\corpus_md --store D:\si_store              # 指定文档库位置
```

- `--no-summary`：不调 LLM，几秒建完，问答时模型靠目录树 + 关键词检索定位页面。先用它跑通。
- 开摘要：每个节点调一次模型，文档多时要很久，换来目录导航更准；`--concurrency` 控制并发（默认 8，本机小模型可调低）。
- 同名文件内容未变会跳过；`--force` 重建。`ask`/`search`/`serve`/`batch` 若用了 `--store`，也要带同一个 `--store`。

## 5. 自检与问答

```powershell
python -m superindex search "final dividend 2021" --top-k 3        # 关键词检索，不调 LLM
python -m superindex ask "2021 年末期股息是多少？" -v                # -v 打印工具调用
python -m superindex ask "..." --doc aia_ar2021                      # 限定文档（名称片段即可）
python -m superindex serve --port 8787                               # 浏览器打开 http://127.0.0.1:8787
```

`search` 能搜到页面，说明建库正常；`ask` 若答非所问，先看 `-v` 输出的工具调用是否读到了正确页码。

## 6. 批量问答（batch）

```powershell
python -m superindex batch samples\questions_sample.jsonl
python -m superindex batch D:\my_questions.csv --concurrency 2 --timeout 300
python -m superindex batch scripts\questions_3docs.json --doc AIA_AR2021 --limit 5   # 题集 doc 与库内文档名不一致时用 --doc 统一指定
python -m superindex batch D:\my_questions.csv --resume                 # 续跑最近一次，跳过已完成的题
```

题集格式：

- `.txt`：每行一题，`#` 开头为注释。
- `.jsonl`：每行 `{"id": "Q1", "question": "...", "expected": "...", "doc": "文档名片段"}`，只有 `question` 必填。
- `.csv`：UTF-8，表头至少有 `question`，可选 `expected`、`doc`、`id`。
- `.json`：`scripts\questions.json` 的格式。

结果在 `results\batch\<时间戳>\`（`--out` 可改）：

- `summary.md`：总览表（命中、耗时、LLM 轮次、读取页码、错误）+ 逐题问题/期望/回答/工具调用。
- `results.jsonl`：逐题完整记录。

“命中”是**粗评分**：期望答案里的数字全部出现在回答中才算命中（没有数字时按整句包含判断），需要人工复核。
`--doc` 会覆盖题集里每题的 `doc`；题集的 `doc` 对不上库里的文档时，该题记为错误，其余照跑。

纯检索评测（不调 LLM，只看每题 BM25 召回的页是否含期望答案，秒级完成）：

```powershell
python -m superindex batch D:\q.jsonl --retrieval-only --match page
python -m superindex batch D:\q.jsonl --retrieval-only --match passage
```

`--match page`（默认）按整页打分；`passage` 按页内小段打分，长页多主题时更好。合成样例上两者总体接近，请在真实 DI 年报题集上各跑一次比较后再决定（`.env` 中 `SUPERINDEX_BM25_MATCH` 可设默认值）。

一键脚本（建库→跑题集→打印 summary 路径）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_batch.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_batch.ps1 -Markdown D:\corpus_md -Questions D:\q.jsonl -Extra "--concurrency","2"
```

## 7. 常见问题

- **`Activate.ps1` 无法加载（ExecutionPolicy）**：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`；或不激活，直接用 `.\.venv\Scripts\python.exe -m superindex ...`。
- **中文乱码**：`superindex` 已把输出设为 UTF-8；控制台仍乱码时先执行 `chcp 65001`，或 `$env:PYTHONUTF8 = "1"`。题集/`.env` 用 UTF-8 保存（记事本“另存为”选 UTF-8）。
- **长路径报错**：仓库和语料放短路径（如 `D:\SuperIndex`、`D:\corpus_md`）；或以管理员执行 `git config --system core.longpaths true` 并在组策略中启用 Win32 长路径。
- **`ModuleNotFoundError: superindex`**：必须在仓库根目录运行 `python -m superindex`。
- **Ollama 连不上**（`Connection refused`）：托盘里确认 Ollama 在运行；`curl.exe http://localhost:11434/api/tags` 应返回模型列表；`PAGEINDEX_BASE_URL` 与之一致；模型名要和 `ollama list` 完全一致（如 `ollama_chat/qwen2.5:7b`）。
- **"does not support tools" / 不调用工具**：换支持 tool calling 的模型（qwen2.5 / qwen3 / llama3.1）；0.5b/1.5b 小模型能跑通流程，但工具参数常填错，答案质量差。
- **"does not support thinking"**：`.env` 里 `PAGEINDEX_REASONING_EFFORT=` 留空。
- **回答明显缺上下文 / 胡编**：多半是上下文被截断。确认 `OLLAMA_CONTEXT_LENGTH` 已生效（`ollama ps` 的 CONTEXT 列），改完要重启 Ollama。
- **单题很慢或卡住**：`batch` 默认每题 300 秒超时，可用 `--timeout` 调整；超时记为错误，`--resume` 可只重跑出错的题。
