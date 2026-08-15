# Android Chrome CDP quirks (driving phone Chrome from Termux)

Condensed from the 2026-08 sessions building `tools/browser_raw_cdp.py` — a
raw-CDP backend that routes Hermes's friendly `browser_*` tools to the phone's
Chrome over `browser.cdp_url` (bypassing the `agent-browser` subprocess, which
is hard-platform-blocked on android-arm64). These facts are about Chrome's
devtools behavior on Android, and apply to ANY raw-CDP client (the
`browser_cdp` tool, `cdp_helper.py`, or the backend).

## Native Android reality: inactive Chrome windows

This is the single most important thing to internalize before debugging CDP on
Android. **It is not a bug and it is not optional** — it is how Chrome + Android
behave, and it will be present on essentially every non-fresh Android device a
Hermes session runs on.

- Android aggressively **backgrounds/suspends** apps (and Chrome's secondary
  windows) to reclaim memory. Chrome, instead of killing those windows, keeps
  them in its session and surfaces them in **Manage windows → Inactive (N)**.
- A real-world device shows e.g. **1 Active window** (the foreground tab you
  care about) and **13 Inactive windows** holding **~167 tabs total** (one
  window alone had 127 tabs). The active window is fully live; the inactive
  ones have renderers that are asleep/suspended.
- **CDP enumerates ALL tabs across ALL windows.** `GET /json/list` therefore
  returns the sum — e.g. **189 entries** — of which only the active window's
  tabs are actually responsive. In a measured session: **189 `/json/list`
  entries → only 1 ALIVE**, 188 DEAD (their page sockets time out on
  `Runtime.evaluate`).

**Implication:** any code that enumerates via `/json/list` and tries to drive
the first entry will drown in dead targets. The fix is to **filter to live
targets**, not to "close tabs" (see below). Closing inactive windows is a valid
*user* remedy, but the backend must be robust regardless — because inactive
windows are a permanent fixture of Android Chrome.

## Behavior facts (verified, Chrome/150 on android-arm64)

1. **`Target.createTarget` WORKS on this device** (contrary to earlier notes).
   It successfully opened Wikipedia and GitHub tabs via the page-target path.
   Treat it as available; if it ever returns `Could not create a Tab`, fall back
   to attaching an existing live target. Do NOT assume it is blocked.

2. **Browser-level socket attach is rejected for page commands.** Connecting to
   the `/devtools/browser` WebSocket and calling `Target.attachToTarget` yields a
   `sessionId` Chrome then rejects: `Session with given id not found`. **Do NOT
   use the browser-level socket for page commands.** Use each page target's own
   socket (implicit session, no `sessionId`).

3. **Robust enumeration: `Target.getTargets` (browser socket), then connect to
   each page target's own socket.** This is the authoritative, *filterable*
   source — unlike `/json/list`.

   - **`Target.getTargets` returns `TargetInfo` with an `attached` boolean and a
     `browserContextId`.** This is the filter signal: **live, drivable tabs have
     `attached == true` and `type == "page"`**. Inactive-window tabs report
     `attached: false` (their renderer is not connected). Filter on
     `attached == true` and you get exactly the live tab(s) — **regardless of how
     many inactive windows exist.** This is THE mechanism that makes the backend
     immune to the inactive-window reality.

   - **`Target.getTargets` does NOT include `webSocketDebuggerUrl`** on Android
     Chrome (unlike `/json/list`). Derive the per-page socket from the
     `targetId`: `ws://<host>/devtools/page/<targetId>` (host parsed from
     `browser.cdp_url`, e.g. `ws://localhost:9222/devtools/browser` →
     `ws://localhost:9222/devtools/page/<id>`). The page socket is an *implicit*
     session — send CDP methods with **no `sessionId`**.

   - **`Target.getTargets` is FLAPPY under load:** a single call intermittently
     returns **0** page targets even when a tab is live. Retry the call a few
     times (small gap, e.g. 0.5s) before concluding there's no responsive tab.

   - **`/json/list` is polluted** (hundreds of dead entries). Do NOT use it for
     enumeration. It remains useful only as a *rough health gauge* (entry count
     tells you how saturated the session is).

4. **Navigation to an error/blank page can stall.** If the current URL is
   `chrome-error://` / `chrome-native://` / `about:blank`, navigate to
   `about:blank` first, then to the target, and poll
   `document.readyState + '|' + location.href` until `state == 'complete'` and
   the URL is not an error URL (up to ~6s) before reporting success.

## Wedged devtools from inactive-window saturation (the one that actually blocks you)

With MANY inactive windows (tens to hundreds of suspended tabs across windows),
the CDP server becomes **saturated**:

- **Measured failure:** high-level `browser_navigate` returned
  `raw-cdp open: server rejected WebSocket connection: HTTP 500` (reproducible,
  identical across retries). The backend's open/create step chokes when the
  server is overwhelmed by dead targets.
