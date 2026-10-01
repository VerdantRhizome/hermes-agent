"""Handlers for ``hermes tier show|set|check``. See agent/tier_config.py for the tier maps and
apply/diff/check/probe logic; this module only prints and drives load_config()/save_config()."""
from __future__ import annotations


def _fmt_entry(entry) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    base = f"{entry.get('model', '?')} (via {entry.get('provider', '?')})"
    if entry.get("reasoning_effort"):
        base += f" [{entry['reasoning_effort']}]"
    return base


def _fmt_role_value(value) -> str:
    if isinstance(value, list):
        return "; ".join(_fmt_entry(e) for e in value) or "(empty)"
    return _fmt_entry(value) if value else "(unset)"


def cmd_tier_show(args) -> None:  # noqa: ARG001
    from hermes_cli.config import load_config
    from agent.tier_config import load_tiers, diff_tier_vs_live

    config = load_config()
    tiers = load_tiers(config)
    active = (config.get("tiers") or {}).get("_active")
    print(f"\n  Active tier (last `hermes tier set`): {active or '(none recorded)'}\n")
    for name in ("paid", "free"):
        tier = tiers.get(name) or {}
        print(f"  --- {name} ---")
        for role, value in tier.items():
            if role.startswith("_"):
                continue
            print(f"    {role:<20} {_fmt_role_value(value)}")
        diffs = diff_tier_vs_live(config, name)
        if diffs:
            print(f"    ({len(diffs)} role(s) differ from live config — run `hermes tier set {name}` to apply)")
        else:
            print("    (matches live config)")
        print()


def cmd_tier_set(args) -> None:
    from hermes_cli.config import load_config, save_config
    from agent.tier_config import check_tier, apply_tier

    name = args.name
    config = load_config()
    rows = check_tier(config, name)
    missing = [r for r in rows if not r["ok"]]
    if missing:
        print(f"\n  ✗ Refusing to set tier '{name}': {len(missing)} role(s) missing credentials.\n")
        for r in missing:
            print(f"    {r['role']:<20} {r['provider']}/{r['model']} — needs {r['missing_key_env']}")
        print("\n  No changes made. Configure the missing credential(s) and retry.\n")
        raise SystemExit(1)

    apply_tier(config, name)
    save_config(config)
    print(f"\n  ✓ Tier '{name}' applied ({len(rows)} role(s) checked, all credentialed).")
    print("  Gateway: run `/restart` in-channel to apply to the main loop.")
    print("  Run `hermes fallback list` to see the new fallback chain.\n")


def cmd_tier_check(args) -> None:
    from hermes_cli.config import load_config
    from agent.tier_config import load_tiers, check_tier, live_probe_model

    config = load_config()
    tiers = load_tiers(config)
    names = [args.name] if args.name else list(tiers.keys())
    overall_ok = True
    for name in names:
        print(f"\n  --- tier check: {name} ---")
        rows = check_tier(config, name)
        for r in rows:
            if not r["ok"]:
                overall_ok = False
                print(f"    ✗ {r['role']:<20} {r['provider']}/{r['model']} — missing {r['missing_key_env']}")
                continue
            ok, detail = live_probe_model({"provider": r["provider"], "model": r["model"]})
            overall_ok = overall_ok and ok
            mark = "✓" if ok else "✗"
            print(f"    {mark} {r['role']:<20} {r['provider']}/{r['model']} — {detail}")
    print()
    if not overall_ok:
        raise SystemExit(1)


def cmd_tier(args) -> None:
    sub = getattr(args, "tier_command", None)
    handler = {"show": cmd_tier_show, "set": cmd_tier_set, "check": cmd_tier_check}.get(sub)
    if handler is None:
        print(f"Unknown tier subcommand: {sub}")
        print("Use one of: show, set, check")
        raise SystemExit(2)
    handler(args)
