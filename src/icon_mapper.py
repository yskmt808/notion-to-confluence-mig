"""
Notion アイコン（Notion標準SVGアイコン・絵文字・URL）を
Confluenceが受け付けるUnicode絵文字にマッピング・解決するユーティリティモジュール。
"""

import os
import re
import urllib.parse
from typing import Any, Optional, Dict

# Notion標準アイコン名 -> Unicode絵文字 マッピングテーブル
NOTION_ICON_TO_EMOJI: Dict[str, str] = {
    # 星座 (Zodiac)
    "aquarius": "♒",
    "aries": "♈",
    "taurus": "♉",
    "gemini": "♊",
    "cancer": "♋",
    "leo": "♌",
    "virgo": "♍",
    "libra": "♎",
    "scorpio": "♏",
    "sagittarius": "♐",
    "capricorn": "♑",
    "pisces": "♓",
    # 交通・乗り物 (Transport & Travel)
    "airplane": "✈️",
    "plane": "✈️",
    "ambulance": "🚑",
    "bicycle": "🚲",
    "bike": "🚲",
    "bus": "🚌",
    "car": "🚗",
    "helicopter": "🚁",
    "rocket": "🚀",
    "sailing-boat": "⛵",
    "boat": "⛵",
    "ship": "🚢",
    "taxi": "🚕",
    "train": "🚆",
    "truck": "🚚",
    # 自然・植物・天候 (Nature, Plants & Weather)
    "clover": "🍀",
    "flower": "🌸",
    "leaf": "🍃",
    "tree": "🌳",
    "seedling": "🌱",
    "plant": "🌱",
    "sun": "☀️",
    "moon": "🌙",
    "cloud": "☁️",
    "rain": "🌧️",
    "snow": "❄️",
    "snowflake": "❄️",
    "thunder": "⚡",
    "lightning": "⚡",
    "fire": "🔥",
    "flame": "🔥",
    "drop": "💧",
    "water": "💧",
    "earth": "🌍",
    "globe": "🌐",
    "star": "⭐",
    "sparkle": "✨",
    "sparkles": "✨",
    # キャラクター・動物 (Animals & Fantasy)
    "alien-pixel": "👾",
    "alien": "👽",
    "ghost": "👻",
    "robot": "🤖",
    "cat": "🐱",
    "dog": "🐶",
    "bird": "🐦",
    "fish": "🐟",
    "bug": "🐛",
    "butterfly": "🦋",
    # オフィス・文房具・タスク (Office, Stationery & Tasks)
    "book": "📖",
    "bookmark": "🔖",
    "calendar": "📅",
    "clock": "🕒",
    "timer": "⏱️",
    "stopwatch": "⏱️",
    "hourglass": "⌛",
    "alarm": "⏰",
    "watch": "⌚",
    "bell": "🔔",
    "mail": "✉️",
    "email": "✉️",
    "envelope": "✉️",
    "inbox": "📥",
    "folder": "📁",
    "folders": "📁",
    "file": "📃",
    "files": "🗂️",
    "document": "📝",
    "documents": "📑",
    "page": "📄",
    "pages": "📑",
    "draft": "📝",
    "checklist": "☑️",
    "task": "📋",
    "tasks": "📋",
    "todo": "☑️",
    "list": "📑",
    "note": "📝",
    "notebook": "📓",
    "notepad": "🗒️",
    "article": "📰",
    "clipboard": "📋",
    "pin": "📌",
    "pushpin": "📌",
    "paperclip": "📎",
    "link": "🔗",
    "search": "🔍",
    "lock": "🔒",
    "unlock": "🔓",
    "key": "🔑",
    "shield": "🛡️",
    "tool": "🛠️",
    "tools": "🛠️",
    "wrench": "🔧",
    "hammer": "🔨",
    "screwdriver": "🪛",
    "settings": "⚙️",
    "cog": "⚙️",
    "gear": "⚙️",
    "gears": "⚙️",
    "trash": "🗑️",
    "tag": "🏷️",
    "tags": "🏷️",
    "flag": "🚩",
    "flags": "🚩",
    "check": "✅",
    "checks": "✅",
    "checkmark": "✅",
    "cross": "❌",
    "close": "❌",
    "ban": "🚫",
    "warning": "⚠️",
    "alert": "⚠️",
    "info": "ℹ️",
    "question": "❓",
    "help": "❓",
    "bulb": "💡",
    "lightbulb": "💡",
    "pencil": "✏️",
    "pen": "🖊️",
    "paintbrush": "🖌️",
    "archive": "📦",
    "stack": "📚",
    "layers": "📑",
    "layout": "📐",
    # 人・コミュニケーション (People & Communication)
    "user": "👤",
    "users": "👥",
    "person": "👤",
    "people": "👥",
    "team": "👥",
    "group": "👥",
    "smile": "😊",
    "smiley": "😊",
    "heart": "❤️",
    "thumbsup": "👍",
    "thumbsdown": "👎",
    "chat": "💬",
    "chats": "💬",
    "comment": "💬",
    "comments": "💬",
    "message": "💬",
    "megaphone": "📢",
    "speaker": "🔊",
    "sound": "🔊",
    "eye": "👁️",
    "hand": "✋",
    # デバイス・IT (Devices & Tech)
    "computer": "💻",
    "laptop": "💻",
    "desktop": "🖥️",
    "mobile": "📱",
    "phone": "📱",
    "smartphone": "📱",
    "device": "📱",
    "tablet": "📱",
    "camera": "📷",
    "video": "📹",
    "mic": "🎤",
    "microphone": "🎤",
    "headphones": "🎧",
    "tv": "📺",
    "terminal": "💻",
    "code": "💻",
    "database": "🗄️",
    "server": "🖥️",
    "wifi": "📶",
    # プロジェクト・進捗 (Projects, Agile & Status)
    "target": "🎯",
    "compass": "🧭",
    "roadmap": "🗺️",
    "milestone": "🏁",
    "sprint": "🏃",
    "guide": "📖",
    "manual": "📚",
    "policy": "📜",
    "release": "🚀",
    "status": "🚥",
    "progress": "📈",
    "metric": "📊",
    "analytics": "📉",
    # ビジネス・建物 (Business, Finance & Places)
    "chart": "📊",
    "graph": "📈",
    "trophy": "🏆",
    "medal": "🏅",
    "crown": "👑",
    "gift": "🎁",
    "bag": "🛍️",
    "shopping-cart": "🛒",
    "cart": "🛒",
    "money": "💰",
    "dollar": "💵",
    "credit-card": "💳",
    "wallet": "👛",
    "receipt": "🧾",
    "building": "🏢",
    "buildings": "🏢",
    "home": "🏠",
    "house": "🏠",
    "store": "🏬",
    "bank": "🏦",
    "hospital": "🏥",
    "school": "🏫",
    "factory": "🏭",
    "map": "🗺️",
    # 飲食・ライフスタイル (Food & Lifestyle)
    "coffee": "☕",
    "tea": "🍵",
    "cup": "☕",
    "beer": "🍺",
    "wine": "🍷",
    "food": "🍴",
    "pizza": "🍕",
    "apple": "🍎",
    "cake": "🎂",
    # 矢印 (Arrows)
    "arrow": "➡️",
    "arrows": "🔄",
    "caret": "🔻",
    # 記号・シェイプ
    "circle": "⚪",
    "square": "⬛",
    "triangle": "🔺",
    "diamond": "🔷",
    "cube": "🧊",
    "box": "📦",
    "dot": "•",
    "dots": "⋯",
}

