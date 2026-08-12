#!/usr/bin/env python3
"""Deterministic visible-tab + targeted-tab tests for tools/browser_raw_cdp.py.

Mocks the CDP transport so the visible-tab / background-tab / by-url resolution
logic is exercised without a flaky Android Chrome devtools socket. This is the
read-only, no-navigation path — it must not disturb tabs.
"""
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_CANDIDATES = [
    _REPO_ROOT,
    "/data/data/com.termux/files/home/.hermes/hermes-agent",
]
for _p in _CANDIDATES:
    if _p not in sys.path and os.path.isdir(os.path.join(_p, "tools")):
        sys.path.insert(0, _p)

import tools.browser_raw_cdp as mod


class FakeWS:
    """Minimal fake websocket that replays a responder list on recv()."""

    def __init__(self, responder, state=None):
        self.responder = responder
        self.sent = []
        self.state = state or {}

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        msg = self.sent.pop(0)
        return json.dumps(self.responder(self.state, msg))

    def close(self):
        pass


# ---------------------------------------------------------------------------
# MOCK RESPONDERS
# ---------------------------------------------------------------------------

def page_visible_responder(state, msg):
    method = msg.get("method")
    eid = msg.get("id")
    if method == "Runtime.enable":
        return {"id": eid, "result": {}}
    if method == "Page.enable":
        return {"id": eid, "result": {}}
    if method == "DOM.getDocument":
        return {"id": eid, "result": _build_dom_root("a", "href",
                     "https://example.com/foo", "Click me", 3)}
    if method == "DOM.querySelectorAll":
        return {"id": eid, "result": {"nodeIds": [3]}}
    if method == "DOM.describeNode":
        return {"id": eid, "result": {"node": {
            "nodeType": 1, "localName": "a", "nodeName": "A",
            "backendNodeId": 3,
            "attributes": ["href", "https://example.com/foo"], "parentId": 2}}}
    if method == "DOM.resolveNode":
        return {"id": eid, "result": {"object": {
            "type": "object", "subtype": "node", "className": "HTMLAnchorElement",
            "objectId": "MOCKOBJ1"}}}
    if method == "Runtime.callFunctionOn":
        expr = (msg.get("params") or {}).get("functionDeclaration", "")
        if "getBoundingClientRect" in expr:
            return {"id": eid, "result": {"result": {"value": {"x": 10, "y": 20}}}}
        return {"id": eid, "result": {"result": {"value": True}}}
    if method == "Runtime.evaluate":
        expr = (msg.get("params") or {}).get("expression", "")
        if expr == "document.visibilityState":
            return {"id": eid, "result": {"result": {"value": "visible"}}}
        if expr == "1+1":
            return {"id": eid, "result": {"result": {"value": 2}}}
        if expr == "location.href":
            return {"id": eid, "result": {"result": {"value": state.get("url", "https://example.com/")}}}
        if expr == "document.title":
            return {"id": eid, "result": {"result": {"value": state.get("title", "Example Domain")}}}
        if "history.back" in expr:
            return {"id": eid, "result": {"result": {"value": None}}}
        if "getBoundingClientRect" in expr:
            return {"id": eid, "result": {"result": {"value": {"x": 10, "y": 20}}}}
        return {"id": eid, "result": {"result": {"value": None}}}
    if method == "Page.navigate":
        new_url = (msg.get("params") or {}).get("url", "")
        state["url"] = new_url
        state["title"] = new_url.split("/")[-1] if new_url else "New Page"
        return {"id": eid, "result": {}}
    if method == "Page.captureScreenshot":
        return {"id": eid, "result": {"data": "BASE64PNGDATA"}}
    if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
        return {"id": eid, "result": {}}
    return {"id": eid, "result": {}}


