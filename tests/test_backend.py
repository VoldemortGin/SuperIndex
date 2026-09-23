#!/usr/bin/env python3
"""
Tests for the extraction-backend resolver.

The policy under test: Azure Document Intelligence is the default extractor
whenever `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` are set, and every entry point
takes it up automatically. All offline — the Azure branch is exercised against
a stubbed `_analyze`, so no network and no credentials.

    python tests/test_backend.py
    pytest tests/test_backend.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractors.backend import (  # noqa: E402
    BACKEND_AZURE, BACKEND_TEXT_LAYER, Extractor, describe, fallback_enabled,
    install_into_pageindex, is_azure_configured, reset_cache,
)

PASS, FAIL = [], []
AZ = {"AZURE_DI_ENDPOINT": "https://demo.cognitiveservices.azure.com/",
      "AZURE_DI_KEY": "k"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ------------------------------------------------------------ resolution
def test_resolution() -> None:
    print("\n[后端解析]")
    check("未配置时不是 Azure", not is_azure_configured({}))
    check("只给 endpoint 不算配置",
          not is_azure_configured({"AZURE_DI_ENDPOINT": "https://a.com/"}))
    check("只给 key 不算配置", not is_azure_configured({"AZURE_DI_KEY": "k"}))
    check("两者齐全才算配置", is_azure_configured(AZ))
    check("兼容 AZURE_DOCUMENT_INTELLIGENCE_* 命名",
          is_azure_configured({"AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://a.com/",
                               "AZURE_DOCUMENT_INTELLIGENCE_KEY": "k"}))

    off = describe({})
    check("未配置 -> text-layer", off.name == BACKEND_TEXT_LAYER, off.name)
    check("未配置时提示该设哪两个变量",
          "AZURE_DI_ENDPOINT" in off.detail and "AZURE_DI_KEY" in off.detail)

    on = describe(AZ)
    check("配置后 -> azure-di", on.name == BACKEND_AZURE, on.name)
    check("详情里带主机名与模型",
          "demo.cognitiveservices.azure.com" in on.detail and "prebuilt-layout" in on.detail)

    check("默认 strict（失败即中止）",
          Extractor(AZ).strict and not Extractor({}).strict)
    check("AZURE_DI_FALLBACK=1 时不再是 strict",
          not Extractor({**AZ, "AZURE_DI_FALLBACK": "1"}).strict)
    for v in ("1", "true", "YES", "on"):
        if not fallback_enabled({"AZURE_DI_FALLBACK": v}):
            check(f"fallback 接受 {v!r}", False)
            break
    else:
        check("fallback 接受 1/true/YES/on", True)
    check("fallback 默认关闭", not fallback_enabled({}))


# --------------------------------------------------------- page splitting
def test_azure_page_split() -> None:
    print("\n[按页切分（stub 掉 analyze）]")
    # Build spans from the real string lengths so the offsets can't drift.
    p1, p2a, p2b, p3 = "PAGE-ONE-BODY", "PAGE-TWO-A", "PAGE-TWO-B", "PAGE-THREE-BODY"
    content = p1 + p2a + p2b + p3
    o1 = 0
    o2 = len(p1)
    o3 = o2 + len(p2a) + len(p2b)
    result = {
        "content": content,
        "pages": [
            {"pageNumber": 1, "spans": [{"offset": o1, "length": len(p1)}]},
            # page 2 arrives as two spans — the real API does this when a page
            # is split across content blocks
            {"pageNumber": 2, "spans": [{"offset": o2, "length": len(p2a)},
                                        {"offset": o2 + len(p2a),
                                         "length": len(p2b)}]},
            {"pageNumber": 3, "spans": [{"offset": o3, "length": len(p3)}]},
        ],
    }
    ex = Extractor(AZ)
    ex._analyze = lambda pdf: result          # stub, no network
    pages = ex._azure_page_texts(Path("dummy.pdf"))
    check("页数正确", len(pages) == 3, str(len(pages)))
    check("第 1 页内容正确", pages[0] == p1, repr(pages[0]))
    check("第 2 页合并了多个 span", pages[1] == p2a + p2b, repr(pages[1]))
    check("第 3 页内容正确", pages[2] == p3, repr(pages[2]))
    check("三页拼回原文",
          "".join(pages) == content, repr("".join(pages))[:40])

    # 缺 spans 的页应产出空串而不是崩溃
    ex2 = Extractor(AZ)
    ex2._analyze = lambda pdf: {"content": "AB",
                                "pages": [{"pageNumber": 1, "spans": []},
                                          {"pageNumber": 2,
                                           "spans": [{"offset": 0, "length": 2}]}]}
    p2 = ex2._azure_page_texts(Path("dummy.pdf"))
    check("缺 spans 的页安全", p2 == ["", "AB"], str(p2))

    # document_text 应带页标记
    ex3 = Extractor(AZ)
    ex3._analyze = lambda pdf: result
    md = ex3._azure_document_text(Path("dummy.pdf"))
    check("document_text 注入页标记",
          all(f"<!-- page: {n} -->" in md for n in (1, 2, 3)))


# --------------------------------------------------------- text-layer path
def test_text_layer_fallback() -> None:
    print("\n[文本层路径（真实 PDF，无网络）]")
    pdf = ROOT / "data" / "aia_reports" / "AIA_Interim_Report_1H2021.pdf"
    if not pdf.is_file():
        check("样例 PDF 存在（跳过）", True, "未下载，跳过")
        return
    ex = Extractor({})                        # 强制 text-layer
    check("uses_azure 为假", not ex.uses_azure)
    pages = ex.page_texts(pdf)
    check("按页文本非空", len(pages) > 100 and any(p.strip() for p in pages),
          f"{len(pages)} 页")
    doc = ex.document_text(pdf)
    check("document_text 带页标记", "<!-- page: 1 -->" in doc and "<!-- page: 2 -->" in doc)


# ---------------------------------------------------- PageIndex integration
def test_pageindex_install() -> None:
    print("\n[PageIndex 接管]")
    info = install_into_pageindex({}, verbose=False)
    check("未配置时不接管（返回 text-layer）", info.name == BACKEND_TEXT_LAYER)

    info2 = install_into_pageindex(AZ, verbose=False)
    check("配置后接管并返回 azure-di", info2.name == BACKEND_AZURE)
    from pageindex.local_api import LocalAPI
    check("LocalAPI._extract_page_texts 已被替换",
          LocalAPI._extract_page_texts is not None)
    # 换成 stub，确认调用链真的走了我们的实现
    ex = Extractor(AZ)
    ex._analyze = lambda pdf: {"content": "HELLO-WORLD",
                               "pages": [{"pageNumber": 1,
                                          "spans": [{"offset": 0, "length": 11}]}]}
    LocalAPI._extract_page_texts = staticmethod(
        lambda fp: ex.page_texts(Path(fp)))
    got = LocalAPI._extract_page_texts("anything.pdf")
    check("接管后返回 Azure 的结果", got == ["HELLO-WORLD"], str(got))


def main() -> int:
    print("=" * 74)
    print("抽取后端解析测试（全部离线，不发网络请求）")
    print("=" * 74)
    test_resolution()
    test_azure_page_split()
    test_text_layer_fallback()
    test_pageindex_install()
    reset_cache()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
