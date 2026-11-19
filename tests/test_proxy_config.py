"""Provider-side rental proxy config tests (ev 16783).

The rental proxy (set_proxy from the renter) is applied on the extension side
via chrome.proxy.settings — the provider only supplies the ProxySpec from its
own container env (CEKI_PROXY_URL / CEKI_PROXY_*). These tests exercise the
provider-side invariants without real Chrome:

- parsing CEKI_PROXY_URL (single URL, with/without credentials) and the
  discrete CEKI_PROXY_* parts into a ProxySpec;
- _proxy_url_with_creds embeds userinfo for httpx/websockets dials;
- server_url (the --proxy-server value) does NOT inline credentials — Chrome
  CLI rejects user:pass in the flag, auth is answered by the extension via
  webRequest.onAuthRequired;
- bypass_list carries the base entries and CEKI_PROXY_BYPASS_EXTRA;
- no proxy configured -> _proxy_from_env() returns None.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import (
    ProxySpec,
    _proxy_from_env,
    _proxy_url_with_creds,
)

# Internal hosts the proxy must always bypass. Kept in sync with daemon.py.
BASE_BYPASS = ["<local>", "localhost", "127.0.0.1", "::1", "*.ceki.me", "*.ceki.com"]


def _clear_proxy_env():
    for k in (
        "CEKI_PROXY_URL",
        "CEKI_PROXY",
        "CEKI_PROXY_SCHEME",
        "CEKI_PROXY_HOST",
        "CEKI_PROXY_PORT",
        "CEKI_PROXY_USERNAME",
        "CEKI_PROXY_PASSWORD",
        "CEKI_PROXY_BYPASS_EXTRA",
    ):
        os.environ.pop(k, None)


class TestProxyFromEnv:
    def test_no_proxy_env_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            assert _proxy_from_env() is None

    def test_single_url_with_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ["CEKI_PROXY_URL"] = "http://user:pass@1.2.3.4:3128"
            p = _proxy_from_env()
            assert p is not None
            assert (p.scheme, p.host, p.port) == ("http", "1.2.3.4", 3128)
            assert p.username == "user"
            assert p.password == "pass"

    def test_single_url_without_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ["CEKI_PROXY_URL"] = "http://1.2.3.4:3128"
            p = _proxy_from_env()
            assert p is not None
            assert (p.scheme, p.host, p.port) == ("http", "1.2.3.4", 3128)
            assert p.username is None
            assert p.password is None

    def test_https_url_normalized_to_http(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ["CEKI_PROXY_URL"] = "https://proxy.example:443"
            p = _proxy_from_env()
            assert p is not None
            assert p.scheme == "http"
            assert p.port == 443

    def test_discrete_parts(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ.update(
                {
                    "CEKI_PROXY_SCHEME": "socks5",
                    "CEKI_PROXY_HOST": "10.0.0.5",
                    "CEKI_PROXY_PORT": "1080",
                    "CEKI_PROXY_USERNAME": "user",
                    "CEKI_PROXY_PASSWORD": "pw",
                }
            )
            p = _proxy_from_env()
            assert p is not None
            assert (p.scheme, p.host, p.port) == ("socks5", "10.0.0.5", 1080)
            assert (p.username, p.password) == ("user", "pw")

    def test_default_ports(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ["CEKI_PROXY_URL"] = "http://proxy.example"  # no port
            p = _proxy_from_env()
            assert p is not None
            assert p.port == 3128


class TestProxyUrls:
    def test_server_url_does_not_inline_credentials(self):
        """Chrome CLI rejects user:pass in --proxy-server; auth is handled by
        the extension. server_url must carry only scheme://host:port."""
        p = ProxySpec("http", "1.2.3.4", 3128, username="user", password="pass")
        assert p.server_url == "http://1.2.3.4:3128"
        assert "user" not in p.server_url
        assert "pass" not in p.server_url

    def test_proxy_url_with_creds_embeds_userinfo(self):
        """httpx/websockets dials (geo resolve, backend fetch) DO need userinfo."""
        p = ProxySpec("http", "1.2.3.4", 3128, username="user", password="pass")
        assert _proxy_url_with_creds(p) == "http://user:pass@1.2.3.4:3128"

    def test_proxy_url_with_creds_none(self):
        assert _proxy_url_with_creds(None) is None

    def test_proxy_url_with_creds_no_auth(self):
        p = ProxySpec("http", "1.2.3.4", 3128)
        assert _proxy_url_with_creds(p) == "http://1.2.3.4:3128"


class TestBypassList:
    def test_base_entries_always_present(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            p = ProxySpec("http", "1.2.3.4", 3128)
            assert p.bypass_list == BASE_BYPASS

    def test_bypass_extra_appends(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_proxy_env()
            os.environ["CEKI_PROXY_BYPASS_EXTRA"] = "10.0.0.0/8 internal.host"
            p = ProxySpec("http", "1.2.3.4", 3128)
            assert p.bypass_list == BASE_BYPASS + ["10.0.0.0/8", "internal.host"]