def page_hidden_responder(state, msg):
    method = msg.get("method")
    eid = msg.get("id")
    if method == "Runtime.enable":
        return {"id": eid, "result": {}}
    if method == "Page.enable":
        return {"id": eid, "result": {}}
    if method == "Runtime.evaluate":
        expr = (msg.get("params") or {}).get("expression", "")
        if expr == "document.visibilityState":
            return {"id": eid, "result": {"result": {"value": "hidden"}}}
        if expr == "1+1":
            return {"id": eid, "result": {"result": {"value": 2}}}
        if expr == "location.href":
            return {"id": eid, "result": {"result": {"value": state.get("url", "https://other.example/")}}}
        if expr == "document.title":
            return {"id": eid, "result": {"result": {"value": state.get("title", "Other Domain")}}}
        if "history.back" in expr:
            return {"id": eid, "result": {"result": {"value": None}}}
        if "getBoundingClientRect" in expr:
            return {"id": eid, "result": {"result": {"value": {"x": 30, "y": 40}}}}
        return {"id": eid, "result": {"result": {"value": None}}}
    if method == "Page.navigate":
        new_url = (msg.get("params") or {}).get("url", "")
        state["url"] = new_url
        state["title"] = new_url.split("/")[-1] if new_url else "New Page"
        return {"id": eid, "result": {}}
    if method == "Page.captureScreenshot":
        return {"id": eid, "result": {"data": "OTHERBASE64"}}
    if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
        return {"id": eid, "result": {}}
    return {"id": eid, "result": {}}


def browser_responder(state, msg):
    eid = msg.get("id")
    if msg.get("method") == "Target.getTargets":
        return {"id": eid, "result": {"targetInfos": [
            {"type": "page", "url": "chrome-native://newtab/", "targetId": "nat1"},
            {"type": "page", "url": "https://example.com/", "targetId": "ex1",
             "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/EX1"},
            {"type": "page", "url": "https://other.example/", "targetId": "OTH1",
             "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/OTH1"},
        ]}}
    return {"id": eid, "result": {}}


def _build_dom_root(tag, attr_key, attr_val, text_content, node_id):
    return {
        "root": {
            "nodeId": 1, "nodeType": 1, "localName": "html", "nodeName": "HTML",
            "attributes": [], "childNodeCount": 1,
            "children": [{
                "nodeId": 2, "nodeType": 1, "localName": "body", "nodeName": "BODY",
                "attributes": [], "childNodeCount": 1,
                "children": [{
                    "nodeId": node_id, "nodeType": 1, "localName": tag, "nodeName": tag.upper(),
                    "attributes": [attr_key, attr_val],
                    "childNodeCount": 0,
                    "children": [{"nodeId": node_id + 1, "nodeType": 3,
                                  "nodeValue": text_content, "children": []}]
                }]
            }]
        }
    }


# ---------------------------------------------------------------------------
# FAKE WEBSOCKET CONNECT
# ---------------------------------------------------------------------------

def fake_ws_connect(url, **kwargs):
    s = str(url)
    if s.endswith("/devtools/browser"):
        return FakeWS(browser_responder)
    if "EX1" in s:
        return FakeWS(page_visible_responder)
    if "OTH1" in s:
        return FakeWS(page_hidden_responder)
    return FakeWS(page_visible_responder)


mod.ws_connect = fake_ws_connect


# ---------------------------------------------------------------------------
# TEST RUNNER
# ---------------------------------------------------------------------------

