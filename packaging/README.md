# superindex — 打包与离线部署

`superindex` 把 Azure DI 产出的 Markdown 建成 PageIndex 文档库，并提供问答（`ask`）和网页界面（`serve`）。
这里的脚本把它打成**不需要 Python** 的可执行程序，面向离线 Windows 服务器 + 本机 Ollama。
不打包、直接用 Python 源码运行：见 [`docs/windows-quickstart.md`](../docs/windows-quickstart.md)。

## 1. 在联网的 Windows 电脑上打包

前提：Python 3.11 或 3.12（64 位，python.org 安装包或 `py` 启动器均可）、本仓库完整代码。

```powershell
# 在仓库根目录
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# 单文件版（见下文取舍）
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -OneFile
```

脚本会：检查 Python → 建 `build\venv-bundle` → 安装 `packaging\requirements-bundle.txt`（锁定版本）
→ PyInstaller → 冒烟测试（`--help`、`index --no-summary`，均在代理指向无效地址的"断网"环境下）
→ 生成 `dist\superindex-windows-x64.zip`。

macOS 上同等流程：`bash packaging/build_macos.sh`（只能产出 macOS 版，不能交叉编译 Windows）。

**onedir（默认）还是 onefile**：默认 onedir（`superindex\superindex.exe` + `_internal\`）。
启动快（onefile 每次启动都要把约 90 MB 解压到 `%TEMP%`，本机实测 `--help` 约 12 秒 vs onedir 0.1 秒）、杀软误报少、`%TEMP%` 受限的服务器上也能跑。
只有"必须是单个 exe"时才用 `-OneFile`。

## 2. 离线部署

1. 把 zip 拷到服务器，解压到一个**短路径**（如 `D:\superindex\`），避免 260 字符路径限制。
2. 在 `superindex.exe` 同目录把 `.env.example` 复制为 `.env` 并按需修改（默认已是本机 Ollama）：
   ```
   PAGEINDEX_INDEX_MODEL=ollama_chat/qwen2.5:7b
   PAGEINDEX_CHAT_MODEL=ollama_chat/qwen2.5:7b
   PAGEINDEX_BASE_URL=http://localhost:11434
   PAGEINDEX_API_KEY_OVERRIDE=ollama
   PAGEINDEX_REASONING_EFFORT=
   ```
   `PAGEINDEX_REASONING_EFFORT` 必须留空：非推理模型收到该参数会报 "does not support thinking"。
   `.env` 的查找顺序：当前工作目录 → exe 所在目录；已有的环境变量优先。
3. Ollama（离线）：
   - 在联网机器下载 Windows 离线安装包（`OllamaSetup.exe`）拷过去安装。
   - 模型：在联网机器 `ollama pull qwen2.5:7b`，把整个模型目录（默认 `%USERPROFILE%\.ollama\models`，
     可用 `OLLAMA_MODELS` 改）拷到服务器同一位置，`ollama list` 确认可见。模型需支持 tool calling（qwen2.5 / qwen3 / llama3.1 …）。
   - 调大上下文：`setx OLLAMA_CONTEXT_LENGTH 32768`，然后重启 Ollama，用 `ollama ps` 查看 CONTEXT 列。
     默认上下文很小，超长 prompt 会被静默截断，问答质量会明显变差。
4. 文档库默认在 exe 同目录的 `superindex_store\`；可用 `--store` 或 `.env` 里的 `SUPERINDEX_STORE` 改。

## 3. 常用命令

```powershell
superindex.exe --help
superindex.exe index D:\corpus_md                     # 递归索引目录下的 .md（带 LLM 摘要）
superindex.exe index report.md --no-summary           # 不调 LLM，只建目录树
superindex.exe index D:\corpus_md --force             # 重建未改动的文件
superindex.exe ask "2021 年末期股息是多少？"
superindex.exe ask "..." --doc aia_ar2021 -v          # 限定文档，-v 打印工具调用
superindex.exe search "末期股息 2023" --top-k 5       # 关键词(BM25)检索，不调 LLM，便于排查
superindex.exe serve --port 8787                      # 浏览器打开 http://127.0.0.1:8787
superindex.exe serve --host 0.0.0.0 --port 8787       # 局域网访问（注意防火墙）
```

## 4. 已知限制

- **不要并发 index**：文档库的文件锁在 Windows 上不生效，两个 `index` 同时写同一个 store 可能损坏 `manifest.json`。
  `serve` 运行时也尽量不要对同一 store 执行 `index`。
- 只支持 Markdown 输入（PDF 解析栈已从包中剔除）；PDF 需先用 Azure DI 转成 Markdown。
- 杀毒软件可能拦截或隔离新生成的 exe，必要时把解压目录加入白名单。
- 包内 Windows 版只能在 Windows 上构建；Windows 7 / Server 2008 不受支持（Python 3.11+ 要求 Windows 8.1 / Server 2012 R2 以上）。
