#!/usr/bin/env python3
"""Run the deterministic browser-backend test suites standalone (no pytest).

Matches CI: the workflow invokes the individual test files as scripts; this
aggregator is the one-shot local equivalent. Exits non-zero if any suite
fails.
"""
import sys

import tools.tests.test_browser_android_cdp_inactive_windows as inactive
import tools.tests.test_browser_android_cdp_targeted as targeted

rc = targeted.run()
rc += inactive.run()
sys.exit(1 if rc else 0)