def run() -> int:
    BWS = "ws://localhost:9222/devtools/browser"
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}")

    # ---- visible tab resolution ----
    vis = mod._refresh_visible_tab(BWS)
    check("visible tab is example.com", vis is not None and vis.get("targetId") == "ex1")
    check("visible tab ws_url present", vis is not None and bool(vis.get("ws_url")))
    check("visible tab url field correct", vis is not None and vis.get("url") == "https://example.com/")

    # ---- by-url resolution ----
    tid = mod._resolve_tab_by_url(BWS, "https://example.com/")
    check("resolve by url picks example.com", tid == "ex1")
    tid2 = mod._resolve_tab_by_url(BWS, "https://other.example/")
    check("resolve by url picks other.example", tid2 == "OTH1")
    tid3 = mod._resolve_tab_by_url(BWS, "https://missing.example/")
    check("resolve by url returns None for missing", tid3 is None)

    # ---- background tab ws ----
    bg = mod._background_tab_ws(BWS)
    check("background tab ws is the OTHER (hidden) tab", bg is not None and "OTH1" in bg)

    # ---- run_raw_cdp_command on_visible (read-only snapshot) ----
    r = mod.run_raw_cdp_command("tv", "snapshot", [], BWS, on_visible=True)
    check("on_visible snapshot success", r.get("success") is True)
    check("on_visible snapshot has text", len(r.get("data", {}).get("snapshot", "")) > 0)

    # ---- run_raw_cdp_command on_visible when no visible tab ----
    def no_targets_responder(msg):
        eid = msg["id"]
        if msg.get("method") == "Target.getTargets":
            return {"id": eid, "result": {"targetInfos": []}}
        return {"id": eid, "result": {}}

    orig_connect = mod.ws_connect

    def fake_no_targets(url, **kwargs):
        s = str(url)
        if s.endswith("/devtools/browser"):
            return FakeWS(no_targets_responder)
        return FakeWS(page_visible_responder)

    mod.ws_connect = fake_no_targets
    r2 = mod.run_raw_cdp_command("novec", "snapshot", [], BWS, on_visible=True)
    check("on_visible with no visible tab returns error",
          r2.get("success") is False and "visible" in (r2.get("error") or "").lower())
    mod.ws_connect = orig_connect

    # ---- run_raw_cdp_command per-target: visible snapshot vs background navigate ----
    # Snapshot the visible tab (ex1) stays on example.com (read-only).
    r3 = mod.run_raw_cdp_command("t-vis", "snapshot", [], BWS, target_id="ex1")
    check("targeted snapshot of visible tab", r3.get("success") is True)

    r3b = mod.run_raw_cdp_command("t-vis", "eval", ["document.title"], BWS, target_id="ex1")
    check("targeted eval of visible tab returns example.com title",
          r3b.get("success") is True and "Example Domain" in (r3b.get("data", {}).get("result", "") or ""))

    # Navigate the background tab (oth1) — does NOT disturb visible tab.
    r4 = mod.run_raw_cdp_command("t-bg", "open", ["https://other.example/loaded"], BWS, target_id="oth1")
    check("background tab navigate success", r4.get("success") is True)

    # Re-check visible tab: untouched (still example.com, read-only).
    r5 = mod.run_raw_cdp_command("t-vis2", "eval", ["document.title"], BWS, target_id="ex1")
    check("visible tab unchanged after background navigate",
          r5.get("success") is True and "Example Domain" in (r5.get("data", {}).get("result", "") or ""))

    # ---- visible tab targeted by URL ----
    r6 = mod.run_raw_cdp_command("t-url", "eval", ["document.title"], BWS, target_url="https://example.com/")
    check("target by url eval on visible tab",
          r6.get("success") is True and "Example Domain" in (r6.get("data", {}).get("result", "") or ""))

    # ---- prefer_background: navigate without disturbing visible ----
    r7 = mod.run_raw_cdp_command("t-bg2", "open", ["https://other.example/mutated"], BWS, prefer_background=True)
    check("prefer_background navigate success", r7.get("success") is True)

    # Visible tab still example.com.
    r8 = mod.run_raw_cdp_command("t-vis3", "eval", ["document.title"], BWS, on_visible=True)
    check("visible tab still example.com after prefer_background navigate",
          r8.get("success") is True and "Example Domain" in (r8.get("data", {}).get("result", "") or ""))

    print(f"\n=== {passed}/{passed + failed} checks passed ===")
    return failed


if __name__ == "__main__":
    rc = run()
    sys.exit(rc)