- **Cascade:** after repeated 500s, the **entire CDP endpoint can wedge**
  (ConnectionRefused on 9222) and even the **adb wireless-debug link drops to
  offline**. Recovery then needs a phone-side re-enable of wireless debugging.
- **But the live tab itself stays 100% reliable.** Driving the *known-live*
  (attached, responsive) tab via its page-target WebSocket returned correct
  results every time (`"Example Domain"` consistently). The dead-list does not
  corrupt the live tab — it saturates the server and breaks the *open/discover*
  path + can take down the whole endpoint.

**Two-layer mitigation:**

1. **Backend (code, mandatory): filter to live targets.**
   Enumerate via `Target.getTargets`, keep only `attached == true &&
   type == "page"`, probe each with `Runtime.evaluate("1+1") → 2`, and use the
   first responsive one. Never walk `/json/list`. This makes navigation robust
   even with 13 inactive windows / 188 dead entries. (See code patterns below.)

2. **User remedy (optional, speeds things up): close inactive windows.** In
   Chrome: **Manage windows → Inactive (N) → remove them** (or open one and
   close its tabs). This drops `/json/list` from ~189 to ~1 and removes server
   saturation. It is NOT required for the backend to work (layer 1 handles it),
   but it eliminates the 500s faster. Note: `am force-stop` + relaunch does
   **NOT** help — Android restores the entire window/tab session.

## Code patterns that make a raw-CDP backend resilient

- **Filter, don't enumerate-blind.** `Target.getTargets` → keep
  `attached == true && type == "page"`. Ignore everything else. This is the
  single highest-leverage rule for Android.
- **Bound `ws.recv`.** A wedged socket blocks `recv` forever. Use
  `ws.recv(timeout=min(remaining, 5.0))` and convert a recv-timeout into a
  `TimeoutError`. Without this the agent hangs indefinitely on a dead tab.
- **Probe before use.** For each candidate (attached) tab: open its socket,
  `Runtime.enable`, then `Runtime.evaluate("1+1")` and expect `2`. Skip any
  socket that doesn't answer within a few seconds. Pick the first *responsive*
  tab, not the first listed.
