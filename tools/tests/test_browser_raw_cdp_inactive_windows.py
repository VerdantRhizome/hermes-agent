#!/usr/bin/env python3
"""Inactive-window / saturation resilience test for tools/browser_raw_cdp.py.

Reproduces the real Android Chrome condition this project exists to survive:
Chrome keeps backgrounded/inactive windows' tabs in the CDP target list with
their renderers asleep. The device measured 189 Target.getTargets entries ->
only 1 attached (live), 188 unattached (inactive-window) tabs.

This test proves the backend picks the single live (attached) tab instead of
burning its MAX_PROBES budget on dead ones or falling over with HTTP 500.

Deterministic: mocks the CDP WebSocket transport (no live phone needed), so it
runs in CI and anywhere the repo lives.

Run:
    python3 tools/tests/test_browser_raw_cdp_inactive_windows.py   # standalone
    pytest tools/tests/test_browser_raw_cdp_inactive_windows.py     # pytest
"""
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_CANDIDATES = [_REPO_ROOT, "/data/data/com.termux/files/home/.hermes/hermes-agent"]
for _p in _CANDIDATES:
    if _p not in sys.path and os.path.isdir(os.path.join(_p, "tools")):
        sys.path.insert(0, _p)

import tools.browser_raw_cdp as mod


# --------------------------------------------------------------------------
# Scenario builder: 1 attached (live) + N unattached (inactive-window) targets
# --------------------------------------------------------------------------
def _build_targets(live_url, live_title, inactive_count=188):
    targets = [{
        "type": "page",
        "url": live_url,
        "targetId": "LIVE1",
        "attached": True,
        "title": live_title,
    }]
    for i in range(inactive_count):
        targets.append({
            "type": "page",
            "url": f"https://inactive-window-tab-{i}.example/",
            "targetId": f"DEAD{i:04d}",
            "attached": False,  # backgrounded window -> renderer asleep
            "title": f"Inactive {i}",
        })
    return targets


class FakeWS:
    def __init__(self, responder):
        self.responder = responder
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        msg = self.sent.pop(0)
        return json.dumps(self.responder(msg))

    def close(self):
        pass


def _make_browser_responder(targets):
    def browser_responder(msg):
        eid = msg["id"]
        if msg.get("method") == "Target.getTargets":
            return {"id": eid, "result": {"targetInfos": targets}}
        return {"id": eid, "result": {}}
    return browser_responder


def _page_responder_for(live_url, live_title):
    def page_responder(msg):
        method = msg.get("method")
        eid = msg["id"]
        if method == "Page.enable":
            return {"id": eid, "result": {}}
        if method == "Runtime.enable":
            return {"id": eid, "result": {}}
        if method == "Page.navigate":
            return {"id": eid, "result": {}}
        if method == "Runtime.evaluate":
            expr = (msg.get("params") or {}).get("expression", "")
            if expr == "1+1":
                return {"id": eid, "result": {"result": {"value": 2}}}
            if expr == "location.href":
                return {"id": eid, "result": {"result": {"value": live_url}}}
            if expr == "document.title":
                return {"id": eid, "result": {"result": {"value": live_title}}}
            if expr.startswith("document.readyState"):
                return {"id": eid, "result": {"result": {"value": "complete|" + live_url}}}
            return {"id": eid, "result": {"result": {"value": None}}}
        if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            return {"id": eid, "result": {}}
        return {"id": eid, "result": {}}
    return page_responder


def _fake_ws_connect_factory(targets, live_url, live_title):
    live_ws = f"ws://localhost:9222/devtools/page/LIVE1"
    dead_ws = f"ws://localhost:9222/devtools/page/DEAD0000"

    def fake_ws_connect(url, **kwargs):
        if str(url).endswith("/devtools/browser"):
            return FakeWS(_make_browser_responder(targets))
        # The live tab's socket responds; the dead one's hangs (recv never
        # answers within the probe timeout) -> simulates asleep renderer.
        if str(url).endswith("/devtools/page/LIVE1"):
            return FakeWS(_page_responder_for(live_url, live_title))
        # Dead inactive-window tab: block recv forever (probe must time out).
        class HangWS(FakeWS):
            def recv(self, timeout=None):
                import time as _t
                _t.sleep(timeout or 5)
                raise TimeoutError("simulated dead socket")
        return HangWS(_page_responder_for(live_url, live_title))

    return fake_ws_connect


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_open_succeeds_with_single_live_amid_188_inactive_windows():
    targets = _build_targets("https://example.com/", "Example Domain")
    mod.ws_connect = _fake_ws_connect_factory(
        targets, "https://example.com/", "Example Domain")
    r = mod.run_raw_cdp_command(
        "t1", "open", ["https://example.com/"],
        "ws://localhost:9222/devtools/browser")
    assert r.get("success") is True, r
    assert r["data"]["title"] == "Example Domain"


def test_eval_succeeds_on_live_tab_only():
    targets = _build_targets("https://example.com/", "Example Domain")
    mod.ws_connect = _fake_ws_connect_factory(
        targets, "https://example.com/", "Example Domain")
    r = mod.run_raw_cdp_command(
        "t1", "eval", ["document.title"],
        "ws://localhost:9222/devtools/browser")
    assert r.get("success") is True, r
    assert "Example Domain" in (r.get("data", {}).get("result", "") or "")


def test_filter_ignores_unattached_targets():
    """The backend must short-circuit on attached==True and never probe the
    188 dead inactive-window tabs (which would exhaust MAX_PROBES)."""
    targets = _build_targets("https://example.com/", "Example Domain")
    # Count how many page sockets the backend actually opens (probes).
    opened = []

    class CountingWS(FakeWS):
        def __init__(self, responder):
            super().__init__(responder)
            opened.append(1)

    live_ws = "ws://localhost:9222/devtools/page/LIVE1"

    def counting_connect(url, **kwargs):
        if str(url).endswith("/devtools/browser"):
            return CountingWS(_make_browser_responder(targets))
        if str(url).endswith("/devtools/page/LIVE1"):
            return CountingWS(_page_responder_for(
                "https://example.com/", "Example Domain"))
        class HangWS(CountingWS):
            def recv(self, timeout=None):
                import time as _t
                _t.sleep(timeout or 5)
                raise TimeoutError("dead")
        return HangWS(_page_responder_for(
            "https://example.com/", "Example Domain"))

    mod.ws_connect = counting_connect
    r = mod.run_raw_cdp_command(
        "t1", "open", ["https://example.com/"],
        "ws://localhost:9222/devtools/browser")
    assert r.get("success") is True, r
    # Browser socket (1) + the single LIVE page socket (1) = 2. We must NOT have
    # probed the 188 dead inactive-window tabs.
    assert len(opened) <= 3, f"backend probed too many sockets: {len(opened)}"


def run():
    tests = [
        test_open_succeeds_with_single_live_amid_188_inactive_windows,
        test_eval_succeeds_on_live_tab_only,
        test_filter_ignores_unattached_targets,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
    print(f"\n=== {passed}/{passed + failed} inactive-window checks passed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
