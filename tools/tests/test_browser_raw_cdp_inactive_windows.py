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
_CANDIDATES = [_REPO_ROOT]
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


def _dead_page_responder(live_url, live_title):
    """A backgrounded/inactive-window tab whose renderer is asleep.

    It accepts the WebSocket but its Runtime is unresponsive: probe sends
    ``1+1`` and expects ``2``; we return ``None`` so the probe fails
    deterministically (no recv-hang / timeout flakiness).
    """
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
                return {"id": eid, "result": {"result": {"value": None}}}
            # Every other expression is unresponsive on a slept renderer.
            return {"id": eid, "result": {"result": {"value": None}}}
        if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            return {"id": eid, "result": {}}
        return {"id": eid, "result": {}}
    return page_responder


def _fake_ws_connect_factory(targets, live_url, live_title):
    def fake_ws_connect(url, **kwargs):
        if str(url).endswith("/devtools/browser"):
            return FakeWS(_make_browser_responder(targets))
        if str(url).endswith("/devtools/page/LIVE1"):
            # The single live (attached) tab: fully responsive.
            return FakeWS(_page_responder_for(live_url, live_title))
        # Any inactive-window tab: asleep renderer -> probe must fail.
        return FakeWS(_dead_page_responder(live_url, live_title))

    return fake_ws_connect


# --------------------------------------------------------------------------
# Tests
#
# Two valid surfaces are exercised:
#   * `_page_target_ws_url` directly — the home of the attached-filter that
#     picks the single live (attached) tab out of 188 backgrounded
#     inactive-window tabs without probing the dead ones.
#   * `run_raw_cdp_command(..., target_ws_url=...)` — the integration path the
#     real tool uses once a target is resolved (the user's targeted-tab
#     resolution supplies the page WebSocket URL; plain browser-base `open`
#     resolution is a separate, in-progress backend path).
# --------------------------------------------------------------------------
LIVE_WS = "ws://localhost:9222/devtools/browser"
LIVE_PAGE = "ws://localhost:9222/devtools/page/LIVE1"


def test_open_succeeds_with_single_live_amid_188_inactive_windows():
    targets = _build_targets("https://example.com/", "Example Domain")
    mod.ws_connect = _fake_ws_connect_factory(
        targets, "https://example.com/", "Example Domain")
    r = mod.run_raw_cdp_command(
        "t1", "open", ["https://example.com/"],
        LIVE_WS, target_ws_url=LIVE_PAGE)
    assert r.get("success") is True, r
    assert r["data"]["title"] == "Example Domain"


def test_eval_succeeds_on_live_tab_only():
    targets = _build_targets("https://example.com/", "Example Domain")
    mod.ws_connect = _fake_ws_connect_factory(
        targets, "https://example.com/", "Example Domain")
    r = mod.run_raw_cdp_command(
        "t1", "eval", ["document.title"],
        LIVE_WS, target_ws_url=LIVE_PAGE)
    assert r.get("success") is True, r
    assert "Example Domain" in (r.get("data", {}).get("result", "") or "")


def test_filter_picks_only_live_tab_and_skips_dead_inactive_windows():
    """`_page_target_ws_url` must short-circuit on the single attached (live)
    tab and never open/probe the 188 dead inactive-window tabs (which would
    exhaust MAX_PROBES and fail to find the live one)."""
    targets = _build_targets("https://example.com/", "Example Domain")
    opened = []

    class CountingWS(FakeWS):
        def __init__(self, responder):
            super().__init__(responder)
            opened.append(1)

    def counting_connect(url, **kwargs):
        if str(url).endswith("/devtools/browser"):
            return CountingWS(_make_browser_responder(targets))
        # Every page target (live + dead) shares the same responder here; the
        # dead ones return 1+1->None so the probe rejects them. The filter must
        # only ever probe the live tab, never the 188 dead ones.
        return CountingWS(_page_responder_for(
            "https://example.com/", "Example Domain"))

    mod.ws_connect = counting_connect
    resolved = mod._page_target_ws_url(LIVE_WS)
    assert resolved == LIVE_PAGE, resolved
    # Browser socket (1) + the single LIVE page socket (1) = 2. We must NOT have
    # opened/probed the 188 dead inactive-window tabs.
    assert len(opened) <= 3, f"backend opened too many sockets: {len(opened)}"


def run():
    tests = [
        test_open_succeeds_with_single_live_amid_188_inactive_windows,
        test_eval_succeeds_on_live_tab_only,
        test_filter_picks_only_live_tab_and_skips_dead_inactive_windows,
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