- **Cap probes** (e.g. `MAX_PROBES = 6`). With many zombies, scanning all is
  minutes of hanging. Fail loudly with an actionable message ("close inactive
  Chrome windows / re-enable wireless debugging") instead of a long hang.
- **Re-enable domains per connection.** Re-issue `Page.enable` + `Runtime.enable`
  on every (re)connection — idempotent and self-heals a socket that lost enable
  state.
- **Health gauge (optional, non-blocking):** count `GET /json/list` entries. If
  it is very high (e.g. > 50), log a hint that closing inactive Chrome windows
  will improve CDP responsiveness. This is a *signal*, not a gate — the backend
  must still work via the `attached` filter.

## DOM node identity: `nodeId` vs `backendNodeId` (silent click/fill killer)

`DOM.querySelectorAll` returns **`nodeId`s** — transient, connection-scoped ids
— NOT `backendNodeId`s. If you store the raw value as if it were a
`backendNodeId` (e.g. `bid:<n>`) and later call
`DOM.resolveNode({"backendNodeId": n})` or `DOM.getBoxModel({"backendNodeId": n})`,
Chrome returns **"No node with given id found"** and click/fill silently fail
("Element not found").

**Correct pattern (verified working on android-arm64, Chrome/150):**
- For each `nodeId` from `querySelectorAll`, first call
  `DOM.describeNode({"nodeId": nid})` and read `node.backendNodeId`. Store THAT.
- Build the snapshot document with a **non-pierce** `DOM.getDocument({"depth": -1})`.
  (A `pierce: true` document's `backendNodeId`s also failed to resolve via
  `DOM.resolveNode` here — use the plain document.)
- To resolve an element to coordinates for click:
  `DOM.resolveNode({"backendNodeId": bid})` → `objectId` →
  `Runtime.callFunctionOn({objectId, functionDeclaration: "function(){ const r = this.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; }", returnByValue: true})`.
  This is reliable; `DOM.getBoxModel` was NOT on android-arm64 for
  pierce-derived nodes.
- For fill: same `resolveNode` → `callFunctionOn` with
  `function(v){ this.focus(); this.value = v; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); }`
  and `arguments: [{"value": text}]`.

**React controlled inputs need the native setter.** `Input.insertText` /
`DOM.setAttribute` does NOT update a React controlled component's `.value`
(React's `_valueTracker` ignores the synthetic change). Verified: after a
high-level `browser_type`, the input's `.value` was empty on a React
`input[name=q]`. Fix that works:
```js
var i = document.querySelector('input[name=q]');
var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
setter.call(i, 'text');
i.dispatchEvent(new Event('input', {bubbles: true}));
```
Then submit via `form.requestSubmit()` (or `form.submit()`). This drove a real
DDG search to its results page. The `browser_raw_cdp` fill path should detect
React inputs (presence of `_valueTracker`) and use this pattern.

Storing a tag-name-only selector (e.g. `"a"`, `"body"`) as the ref also works for
`document.querySelector()` but is ambiguous on real pages — prefer the
`backendNodeId` → resolveNode path.

## Python f-string pitfall when building CDP JS expressions

Building a CDP `Runtime.evaluate` expression that contains a JS **object literal**
(`{x: ..., y: ...}`) inside a Python **f-string** silently corrupts the generated
JS. `f"(() => {{ const el = document.querySelector({json.dumps(sel)}); ... return {x: r.left + r.width/2, y: ...}; }})()"` produces `SyntaxError: Unexpected token '}'` in Chrome (the `{{`/`}}` escaping interacts badly with the inner `{x:` literal). The error surfaces only at runtime as "Element not found" / null eval — easy to misdiagnose as a socket problem.

**Fix:** build the JS with **plain string concatenation**, never an f-string,
when the body contains `{...}` object literals:
```python
expr = ("(() => { const el = document.querySelector(" + json.dumps(sel)
       + "); if (!el) return null; const r = el.getBoundingClientRect();"
       + " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()")
```
This is a general gotcha for any tool that constructs CDP JS payloads in Python.

## Keep Chrome FOREGROUND, not just screen-awake

Android sleeps a backgrounded Chrome's devtools socket quickly. The user keeping
"the screen awake" is not enough if Chrome is only a side panel / background app
— switch to Chrome so it is the **foreground** app, and stay in it while the
agent drives the browser. If the user switches apps mid-session, the socket goes
stale and `Runtime.evaluate` returns null even though `/json/version` still
answers 200. Symptom: `/json/version` is 200 but every `Runtime.evaluate`
returns `null` → the tab process is backgrounded, not disconnected.

`tools/browser_raw_cdp.py` implements the above. `_run_browser_command` in
`tools/browser_tool.py` hands off to it when `browser.cdp_url` is set and no
cloud (Browserbase) session is active. The inactive-window satisfaction is the
only thing that stops a green E2E, and it is handled by the `attached`-filter +
probe-cap + bounded-recv patterns above — which must be kept even after the core
patch lands, because inactive Chrome windows exist on any real Android device.

## Verifying CDP code when the live link is DOWN

The CDP endpoint wedges easily under inactive-window saturation (HTTP 500 →
ConnectionRefused on 9222 → even the adb wireless-debug link dropping to
offline). You will frequently need to verify a code change (backend filter,
plugin health signal, forwarder logic) when **no live phone is reachable**.

Do NOT claim a change works on the strength of a stale run. Verify it in
isolation with an ad-hoc script:

- Write a throwaway verifier at an **OS-safe temp path**: on Termux `/tmp` is
  non-writable, so use `$TMPDIR/hermes-verify-<name>.py`
  (or `$HOME`). Never write to `/tmp`.
- **Stub the network** so the test needs no live endpoint. For the saturation
  gauge, `monkeypatch http.client.HTTPConnection` to return a `FakeConn` serving
  a synthetic `/json/list` body (valid array, non-list JSON, and a
  `ConnectionRefusedError` case). For page-target logic, stub `websockets.connect`.
  Cover the success path AND the degraded paths (None on error, no raise).
- Assert the **threshold/branch logic** by capturing `sys.stderr` (e.g.
  `contextlib.redirect_stderr`) and checking the advisory text appears when
  count > 50.
- **Delete the script after running** (`rm` the temp file). Leave no artifact —
  the goal is evidence, not a committed test (the repo's `verify_attach.py` /
  `verify_raw_cdp_mock.py` cover the permanent suites).
- Only after the isolated check passes, report. If you also need a live
  end-to-end, do it in a separate step once the phone's wireless debugging is
  re-enabled (new random port → re-run `main.py`).

This discipline lets CDP-side changes be proven correct even on a wedged
endpoint, instead of shipping unverified or waiting for the link to come back.

## Before pushing this repo (or any of the CDP repos) public

These repos once carried real device details (LAN IPs, device serials,
Termux absolute paths). Before any public push, run the **`repo-sanitization`
skill** — it covers the full working-tree + history scrub AND the
commit-message scan (`git log <branch> --format="%B"`), which is the step
that most often gets missed (a scrub commit message itself can leak the very
PII it removes).

Quick pre-push scan:
```bash
grep -rnE "192\.168\.[0-9]+\.[0-9]+|/data/data/com\.termux|api[_-]?key|token" --include="*.py" --include="*.md" .
git log <branch> --format="%B" | grep -nE "192\.168\.|/data/data/com\.termux|token"
```
