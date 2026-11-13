"""Confluence Storage Format (XHTML) の整形式検証ヘルパー。

converter が生成する Storage Format は ac: / ri: 名前空間を含む XHTML フラグメントであり、
閉じタグ漏れ・属性エスケープ不備・不正ネストがあると Confluence API が 400 を返す。
部分文字列一致 (assertIn) では検出できないため、XML パーサで構文検証する。
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional

_WRAPPER = (
    '<root xmlns:ac="http://atlassian.com/content" '
    'xmlns:ri="http://atlassian.com/resource/identifier">{body}</root>'
)

# Confluence Storage Format では XHTML 定義済みエンティティ (&nbsp; 等) が使われうるが、
# XML には定義されていないため、構文検証前に数値文字参照へ置換する。
_HTML_ENTITIES = {
    "nbsp": "&#160;",
    "copy": "&#169;",
    "reg": "&#174;",
    "hellip": "&#8230;",
    "mdash": "&#8212;",
    "ndash": "&#8211;",
    "rarr": "&#8594;",
    "larr": "&#8592;",
}


def _normalize_entities(body: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        return _HTML_ENTITIES.get(m.group(1), m.group(0))

    return re.sub(r"&([a-zA-Z]+);", repl, body)


def parse_storage_xml(body: str) -> ET.Element:
    """Storage Format 断片を XML としてパースし、ラッパー要素を返す。

    不正な場合は xml.etree.ElementTree.ParseError を送出する。
    """
    return ET.fromstring(_WRAPPER.format(body=_normalize_entities(body)))


def assert_valid_storage_xml(testcase, body: str, label: Optional[str] = None) -> ET.Element:
    """unittest.TestCase から呼ぶ整形式アサーション。失敗時は該当箇所の抜粋を表示する。"""
    try:
        return parse_storage_xml(body)
    except ET.ParseError as e:
        line, col = e.position
        lines = _normalize_entities(body).split("\n")
        # ラッパーは同一行に置かれるため、1 行目のみ列オフセットが前置分ずれる
        offset = len(_WRAPPER.format(body="")) - len("</root>") if line == 1 else 0
        src_line = lines[line - 1] if 0 < line <= len(lines) else ""
        start = max(0, col - offset - 60)
        excerpt = src_line[start : col - offset + 60]
        prefix = f"[{label}] " if label else ""
        testcase.fail(f"{prefix}Storage Format is not well-formed XML: {e}\n  ...{excerpt}...")
