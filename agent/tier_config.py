"""Inference-selector tier definitions + apply/diff/check logic.

One knob (`hermes tier set paid|free`) switches the whole routing stack — main
model, fallback_providers, auxiliary.* tasks, delegation, verify-gate escalate
chain — as one unit from a named tier map stored under config.yaml's `tiers:` key.

Spec: hermes-model-routing skill, references/inference-selector-spec.md.
Design decisions there are settled; this module just implements them (~150 LoC target).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

# Role -> config.yaml path (as a tuple of keys) each tier entry maps onto.
# "main" writes model.provider/model.default; "fallback_chain" writes fallback_providers (a list);
# everything else writes a single {provider, model[, reasoning_effort]} dict at that path.
_AUX_ROLES = (
    "aux.vision", "aux.web_extract", "aux.compression", "aux.title_generation",
    "aux.kanban_decomposer", "aux.goal_judge", "aux.curator", "aux.background_review")
_ROLES: Tuple[str, ...] = ("main", "fallback_chain", "delegation", "escalate_chain") + _AUX_ROLES

# READY-SLOT providers: rows kept in the tier map for documentation/future use, but never
# eligible for `tier set` (no creds configured yet) and always refused by `tier check`.
READY_SLOT_PROVIDERS = frozenset({"nvidia", "openai-codex"})

# Providers banned as inference outright (2026-09-26 standing rule) — refused everywhere.
BANNED_INFERENCE_PROVIDERS = frozenset({"huggingface"})

_AUX_KEY_MAP = {
    "aux.vision": "vision", "aux.web_extract": "web_extract", "aux.compression": "compression",
    "aux.title_generation": "title_generation", "aux.kanban_decomposer": "kanban_decomposer",
    "aux.goal_judge": "goal_judge", "aux.curator": "curator", "aux.background_review": "background_review"}


def _entry(provider: str, model: str, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    e: Dict[str, Any] = {"provider": provider, "model": model}
    if reasoning_effort:
        e["reasoning_effort"] = reasoning_effort
    return e


# Tier maps. `main`/`delegation`/aux.* are single entries; `fallback_chain`/`escalate_chain`
# are ordered lists. Free-tier non-main entries default reasoning_effort=medium (2026-09-17
# cost-leak rule) except title_generation/compression which stay cheap-and-terse (none/low) —
# matching the live config's existing pattern, unchanged by tier switches.
DEFAULT_TIERS: Dict[str, Dict[str, Any]] = {
    "paid": {
        "main": _entry("nous", "anthropic/claude-opus-5.5"),
        "fallback_chain": [
            _entry("custom", "pollinations:openai"),
            _entry("venice", "claude-opus-5-5")],
        "delegation": _entry("nous", "anthropic/claude-opus-5.5", "medium"),
        "escalate_chain": [
            _entry("nous", "openai/gpt-6-luna-pro"),
            _entry("venice", "openai-gpt-55-pro")],
        "aux.vision": _entry("pollinations", "openai/gpt-6-luna"),
        "aux.web_extract": _entry("nous", "anthropic/claude-opus-5.5", "low"),
        "aux.compression": _entry("nous", "anthropic/claude-opus-5.5", "low"),
        "aux.title_generation": _entry("nous", "anthropic/claude-opus-5.5", "none"),
        "aux.kanban_decomposer": _entry("nous", "anthropic/claude-opus-5.5", "medium"),
        "aux.goal_judge": _entry("nous", "anthropic/claude-opus-5.5", "medium"),
        "aux.curator": _entry("nous", "anthropic/claude-opus-5.5", "low"),
        "aux.background_review": _entry("nous", "anthropic/claude-opus-5.5", "medium"),
    },
    "free": {
        "main": _entry("custom", "pollinations:openai", "medium"),
        "fallback_chain": [
            _entry("nous", "stepfun/step-3.7-flash:free"),
            _entry("custom", "pollinations:deepseek/deepseek-v4-flash"),
            _entry("nous", "upstage/solar-pro4:free")],
        "delegation": _entry("custom", "pollinations:openai", "medium"),
        "escalate_chain": [
            _entry("nous", "upstage/solar-pro4:free"),
            _entry("custom", "pollinations:openai")],
        "aux.vision": _entry("pollinations", "qwen/qwen3.8-flash", "medium"),
        "aux.web_extract": _entry("nous", "upstage/solar-pro4:free", "low"),
        "aux.compression": _entry("nous", "upstage/solar-pro4:free", "low"),
        "aux.title_generation": _entry("nous", "stepfun/step-3.7-flash:free", "none"),
        "aux.kanban_decomposer": _entry("nous", "stepfun/step-3.7-flash:free", "medium"),
        "aux.goal_judge": _entry("nous", "stepfun/step-3.7-flash:free", "medium"),
        "aux.curator": _entry("nous", "stepfun/step-3.7-flash:free", "low"),
        "aux.background_review": _entry("nous", "stepfun/step-3.7-flash:free", "medium"),
        # READY-SLOTS — no creds today; kept for documentation, refused by tier set/check.
        "_ready_slots": [
            _entry("nvidia", "z-ai/glm-5.3-flash"),
            _entry("openai-codex", "gpt-5.6-luna")],
    },
}


def load_tiers(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Tier maps from config.yaml's `tiers:` key, falling back to DEFAULT_TIERS per-tier
    (a user may have only customized one of the two)."""
    stored = config.get("tiers") if isinstance(config, dict) else None
    tiers = copy.deepcopy(DEFAULT_TIERS)
    if isinstance(stored, dict):
        for name, roles in stored.items():
            if isinstance(roles, dict):
                tiers.setdefault(name, {}).update(copy.deepcopy(roles))
    return tiers


