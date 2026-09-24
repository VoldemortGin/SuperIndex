# Changelog

## 0.1.2 — 2026-09-24

修正版：参数 schema、工具名、消息形态、store 格式、CLI 参数与 webapp API 不变；变化集中在源码运行方式、发给模型的描述文字与文档。

### 修复
- 源码入口 `scripts/superindex.py` 改名为 `scripts/si.py`：原名与 `superindex` 包同名，运行 `scripts/` 下其他脚本（如 `scripts/01_build_trees.py`）时遮蔽包，报 `'superindex' is not a package`。
  - 源码运行统一为 `uv run python scripts/si.py <子命令>`，实验脚本为 `uv run python scripts/0X_xxx.py`；pip 安装后的 `superindex <子命令>` 不变；
  - Windows pip 兜底步骤需要 `pip install --no-deps -e .`。
- 发给模型的工具描述与提示词：
  - 删除 `browse_documents` 描述及 folder/sort/query 错误返回中 "PageIndex cloud / PageIndexCloudClient" 的残留；`browse_documents` 不再暗示支持 sort/query（总是按时间倒序）；
  - `get_page_content` 描述写明物理页码、页码格式示例与约 95,000 字符的单次上限；新增 `get_document` 本地描述（本地文档均已索引，无需先确认 ready）；
  - 空库 / 索引失败提示改为让用户执行 `superindex index <path>`（`--force` 重新索引）；`get_document` 的 next_steps 与阅读流程一致（≤20 页直接读全部）；
  - `calculate` 描述去掉不支持的比较运算，补上 `^` `×` `÷`；
  - 系统提示中的文档库描述、错误处理规则与重试建议修正；`search_pages` 优先于 `browse_documents` 的指引更明确。

### 文档
- nav 模型配置：`NAV_MODEL` / `NAV_REASONING_EFFORT`，兜底 `deepseek/deepseek-flash`，不读 `SUPERINDEX_BASE_URL`；`.env.example` 中"不回落云端"的说法补上 nav 与部分实验脚本自带默认模型的例外。
- nav PDF 抽取改为 Azure DI → 文本层，不再承诺书签。
- `packaging/README.md` 体积数字按实测更新。
- `docs/windows-quickstart.md`：样例批量题集前需先索引两份样本；`run_batch.ps1` 的 `-Extra` 用法修正。
- `docs/engine/naming-rules.md` 标注为上游参考，清理 cloud SDK / 文件夹相关内容。
- CLI 帮助补默认值；webapp 文档计数改为"已索引 N 份"；engine 过时 docstring 清理。

## 0.1.1 — 2026-09-24

对外行为不变：CLI 参数、环境变量、store 格式、工具名与返回契约、发给模型的请求体、webapp API / SSE 事件与 0.1.0 一致（逐字节回归对比）。

### 变更
- 引擎新增正式扩展点，取代对引擎内部的包装与 monkeypatch：
  - `SuperIndexClient(tools=[AgentTool(...)])`：注册额外的本地 agent 工具（`search_pages`、`calculate` 走此路径），工具的 guidance 由引擎追加到系统提示；
  - `chat(..., stream=True, extras=ChatExtras(...))`：多模态 user 消息、追加指令、额外工具与 `call_model_input_filter`（PDF 页面截图与 `get_page_image` 走此路径）；
  - `SuperIndexClient(page_text_extractor=...)`：替换 PDF 文本层抽取（Azure DI 走此路径）。`superindex.extractors.backend.install_into_pageindex()` 改为 `page_text_extractor()`；
  - `agent_tools.resolve_document` 改为公开函数。
- `superindex/nav/README.md` 删除含真实公司名与失真数字的实测表，改为验证方法说明。

### 移除
- VectifyAI 托管云客户端（`PageIndexCloudClient`、`cloud_api`、云端 managed chat 与仅云方法）与云 MCP bridge（`mcp_bridge`）。
- Anthropic / Claude Agent SDK 集成（`protocol="messages"`、`as_anthropic_tools`、`as_claude_mcp` 等）及 extras `[anthropic]` / `[claude]`。
- 直接依赖 `requests`、`urllib3`（`mcp` 保留：本地工具集经进程内 MCP server 交给 openai-agents）。

## 0.1.0 — 2026-09-24

首个 PyPI 版本。
- `superindex` CLI：`index`（Azure DI Markdown → 树索引，可关联 PDF）、`ask`、`search`（BM25 页级 / passage 检索）、`serve`（Web UI）、`batch`（批量问答与 `--retrieval-only` 纯检索评测）。
- 本地问答 agent：`search_pages` 关键词检索、检索前置（prefetch）、`calculate` 数值计算、`--page-image` PDF 页面截图与 `get_page_image` 工具。
- 引擎 `superindex.engine` 派生自 VectifyAI/PageIndex 0.2.10（MIT，见 NOTICE），另含 `nav` 两级导航检索与 Azure DI 抽取后端。
- 配置通过 `.env` 与 `SUPERINDEX_*` 环境变量（旧 `PAGEINDEX_*` 仍兼容）。
