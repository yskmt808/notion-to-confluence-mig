"""
ロガーユーティリティモジュール。
カスタムログレベル NOTICE (25) の定義と未捕捉例外ハンドラーの提供。
"""

import sys
import logging
from typing import Optional

NOTICE_LEVEL_NUM = 25  # INFO (20) と WARNING (30) の中間


def setup_notice_log_level():
    """Python標準loggingにカスタムレベル NOTICE (25) を登録する。"""
    if not hasattr(logging, "NOTICE"):
        logging.NOTICE = NOTICE_LEVEL_NUM
        logging.addLevelName(NOTICE_LEVEL_NUM, "NOTICE")

        def notice(self, message, *args, **kws):
            if self.isEnabledFor(NOTICE_LEVEL_NUM):
                self._log(NOTICE_LEVEL_NUM, message, args, **kws)

        logging.Logger.notice = notice


def setup_uncaught_exception_handler():
    """未捕捉の例外をキャッチして migration.log に確実に [ERROR] ログを記録するハンドラーを登録する。"""
    def _uncaught_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger = logging.getLogger("FATAL")
        logger.error(
            "Unhandled top-level exception caused program to terminate:",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = _uncaught_handler


# モジュールインポート時に自動登録
setup_notice_log_level()

