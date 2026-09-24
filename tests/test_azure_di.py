#!/usr/bin/env python3
"""
Tests for the Azure Document Intelligence extractor.

Covers the pure logic — config parsing/validation, page-marker injection, error
mapping — with no network calls, so it runs anywhere.

    python tests/test_azure_di.py        # plain asserts
    pytest tests/test_azure_di.py        # also works under pytest
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from superindex.extractors.azure_di import (  # noqa: E402
    PAGE_MARKER,
    AzureDIConfig,
    AzureDIError,
    AzureDocIntelligence,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ------------------------------------------------------------------ config
def test_config_from_env() -> None:
    print("\n[配置解析]")
    cfg = AzureDIConfig.from_env({
        "AZURE_DI_ENDPOINT": "https://demo.cognitiveservices.azure.com/",
        "AZURE_DI_KEY": "abc123",
        "AZURE_DI_FEATURES": "formulas, ocrHighResolution",
    })
    check("读取 endpoint", cfg.endpoint == "https://demo.cognitiveservices.azure.com/")
    check("读取 key", cfg.key == "abc123")
    check("features 按逗号切分并去空格",
          cfg.features == ("formulas", "ocrHighResolution"), str(cfg.features))
    check("默认 model 是 prebuilt-layout", cfg.model == "prebuilt-layout")
    check("默认输出 markdown", cfg.output_format == "markdown")
    check("默认 stringIndexType 是 unicodeCodePoint",
          cfg.string_index_type == "unicodeCodePoint")

    # 兼容旧命名
    cfg2 = AzureDIConfig.from_env({
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://x.cognitiveservices.azure.com/",
        "AZURE_DOCUMENT_INTELLIGENCE_KEY": "k",
    })
    check("兼容 AZURE_DOCUMENT_INTELLIGENCE_* 命名", cfg2.key == "k")


def test_config_validation() -> None:
    print("\n[配置校验]")
    cases = [
        ("缺少 endpoint/key", AzureDIConfig(endpoint="", key=""),
         "not configured"),
        ("endpoint 不是完整 URL", AzureDIConfig(endpoint="my-res.cognitiveservices.azure.com", key="k"),
         "full URL"),
        ("output_format 非法", AzureDIConfig(endpoint="https://a.com", key="k", output_format="xml"),
         "markdown or text"),
        ("stringIndexType 非法", AzureDIConfig(endpoint="https://a.com", key="k", string_index_type="bytes"),
         "unicodeCodePoint"),
    ]
    for name, cfg, expect in cases:
        try:
            cfg.validate()
            check(name, False, "未报错，但应该报错")
        except AzureDIError as exc:
            check(name, expect in str(exc), str(exc).splitlines()[0][:60])

    try:
        AzureDIConfig(endpoint="https://a.com/", key="k").validate()
        check("合法配置通过校验", True)
    except AzureDIError as exc:
        check("合法配置通过校验", False, str(exc))


# ------------------------------------------------------- page-marker logic
def test_page_markers() -> None:
    print("\n[页标记注入]")
    content = "AAAABBBBCCCC"
    result = {
        "content": content,
        "pages": [
            {"pageNumber": 1, "spans": [{"offset": 0, "length": 4}]},
            {"pageNumber": 2, "spans": [{"offset": 4, "length": 4}]},
            {"pageNumber": 3, "spans": [{"offset": 8, "length": 4}]},
        ],
    }
    md = AzureDocIntelligence.to_markdown(result)
    for n in (1, 2, 3):
        check(f"第 {n} 页标记存在", PAGE_MARKER.format(n=n) in md)
    # 顺序必须保持
    check("标记顺序为 1,2,3",
          md.index("page: 1") < md.index("page: 2") < md.index("page: 3"))
    # 正文不能被破坏或重复
    stripped = md
    for n in (1, 2, 3):
        stripped = stripped.replace(f"\n{PAGE_MARKER.format(n=n)}\n", "")
    check("注入后正文与原文一致", stripped == content, repr(stripped[:30]))

    md_off = AzureDocIntelligence.to_markdown(result, page_markers=False)
    check("page_markers=False 时不注入", md_off == content)

    check("空 content 安全返回",
          AzureDocIntelligence.to_markdown({"content": "", "pages": []}) == "")

    # 越界 offset 不应崩溃
    weird = {"content": "AB", "pages": [{"pageNumber": 1, "spans": [{"offset": 999}]}]}
    try:
        AzureDocIntelligence.to_markdown(weird)
        check("越界 offset 不崩溃", True)
    except Exception as exc:  # noqa: BLE001
        check("越界 offset 不崩溃", False, str(exc))

    # 缺 spans 的页跳过
    nospan = {"content": "AB", "pages": [{"pageNumber": 1, "spans": []}]}
    check("缺 spans 的页被跳过",
          AzureDocIntelligence.to_markdown(nospan) == "AB")


# ----------------------------------------------------------- error mapping
def test_error_mapping() -> None:
    print("\n[错误映射]")
    cfg = AzureDIConfig(endpoint="https://demo.cognitiveservices.azure.com/", key="k")
    adi = AzureDocIntelligence(cfg)

    hints = {
        401: "AZURE_DI_KEY",
        404: "AZURE_DI_ENDPOINT",
        429: "rate limited",
    }
    for code, expect in hints.items():
        resp = httpx.Response(code, json={"error": {"message": "boom"}})
        try:
            adi._raise_for(resp)
            check(f"HTTP {code} 抛错", False, "未抛错")
        except AzureDIError as exc:
            check(f"HTTP {code} 抛错且提示可操作", expect in str(exc),
                  str(exc).splitlines()[-1][:56])

    adi._raise_for(httpx.Response(202))       # 不应抛错
    adi._raise_for(httpx.Response(200))
    check("2xx 不抛错", True)


def test_non_pdf_rejected() -> None:
    print("\n[输入校验]")
    cfg = AzureDIConfig(endpoint="https://demo.cognitiveservices.azure.com/", key="k")
    adi = AzureDocIntelligence(cfg)
    try:
        adi.analyze(b"not a pdf at all")
        check("非 PDF 输入被拒绝", False, "未抛错")
    except AzureDIError as exc:
        check("非 PDF 输入被拒绝", "does not look like a PDF" in str(exc))


def test_describe() -> None:
    print("\n[结果统计]")
    info = AzureDocIntelligence.describe({
        "content": "x" * 100,
        "pages": [{}, {}],
        "tables": [{}],
        "paragraphs": [{}, {}, {}],
    })
    check("describe 统计正确",
          info == {"pages": 2, "tables": 1, "paragraphs": 3, "content_chars": 100},
          str(info))
    check("describe 容忍缺字段",
          AzureDocIntelligence.describe({})["pages"] == 0)


def main() -> int:
    print("=" * 74)
    print("Azure Document Intelligence 提取器测试（全部离线，不发网络请求）")
    print("=" * 74)
    test_config_from_env()
    test_config_validation()
    test_page_markers()
    test_error_mapping()
    test_non_pdf_rejected()
    test_describe()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