def provider_has_credential(provider: str, config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """(has_cred, missing_key_env). ``missing_key_env`` is the exact env var to set,
    or a short OAuth hint for portal/OAuth providers."""
    from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status, get_api_key_provider_status
    provider = (provider or "").strip().lower()
    if provider == "custom":
        return True, ""  # named custom providers (custom:<name>) checked separately
    if provider == "gemini":
        st = get_api_key_provider_status("gemini")
        return bool(st.get("configured")), "GOOGLE_API_KEY or GEMINI_API_KEY"
    pconfig = PROVIDER_REGISTRY.get(provider)
    if pconfig is None:
        # Not a registry provider — may be a bare named custom provider (config's
        # ``providers.<name>``, e.g. aux.vision using ``provider: pollinations`` directly).
        if config is not None and isinstance(config.get("providers"), dict) and provider in config["providers"]:
            return custom_provider_has_credential(config, provider)
        return False, f"unknown provider '{provider}'"
    if pconfig.auth_type in ("oauth_device_code", "oauth_external", "oauth_minimax"):
        status = get_auth_status(provider)
        return bool(status.get("logged_in")), f"`hermes login {provider}` (OAuth)"
    status = get_api_key_provider_status(provider)
    env_vars = "/".join(pconfig.api_key_env_vars) or "(no key var registered)"
    return bool(status.get("configured")), env_vars


def custom_provider_has_credential(config: Dict[str, Any], custom_name: str) -> Tuple[bool, str]:
    """For `provider: custom, model: "<name>:..."` fallback-style entries: resolve the
    named entry under `providers:` and check its key_env."""
    providers = config.get("providers") if isinstance(config, dict) else {}
    entry = (providers or {}).get(custom_name) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        return False, f"providers.{custom_name} (missing from config.yaml)"
    key_env = str(entry.get("key_env") or "").strip()
    if not key_env:
        return True, ""  # no-key-required custom endpoint
    from hermes_cli.config import get_env_value_prefer_dotenv
    from hermes_cli.auth import has_usable_secret
    return has_usable_secret(get_env_value_prefer_dotenv(key_env)), key_env


def check_role_credential(config: Dict[str, Any], entry: Dict[str, Any]) -> Tuple[bool, str]:
    """(ok, missing_key_env_or_reason) for one tier-map entry."""
    provider = str(entry.get("provider") or "").strip().lower()
    model = str(entry.get("model") or "")
    if provider in BANNED_INFERENCE_PROVIDERS:
        return False, f"provider '{provider}' is BANNED as inference (standing rule, 2026-09-26)"
    if provider in READY_SLOT_PROVIDERS:
        return False, f"'{provider}' is a READY-SLOT — no credentials configured yet"
    if provider == "custom" and ":" in model:
        return custom_provider_has_credential(config, model.split(":", 1)[0])
    return provider_has_credential(provider, config)


def check_tier(config: Dict[str, Any], tier_name: str) -> List[Dict[str, Any]]:
    """Credential-only check (no live HTTP): one row per role with ok/missing_key_env.
    Used by `tier set` to refuse a half-configured switch."""
    tiers = load_tiers(config)
    tier = tiers.get(tier_name) or {}
    rows = []
    for role in _ROLES:
        entries = tier.get(role)
        if entries is None:
            continue
        entries = entries if isinstance(entries, list) else [entries]
        for entry in entries:
            ok, missing = check_role_credential(config, entry)
            rows.append({
                "role": role, "provider": entry.get("provider"), "model": entry.get("model"),
                "ok": ok, "missing_key_env": missing})
    return rows


def live_probe_model(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Live chat-completion probe for one entry: max_tokens=100 (reasoning models return
    empty content at small budgets — the known trap; ~150 LoC budget keeps this best-effort).
    Returns (ok, detail)."""
    provider = str(entry.get("provider") or "").strip().lower()
    model = str(entry.get("model") or "")
    if provider in READY_SLOT_PROVIDERS or provider in BANNED_INFERENCE_PROVIDERS:
        return False, "not probed (READY-SLOT/banned)"
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        # Named custom providers (custom:<name>) resolve by the NAME, not the literal "custom" —
        # the ladder's named-custom rung keys off the requested identity, and target_model carries
        # the model id only (the "<name>:model" shape is a fallback-chain display convention).
        if provider == "custom" and ":" in model:
            custom_name, real_model = model.split(":", 1)
            runtime = resolve_runtime_provider(requested=custom_name, target_model=real_model)
            model = real_model
        else:
            runtime = resolve_runtime_provider(requested=provider, target_model=model)
    except Exception as exc:
        return False, f"credential resolution failed: {exc}"
    api_key = runtime.get("api_key") or ""
    base_url = (runtime.get("base_url") or "").rstrip("/")
    if not base_url:
        return False, "no base_url resolved"
    import httpx
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={"model": model, "messages": [{"role": "user", "content": "reply with OK"}], "max_tokens": 100},
            timeout=30.0)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return bool(content.strip()), (content[:80] if content else "empty content at max_tokens>=100")
    except Exception as exc:
        return False, f"probe failed: {exc}"


def apply_tier(config: Dict[str, Any], tier_name: str) -> Dict[str, Any]:
    """Rewrite live config keys (model.*, fallback_providers, auxiliary.*, delegation.*) from
    `tiers.<tier_name>`. Caller must call check_tier() first and refuse on any missing credential —
    this function does not check, only writes. Mutates and returns `config`."""
    tiers = load_tiers(config)
    tier = tiers.get(tier_name)
    if not tier:
        raise ValueError(f"Unknown tier '{tier_name}'")

    main = tier.get("main")
    if main:
        config.setdefault("model", {})
        config["model"]["provider"] = main["provider"]
        config["model"]["default"] = main["model"]

    if "fallback_chain" in tier:
        config["fallback_providers"] = copy.deepcopy(tier["fallback_chain"])
        config.pop("fallback_model", None)

    delegation = tier.get("delegation")
    if delegation:
        config.setdefault("delegation", {})
        config["delegation"]["provider"] = delegation["provider"]
        config["delegation"]["model"] = delegation["model"]
        if delegation.get("reasoning_effort"):
            config["delegation"]["reasoning_effort"] = delegation["reasoning_effort"]

    if "escalate_chain" in tier:
        config.setdefault("verify_gate", {})
        config["verify_gate"]["escalate_chain"] = copy.deepcopy(tier["escalate_chain"])

    aux = config.setdefault("auxiliary", {})
    for role, aux_key in _AUX_KEY_MAP.items():
        entry = tier.get(role)
        if not entry:
            continue
        slot = aux.setdefault(aux_key, {})
        slot["provider"] = entry["provider"]
        slot["model"] = entry["model"]
        if entry.get("reasoning_effort"):
            slot["reasoning_effort"] = entry["reasoning_effort"]
        # explicit per-job pins (e.g. gmail-triage's glm-5.3-flash) live in agent.reasoning_overrides
        # / cron job bodies, not here — tier switches never touch them.

    config.setdefault("tiers", {})
    for name, roles in tiers.items():
        config["tiers"][name] = roles
    config["tiers"]["_active"] = tier_name
    return config


def diff_tier_vs_live(config: Dict[str, Any], tier_name: str) -> List[Tuple[str, Any, Any]]:
    """(role, tier_value, live_value) rows where the named tier disagrees with the live config."""
    tiers = load_tiers(config)
    tier = tiers.get(tier_name) or {}
    rows: List[Tuple[str, Any, Any]] = []

    model_cfg = config.get("model") or {}
    live_main = _entry(str(model_cfg.get("provider") or ""), str(model_cfg.get("default") or ""))
    if tier.get("main") and (tier["main"].get("provider"), tier["main"].get("model")) != \
            (live_main["provider"], live_main["model"]):
        rows.append(("main", tier["main"], live_main))

    live_fb = config.get("fallback_providers") or []
    if "fallback_chain" in tier and tier["fallback_chain"] != live_fb:
        rows.append(("fallback_chain", tier["fallback_chain"], live_fb))

    live_deleg = config.get("delegation") or {}
    if tier.get("delegation") and (tier["delegation"].get("provider"), tier["delegation"].get("model")) != \
            (live_deleg.get("provider"), live_deleg.get("model")):
        rows.append(("delegation", tier["delegation"], live_deleg))

    live_escalate = (config.get("verify_gate") or {}).get("escalate_chain") or []
    if "escalate_chain" in tier and tier["escalate_chain"] != live_escalate:
        rows.append(("escalate_chain", tier["escalate_chain"], live_escalate))

    aux = config.get("auxiliary") or {}
    for role, aux_key in _AUX_KEY_MAP.items():
        tier_entry = tier.get(role)
        if not tier_entry:
            continue
        live_entry = aux.get(aux_key) or {}
        if (tier_entry.get("provider"), tier_entry.get("model")) != \
                (live_entry.get("provider"), live_entry.get("model")):
            rows.append((role, tier_entry, live_entry))
    return rows
