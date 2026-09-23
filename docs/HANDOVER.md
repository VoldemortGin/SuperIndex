# 交接文档 — PageIndex × AIA 财报检索项目

> 面向接手同事。读完这份文档 + `README.md` + `nav/README.md`，
> 应该能独立跑起来、看懂每个设计决策的理由、并知道坑在哪。

**代码仓库**：https://github.com/yongsoft/SuperIndex （私有，需授权访问）

```bash
git clone https://github.com/yongsoft/SuperIndex.git
cd SuperIndex
# 仓库里没有 PageIndex/ 和 PDF，先按 README「§0」把它们拉下来
```

> ⚠️ 仓库**不包含** `.env`（含密钥）、`PageIndex/`（上游克隆）、
> `data/*.pdf`、`results/` 下的生成物。README §0 有获取步骤。

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
| 查询延迟诊断 | ✅ | `scripts/profile_query.py` |
| Markdown 格式审计 | ✅ | `scripts/04_md_audit.py` |
| 跨文档重复度诊断 | ✅ | `scripts/05_similarity_probe.py` |

### 未完成的

- **10 份 AIA 报告只索引了 3 份**（1H2021 / FY2021 / FY2022），
  其余 7 份未索引。原因见第五节「为什么中途停了」。
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

`PageIndex/` 是**上游仓库的克隆**，不是本项目代码：

```
仓库:   https://github.com/VectifyAI/PageIndex.git
commit: 71714e8
状态:   干净（无本地改动）
```

它是**以 editable 模式装进 venv** 的（`pip install -e PageIndex`），
所以：

- 改 `PageIndex/` 里的代码**会立即生效**，不用重装
- 但**不要改它** —— 我们所有定制都通过 monkeypatch 或外层脚本完成，
  保持上游干净，将来 `git pull` 才不会冲突

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
├── PageIndex/                 上游克隆，editable 安装，勿改
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
│   ├── monitor.sh              长任务的进度记录
│   ├── questions.json          20 题，覆盖全部 10 份
│   └── questions_3docs.json    18 题，只覆盖已索引的 3 份
│   └── ⚠️ 01_build_index.py / 02_qa.py 是早期版本，已被上面两个取代，可删
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

### 5.6 文档（新增/更新）

- `README.md` — 补了 Web UI、当前语料状态、题集说明
- `nav/README.md` — nav 包完整文档
- `docs/dify-improvement-plan.md` — Dify 改造方案

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

# 5. 启动 Web 界面
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

### 打包建议

| 目录 | 建议 | 理由 |
|---|---|---|
| `PageIndex/` | 保留，或改为让对方自己 `git clone` | 58 MB；是上游代码，不是我们的 |
| `data/aia_reports/` | 保留 | 27 MB，公开年报；`download.sh` 里有原始 URL |
| `results/` | **只保留 `pageindex_store/` 和 `trees/`**，日志可删 | 索引重建要花钱，日志没用 |
| `.env` | **排除** | 含密钥 |
| `.workbuddy-ai/` | 保留 | 项目记忆，含大量决策记录 |
| `.DS_Store` | 删除 | macOS 垃圾文件 |

### 已完成 / 待办

✅ **已建 git 仓库并推送到 GitHub**：https://github.com/yongsoft/SuperIndex（私有）
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
