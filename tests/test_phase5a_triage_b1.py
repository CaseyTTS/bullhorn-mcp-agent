"""5A triage B-1: global log-record redaction and logger level clamps (T-B1a..f, SR-29)."""

from __future__ import annotations

import copy
import http.server
import io
import logging
import logging.config
import threading

import httpx
import pytest

from bullhorn_mcp.auth import secrets as secrets_mod
from bullhorn_mcp.identity import deploy

from ._identity_helpers import activate_shared

S1, S2 = "SENTINELcodeB1x", "SENTINELstateB1y"
PW, CS = "SENTINELpwB1z", "SENTINELsecretB1w"


@pytest.fixture
def root_capture():
    """A StreamHandler on the root logger at DEBUG (what an operator's handler would see)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old)


@pytest.fixture
def shared_logs(tmp_path, monkeypatch):
    s = activate_shared(tmp_path, monkeypatch)
    yield s
    deploy.reset()


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", f"https://auth-x.bullhornstaffing.com/cb?code={S1}&state={S2}&client_id=a")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_t_b1a_and_c_real_httpx_httpcore_traffic_is_redacted(shared_logs, root_capture, caplog):
    srv = _local_server()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/oauth/authorize?client_id=x&password={PW}&client_secret={CS}"
        with caplog.at_level(logging.DEBUG):
            with httpx.Client(follow_redirects=False) as c:
                c.get(url)
    finally:
        srv.shutdown()
    text = root_capture.getvalue() + caplog.text
    assert "HTTP Request" in text  # the httpx INFO line is still logged, redacted
    for s in (S1, S2, PW, CS):
        assert s not in text


def test_t_b1a_child_logger_records_are_redacted_even_at_warning(shared_logs, root_capture, caplog):
    loc = f"[(b'Location', b'https://auth.bullhornstaffing.com/cb?code={S1}&state={S2}')]"
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpcore.http11").debug("receive_response_headers.complete return_value=%s", loc)
        logging.getLogger("httpcore.http11").warning("receive_response_headers.complete return_value=%s", loc)
    text = root_capture.getvalue() + caplog.text
    assert "REDACTED" in text and S1 not in text and S2 not in text


def test_t_b1b_child_created_after_startup(shared_logs, root_capture, caplog):
    child = logging.getLogger("httpcore.newchild")
    child.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        child.debug("GET /cb?code=%s&state=%s Authorization: Bearer %s BhRestToken: %s", S1, S2, PW, CS)
    text = root_capture.getvalue() + caplog.text
    assert S1 not in text and S2 not in text and PW not in text and CS not in text


def test_t_b1b_uvicorn_dictconfig_and_access_log(shared_logs, root_capture):
    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(copy.deepcopy(LOGGING_CONFIG))  # what uvicorn does at startup
    from uvicorn.logging import AccessFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logging.getLogger("uvicorn.access").addHandler(handler)
    try:
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", f"/oauth/bullhorn/callback?code={S1}&state={S2}", "1.1", 200
        )
    finally:
        logging.getLogger("uvicorn.access").removeHandler(handler)
    assert "callback" in buf.getvalue() and S1 not in buf.getvalue() and S2 not in buf.getvalue()


def test_t_b1d_level_clamps(tmp_path, monkeypatch):
    pre = logging.getLogger("httpcore.preexisting_child")
    pre.setLevel(logging.DEBUG)
    activate_shared(tmp_path, monkeypatch)
    try:
        for name in secrets_mod.CLAMPED_LOGGERS + ("httpcore.preexisting_child", "mcp.server.streamable_http"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.INFO, name
    finally:
        deploy.reset()
    assert pre.level == logging.DEBUG  # restored when shared mode is torn down (tests)


def test_t_b1e_mcp_and_sse_debug_records_not_emitted(shared_logs, root_capture, caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("mcp.server.streamable_http").debug("response payload login_url=https://x/start?login=SENTINELlogin")
        logging.getLogger("sse_starlette.sse").debug("chunk: {\"login_url\": \"SENTINELlogin2\"}")
        logging.getLogger("mcp.server.lowlevel.server").debug("record payload SENTINELrecord")
    text = root_capture.getvalue() + caplog.text
    assert "SENTINELlogin" not in text and "SENTINELrecord" not in text


def test_t_b1f_local_mode_installs_nothing(monkeypatch):
    deploy.reset()
    child = logging.getLogger("httpcore.localchild")
    child.setLevel(logging.DEBUG)
    assert deploy.startup({}) is deploy.LOCAL
    assert not secrets_mod.log_redaction_installed()
    assert logging.getLogRecordFactory() is not secrets_mod._redacting_factory
    assert child.level == logging.DEBUG and logging.getLogger("httpx").level == logging.NOTSET


def test_confirmation_and_link_keys_in_patterns():
    text = secrets_mod.redact_query("POST /oauth/bullhorn/confirm link=AAA&csrf=BBB confirmation=CCCDDDEE")
    assert "AAA" not in text and "BBB" not in text and "CCCDDDEE" not in text
