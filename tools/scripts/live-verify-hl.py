#!/usr/bin/env python3
"""Live high-level E2E for the raw-CDP Android backend (self-hosted runner).

Connects to the forwarded Chrome CDP endpoint (CDP_URL env, default
http://localhost:9222), resolves a page target the way the raw-CDP backend
does (Target.getTargets, then derive the page websocket from targetId --
/json/list is ghost-polluted and targets report attached:false on Android
Chrome), then evaluates document.title on the page.

Exit 0 only when a real page answers; anything else exits non-zero with a
diagnostic. Run from the repo checkout; the Actions job that invokes this
installs `websockets` first.
"""
import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request

import websockets

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222").rstrip("/")


def _http_json(path):
    with urllib.request.urlopen(f"{CDP_URL}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


async def _rpc(ws, method, params=None):
    await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if msg.get("id") == 1:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


async def main():
    try:
        version = _http_json("/json/version")
    except Exception as e:  # noqa: BLE001
        print(f"cdp unreachable at {CDP_URL}: {e}")
        return 1
    browser_ws = version.get("webSocketDebuggerUrl")
    if not browser_ws:
        print(f"no webSocketDebuggerUrl in /json/version at {CDP_URL}")
        return 1
    print(f"cdp ok: {version.get('Browser', '?')}")

    async with websockets.connect(browser_ws, max_size=None) as bws:
        targets = (await _rpc(bws, "Target.getTargets")).get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        print(f"targets: {len(targets)} total, {len(pages)} pages")
        if not pages:
            print("no page targets; open a tab on the device")
            return 1
        target = pages[0]
        host = urllib.parse.urlsplit(browser_ws).netloc
        page_ws = f"ws://{host}/devtools/page/{target['targetId']}"
        print(f"page target: {target.get('title') or target.get('url')!r}")

    async with websockets.connect(page_ws, max_size=None) as pws:
        res = await _rpc(
            pws,
            "Runtime.evaluate",
            {"expression": "document.title", "returnByValue": True},
        )
        title = (res.get("result") or {}).get("value", "")
        print(f"document.title: {title!r}")
        if not title:
            print("page did not answer Runtime.evaluate")
            return 1
        print("live E2E OK")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