# Notion標準アイコンのカラーサフィックス除去正規表現
_COLOR_SUFFIX_RE = re.compile(r"_(gray|lightgray|brown|orange|yellow|green|blue|purple|pink|red|default)$", re.IGNORECASE)


def extract_icon_name_from_path(path_or_url: str) -> str:
    """
    '/icons/aquarius_gray.svg?v=1' や 'https://app.notion.com/icons/airplane_purple.svg'
    からアイコン名（'aquarius', 'airplane'）を抽出する。
    """
    if not path_or_url:
        return ""
    clean = urllib.parse.unquote(path_or_url).strip()
    clean = clean.split("?")[0].split("#")[0]
    basename = os.path.basename(clean)
    name_no_ext = os.path.splitext(basename)[0]
    # カラーサフィックス (_gray 等) を除去
    name = _COLOR_SUFFIX_RE.sub("", name_no_ext).lower().strip()
    return name


def is_unicode_emoji(text: str) -> bool:
    """与えられた文字列がUnicode絵文字であるかを判定する。"""
    if not text:
        return False
    # 長いパスやURL、通常英数字単語は絵文字ではない
    if "/" in text or "." in text or "\\" in text or len(text) > 8:
        return False
    # コードポイントのチェック
    for char in text:
        cp = ord(char)
        # 絵文字や記号のUnicode範囲
        if (
            0x1F000 <= cp <= 0x1FFFF  # 拡張絵文字
            or 0x2600 <= cp <= 0x27BF  # 雑種記号・絵文字・ディンバット
            or 0x2B50 <= cp <= 0x2B55
            or 0x2300 <= cp <= 0x23FF
            or 0x2B05 <= cp <= 0x2B07
            or 0x2934 <= cp <= 0x2935
            or 0x3297 <= cp <= 0x3299
            or 0xFE00 <= cp <= 0xFE0F  # 異体字セレクタ
            or 0x200D == cp            # ZWJ
        ):
            return True
    return False


