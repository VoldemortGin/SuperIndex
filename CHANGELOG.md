# Changelog

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
