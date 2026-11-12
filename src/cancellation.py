"""
移行処理の安全な中断（Graceful Cancellation / Safe Stop）マネージャー。
SIGINT (Ctrl+C) および SIGTERM を捕捉し、進行中の単一リクエスト完了後に
安全なチェックポイント保存を行ってプロセスを終了します。
"""

import signal
import sys
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class MigrationCancelledException(Exception):
    """ユーザーまたはシステムによって移行処理が安全に中断された際の例外。"""
    pass


class CancellationManager:
    """SIGINT/SIGTERM シグナルを捕捉し、安全な中断を協調制御するマネージャー。"""

    def __init__(self, on_cancel_callback: Optional[Callable[[], None]] = None):
        self._cancel_requested = False
        self.is_error_halt = False
        self.halt_reason = ""
        self._on_cancel_callback = on_cancel_callback
        self._original_sigint = None
        self._original_sigterm = None
        self._installed = False

    def trigger_error_halt(self, error_message: str):
        """致命的エラー（ERRORレベル事象・リトライ上限超過等）発生時に後続処理および並行キューを即座に安全停止する。"""
        if not self._cancel_requested:
            self._cancel_requested = True
            self.is_error_halt = True
            self.halt_reason = error_message
            logger.error(f"🛑 [FATAL ERROR HALT] 後続処理・並行キューの安全停止を要求しました: {error_message}")
            print("\n" + "=" * 60)
            print(f"🛑 [ERROR] 致命的エラーを検知したため、後続処理を安全停止（Fail-Fast）します。")
            print(f"理由: {error_message}")
            print("進行中の単一APIリクエスト完了後、チェックポイントを安全に保存してプロセスを終了します。")
            print("=" * 60 + "\n")
            if self._on_cancel_callback:
                try:
                    self._on_cancel_callback()
                except Exception as e:
                    logger.error(f"Error in cancel callback during error halt: {e}")

    def install(self):
        """シグナルハンドラーを登録する。"""
        if self._installed:
            return
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        self._installed = True
        logger.debug("CancellationManager: Signal handlers installed (SIGINT, SIGTERM).")

    def restore(self):
        """シグナルハンドラーを元の状態に復元する。"""
        if not self._installed:
            return
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        self._installed = False
        logger.debug("CancellationManager: Signal handlers restored.")

    def _handle_signal(self, signum, frame):
        sig_name = "SIGINT (Ctrl+C)" if signum == signal.SIGINT else "SIGTERM"
        if not self._cancel_requested:
            self._cancel_requested = True
            print("\n" + "=" * 60)
            print(f"🛑 中断シグナル [{sig_name}] を検知しました。")
            print("現在実行中の単一APIリクエスト完了後に、安全にチェックポイントを保存して中断します。")
            print("※ 今すぐ強制終了する場合は、もう一度 Ctrl+C を押してください。")
            print("=" * 60 + "\n")
            logger.warning(f"Graceful cancellation requested via {sig_name}.")
            if self._on_cancel_callback:
                try:
                    self._on_cancel_callback()
                except Exception as e:
                    logger.error(f"Error in cancel callback: {e}")
        else:
            print("\n⚠️ 強制終了シグナルを受信しました。直ちに終了します。")
            sys.exit(130)

    @property
    def is_cancelled(self) -> bool:
        """中断が要求されているかどうかを返す。"""
        return self._cancel_requested

    def check_cancelled(self):
        """中断が要求されている場合、MigrationCancelledException を送出する。"""
        if self._cancel_requested:
            if self.is_error_halt:
                raise MigrationCancelledException(f"移行処理が致命的エラーにより安全停止しました: {self.halt_reason}")
            raise MigrationCancelledException("移行処理がユーザーによって安全に中断されました。")