def resolve_notion_icon_to_emoji(icon_val: Any) -> Optional[str]:
    """
    Notionのアイコン指定（Unicode絵文字、Notion標準SVGパス/URL、Notion APIアイコンオブジェクト）を
    Confluenceで表示可能なUnicode絵文字に解決する。
    解決できない場合は None を返す（不正な文字やパスがConfluenceに送られるのを防ぐ）。
    """
    if not icon_val:
        return None

    # 1. 辞書型（Notion APIオブジェクト）
    if isinstance(icon_val, dict):
        itype = icon_val.get("type")
        if itype == "emoji":
            return icon_val.get("emoji")
        elif itype == "icon":
            # API 2022-06-28 / 2025-09-03: {"type": "icon", "icon": {"name": "aquarius", "color": "gray"}}
            sub_icon = icon_val.get("icon") or {}
            name = sub_icon.get("name", "")
            return NOTION_ICON_TO_EMOJI.get(name.lower())
        elif itype in ("external", "file"):
            sub_obj = icon_val.get(itype) or {}
            url = sub_obj.get("url", "")
            name = extract_icon_name_from_path(url)
            return NOTION_ICON_TO_EMOJI.get(name)

    # 2. 文字列型
    if isinstance(icon_val, str):
        icon_str = icon_val.strip()
        if not icon_str:
            return None

        # すでに絵文字の場合
        if is_unicode_emoji(icon_str):
            return icon_str

        # パスまたはURLの場合 (例: /icons/aquarius_gray.svg)
        if "/icons/" in icon_str or icon_str.endswith(".svg") or icon_str.startswith("http"):
            name = extract_icon_name_from_path(icon_str)
            if name in NOTION_ICON_TO_EMOJI:
                return NOTION_ICON_TO_EMOJI[name]
            prefix = name.split("-")[0]
            if prefix in NOTION_ICON_TO_EMOJI:
                return NOTION_ICON_TO_EMOJI[prefix]
            return None

        # 直接アイコン名が渡された場合
        name = _COLOR_SUFFIX_RE.sub("", icon_str).lower().strip()
        if name in NOTION_ICON_TO_EMOJI:
            return NOTION_ICON_TO_EMOJI[name]
        prefix = name.split("-")[0]
        if prefix in NOTION_ICON_TO_EMOJI:
            return NOTION_ICON_TO_EMOJI[prefix]

    return None

