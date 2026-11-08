"""
タイムゾーン処理およびローカル実行環境からのタイムゾーン自動判定ユーティリティ。
"""

import os
import datetime
from typing import Optional, Union
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def get_local_timezone() -> datetime.tzinfo:
    """
    ローカル実行環境のタイムゾーンを取得する。
    1. 環境変数 TZ が設定されていればそれを優先
    2. システムローカルの tzinfo (datetime.now().astimezone().tzinfo)
    3. フォールバックとして UTC
    """
    tz_env = os.getenv("TZ")
    if tz_env:
        resolved = resolve_timezone(tz_env)
        if resolved:
            return resolved

    local_tz = datetime.datetime.now().astimezone().tzinfo
    if local_tz is not None:
        return local_tz

    return datetime.timezone.utc


def get_local_timezone_name() -> str:
    """ローカル実行環境のタイムゾーン表示名を取得する (例: 'JST', 'Asia/Tokyo', '+0900')"""
    tz_env = os.getenv("TZ")
    if tz_env:
        return tz_env
    local_dt = datetime.datetime.now().astimezone()
    name = local_dt.tzname()
    if name:
        return name
    if local_dt.tzinfo:
        return str(local_dt.tzinfo)
    return "UTC"


def resolve_timezone(tz_val: Optional[Union[str, datetime.tzinfo]]) -> datetime.tzinfo:
    """
    指定された文字列または tzinfo から有効な datetime.tzinfo オブジェクトを解決する。
    未指定の場合はローカル実行環境のタイムゾーンを返す。
    """
    if tz_val is None:
        return get_local_timezone()

    if isinstance(tz_val, datetime.tzinfo):
        return tz_val

    if isinstance(tz_val, str):
        tz_str = tz_val.strip()
        if not tz_str:
            return get_local_timezone()

        # IANA タイムゾーン名 (例: 'Asia/Tokyo', 'UTC', 'America/New_York')
        if ZoneInfo is not None:
            try:
                return ZoneInfo(tz_str)
            except Exception:
                pass

        # 一般的な略称やオフセットのフォールバック
        upper_tz = tz_str.upper()
        if upper_tz in ("JST", "ASIA/TOKYO"):
            return datetime.timezone(datetime.timedelta(hours=9), "JST")
        elif upper_tz in ("UTC", "GMT", "Z"):
            return datetime.timezone.utc
        elif upper_tz in ("EST", "EDT"):
            return datetime.timezone(datetime.timedelta(hours=-5), "EST")
        elif upper_tz in ("PST", "PDT"):
            return datetime.timezone(datetime.timedelta(hours=-8), "PST")

        # ±HH:MM または ±HHMM 形式の解析
        if (tz_str.startswith("+") or tz_str.startswith("-")) and len(tz_str) in (5, 6):
            try:
                sign = 1 if tz_str[0] == "+" else -1
                parts = tz_str[1:].replace(":", "")
                hours = int(parts[:2])
                minutes = int(parts[2:4]) if len(parts) >= 4 else 0
                return datetime.timezone(datetime.timedelta(hours=sign * hours, minutes=sign * minutes), tz_str)
            except Exception:
                pass

    return get_local_timezone()


def format_iso_datetime(
    iso_str: Optional[str],
    tz: Optional[Union[str, datetime.tzinfo]] = None,
    fmt: str = "%Y-%m-%d %H:%M %Z",
) -> str:
    """
    ISO 8601 形式の文字列（例: '2026-11-07T23:30:00.000Z'）を指定またはローカルのタイムゾーンに変換してフォーマットする。
    パースできない場合は元の文字列を安全に整形して返す。
    """
    if not iso_str:
        return ""

    clean = str(iso_str).strip()
    target_tz = resolve_timezone(tz)

    try:
        # ISO8601 パース ('Z' を '+00:00' に置換)
        if clean.endswith("Z"):
            dt = datetime.datetime.fromisoformat(clean[:-1] + "+00:00")
        else:
            dt = datetime.datetime.fromisoformat(clean)

        # タイムゾーン情報がない場合は UTC として解釈
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)

        converted = dt.astimezone(target_tz)
        formatted = converted.strftime(fmt).strip()
        return formatted
    except Exception:
        # パース失敗時は基本的な文字列置換でフォールバック
        return clean.replace("T", " ").replace(".000Z", " UTC").split("+")[0]

