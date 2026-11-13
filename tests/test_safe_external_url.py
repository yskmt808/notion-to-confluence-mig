"""SSRF 対策 `_is_safe_external_url` の境界テスト (TCK_00003)。

カバー画像・添付ファイルの外部 URL ダウンロード前に呼ばれる安全性判定。
http/https 以外、ループバック・リンクローカル・プライベート・マルチキャスト・予約アドレスを拒否すること。

【既知の抜け穴】 `127.1` / `2130706433` / `0x7f.0.0.1` / `localhost.` などの非標準表記は
`ipaddress.ip_address()` が ValueError となるためホスト名扱いで素通りし、OS 側では
ループバックに解決される。該当ケースは `expectedFailure` で明示しており、修正されると
「unexpected success」でテストが知らせる（修正は別チケットで管理）。
"""

import unittest

from src.importer import _is_safe_external_url


class TestIsSafeExternalUrlAllowed(unittest.TestCase):
    def test_public_http_and_https_are_allowed(self):
        for url in (
            "https://example.com/a?b=1",
            "http://example.com",
            "https://sub.domain.example.co.jp:8443/path/file.png",
            "http://8.8.8.8/",
            "http://[2001:4860:4860::8888]/",
        ):
            with self.subTest(url=url):
                self.assertTrue(_is_safe_external_url(url))

    def test_scheme_and_host_are_case_insensitive_and_whitespace_trimmed(self):
        self.assertTrue(_is_safe_external_url("HTTPS://EXAMPLE.COM"))
        self.assertTrue(_is_safe_external_url("  https://example.com  "))
        self.assertFalse(_is_safe_external_url("  http://LOCALHOST:8080/ "))


class TestIsSafeExternalUrlRejected(unittest.TestCase):
    def _assert_all_rejected(self, urls):
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(_is_safe_external_url(url))

    def test_invalid_input_types_and_empty(self):
        self._assert_all_rejected(["", None, 123, b"http://example.com", [], {}])

    def test_non_http_schemes(self):
        self._assert_all_rejected(
            [
                "ftp://example.com/x",
                "file:///etc/passwd",
                "javascript:alert(1)",
                "data:text/html,<script>1</script>",
                "gopher://example.com/",
                "//example.com/protocol-relative",
                "example.com/no-scheme",
            ]
        )

    def test_localhost_names_and_internal_suffixes(self):
        self._assert_all_rejected(
            [
                "http://localhost/",
                "http://LOCALHOST:8080/",
                "http://ip6-localhost/",
                "http://ip6-loopback/",
                "http://printer.local/",
                "http://db.internal/",
                "http://a.b.corp.internal:5432/",
            ]
        )

    def test_ipv4_loopback_private_linklocal_multicast_reserved(self):
        self._assert_all_rejected(
            [
                "http://127.0.0.1/",
                "http://127.0.0.1:8080/x",
                "http://127.255.255.254/",
                "http://10.0.0.5/",
                "http://172.16.0.1/",
                "http://172.31.255.255/",
                "http://192.168.1.1/",
                "http://169.254.169.254/latest/meta-data/",  # クラウドメタデータ
                "http://224.0.0.1/",
                "http://240.0.0.1/",
                "http://0.0.0.0/",
            ]
        )

    def test_ipv6_loopback_private_linklocal(self):
        self._assert_all_rejected(
            [
                "http://[::1]/",
                "http://[fe80::1]/",
                "http://[fc00::1]/",
                "http://[fd12:3456:789a::1]/",
                "http://[::ffff:127.0.0.1]/",  # IPv4-mapped ループバック
                "http://[::ffff:10.0.0.1]/",
            ]
        )

    def test_userinfo_does_not_hide_private_host(self):
        # urlparse().hostname は userinfo を除いた実ホストを返す
        self._assert_all_rejected(
            [
                "http://user:pw@127.0.0.1/",
                "http://example.com@127.0.0.1/",
                "http://example.com@localhost/",
            ]
        )

    def test_public_looking_boundaries_are_not_over_rejected(self):
        # 172.16/12 の外側・プライベート範囲に隣接する公開アドレスは許可される
        for url in ("http://172.15.255.255/", "http://172.32.0.1/", "http://11.0.0.1/", "http://192.169.0.1/"):
            with self.subTest(url=url):
                self.assertTrue(_is_safe_external_url(url))


class TestIsSafeExternalUrlKnownBypasses(unittest.TestCase):
    """OS では loopback に解決されるが現状は「安全」と誤判定される表記 (要修正)。"""

    @unittest.expectedFailure
    def test_ipv4_shorthand_loopback(self):
        self.assertFalse(_is_safe_external_url("http://127.1/"))

    @unittest.expectedFailure
    def test_ipv4_decimal_integer_loopback(self):
        self.assertFalse(_is_safe_external_url("http://2130706433/"))

    @unittest.expectedFailure
    def test_ipv4_hex_loopback(self):
        self.assertFalse(_is_safe_external_url("http://0x7f.0.0.1/"))

    @unittest.expectedFailure
    def test_localhost_with_trailing_dot(self):
        self.assertFalse(_is_safe_external_url("http://localhost./"))


if __name__ == "__main__":
    unittest.main()

