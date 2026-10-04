"""``hermes tier`` — switch the whole inference stack (main/fallback/aux/delegation/verify-gate)
via one named tier map. See agent/tier_config.py for the tier definitions and apply/diff/check
logic; this module is just the CLI (argparse wiring + printing).

Spec: hermes-model-routing skill, references/inference-selector-spec.md.
"""
from __future__ import annotations


def build_tier_parser(subparsers) -> None:
    from hermes_cli.tier_cmd import cmd_tier

    parser = subparsers.add_parser(
        "tier", help="Switch the whole inference tier (main/fallback/aux/delegation/verify-gate)",
        description="Manage named inference tiers that switch the whole routing "
            "stack as one unit. See references/inference-selector-spec.md in the "
            "hermes-model-routing skill.")
    tier_subparsers = parser.add_subparsers(dest="tier_command")
    tier_subparsers.add_parser("show", help="Render every tier and diff against live config")
    set_p = tier_subparsers.add_parser("set", help="Apply a tier (refuses on missing credentials)")
    set_p.add_argument("name", help="Tier to apply")
    check_p = tier_subparsers.add_parser("check", help="Live-probe every role's model")
    check_p.add_argument("name", nargs="?", default=None,
                          help="Tier to check (default: both)")
    parser.set_defaults(func=cmd_tier)
