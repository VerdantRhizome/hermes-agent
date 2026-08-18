# Skill Plan Cache (SPC)

**Status:** DRAFT — workshopped on a fork. Decided (2026-08-19): ships as a
**standalone plugin**, not an in-tree contribution. This document now
records the plugin's design for future implementation, not a PR intended
against `NousResearch/hermes-agent` core — see open question #3's
resolution for the reasoning.

**Author:** Adrien Evian (fork: VerdantRhizome/hermes-agent), drafted with agent
assistance.

**Inspired by (not implementing):**
- Zhang, Wornow, Olukotun. *Agentic Plan Caching: Test-Time Memory for Fast
  and Cost-Efficient LLM Agents.* NeurIPS 2025. arXiv:2506.14852. Test-time
  memory that extracts, stores, adapts, and reuses structured plan templates
  across semantically similar agent tasks, matched by keyword extraction and
  adapted by a lightweight model — reported ~46–50% cost/latency reduction.
- *SkillRT: Compiling Skills for Efficient Execution Everywhere*
  (arXiv:2604.03088). AOT/JIT compilation of "skill" text into optimized
  structures with environment binding and concurrency extraction, so a
  runtime parses compiled artifacts instead of re-feeding raw skill text to
  the model every time.

Neither paper's system is proposed here verbatim. Hermes' skills are
markdown procedures loaded by progressive disclosure, not the graph/DSL
representations either paper compiles — this RFC adapts the *idea* (cache a
successful plan, replay it cheaply on a recognized-similar task) to Hermes'
actual architecture, scoped down hard to respect the Footprint Ladder and
prompt-cache-safety rules in `AGENTS.md`.

## Problem

A skill like `cad-fem-integration-testing` or the FreeCAD/Gmsh pipeline in
this user's own workflow gets invoked repeatedly with the same underlying
procedure and only the parameters changing (part geometry, mesh size, load
value). Every invocation currently re-derives the tool-call sequence from
scratch: the LLM re-reads the skill text, re-reasons about which
terminal/execute_code calls to make, and re-generates the glue script
(bash/python) — full input + output token cost and full latency, even though
the *procedure* is identical to five runs ago and only the numbers changed.

This is exactly the class of waste APC targets ("plans," not chat turns) and
SkillRT targets ("skills," not chat turns) — but Hermes has no analog. Skills
are pure text today: no compiled artifact, no plan memoization, no
fingerprint-matched replay path.

## Non-goals (v0)

- **Not** a new core model tool. Every core tool ships on every API call
  (`AGENTS.md`, Footprint Ladder) and plan-cache lookup/replay does not meet
  the bar for "fundamental, broadly useful to nearly every user."
- **Not** automatic silent replay. A cached plan re-executing a bash/rust/
  system-binary sequence without the model in the loop is a security
  surface — replay must go through the same approval gates
  (`security.approval_mode`, terminal command approval) as a freshly
  LLM-generated command. No bypass.
- **Not** general-purpose semantic caching of arbitrary chat turns — that's
  provider-side prompt caching, which Hermes already has
  (`agent/prompt_caching.py`) and is orthogonal to this.
- **Not** mid-conversation cache mutation. Recording happens at
  `on_session_end`; promotion is checked at the start of a later,
  separate skill invocation — never mid-turn, so neither step can
  invalidate the provider prompt cache the way a rebuilt system prompt
  would.

## Proposal (v0, scoped for real review)

A **plugin** (Footprint Ladder rung 4) plus a **CLI command** (rung 2). No
core tool.

### 1. Capture: two separate triggers, not one

Two distinct decisions were originally conflated under a single
"propose caching at `on_session_end`" trigger. Once slot identification
requires diffing across multiple real runs (see open question #2's
resolution below), a single-run trigger no longer makes sense — capture
splits into **recording** (cheap, silent, per-run) and **promotion**
(the point a cached plan actually becomes trustable and replayable).
Resolved design, see "Open questions" §1 below for the reasoning:

- **Recording** — `on_session_end`, silent, automatic, gated only on the
  invoked skill's tool calls all succeeding and the skill having opted in
  via its own `SKILL.md` frontmatter config. No user prompt. This is
  cheap evidence-journaling, not a durable/trust-bearing artifact — same
  posture as the existing plugin state facade (atomic write, quota, no
  approval needed).
- **Promotion** — checked lazily, at the *start* of the next invocation of
  a skill that already has ≥2 recorded successful runs and no promoted
  plan yet. The agent asks once, inline, before proceeding with the
  traditional re-derivation:

  ```
  This looks like a repeatable procedure you've run before. Save it as a
  cached plan for `cad-fem-integration-testing`? [y/N]
  ```

  On confirmation, the plugin diffs the recorded runs to identify slots
  (symbolic, not model-judged — see open question #2's resolution) and
  writes the plan manifest.

This is lazy evaluation at the next natural use point: no new background
scan job (rejected — speculative infra per `AGENTS.md`), and no
mid-task interruption for a housekeeping question unrelated to what the
user is doing right now (rejected — the immediate-at-2nd-run alternative).
The prompt only ever appears when the user is already about to run that
exact skill again, so it is contextually relevant rather than a
context-switch.

### 2. Storage: plugin state facade, not the skill's own directory

Superseded 2026-08-19 (see open question #3's resolution): the original
plan to write into the skill's own `scripts/`/`cache/` folder via
`skill_manage` was a core-touching design (extending `skill_manage`'s
write allowlist). Since SPC is now decided as a standalone plugin, use the
**plugin state facade** instead — `ctx.state`, documented in
`docs/rfcs/plugin-config-state-bridge.md`, already generally available to
any plugin with zero core involvement: atomic read-modify-write,
profile-scoped, quota'd (10 MiB), fail-closed on malformed data.

Recorded runs and promoted plan manifests are both plugin-owned runtime
data, not user-facing config, so `ctx.state` is the right fit per that
RFC's own "state vs. config" table. Keyed by skill name:

```python
recordings = ctx.state.get(f"recordings.{skill_name}", default=[])
recordings.append(new_run_record)
ctx.state.set(f"recordings.{skill_name}", recordings)

# On promotion:
ctx.state.set(f"plans.{skill_name}.{plan_name}", manifest)
```

The literal script file the agent generated and ran is stored as a string
field inside the manifest (or, if large, a plugin-owned path under
`ctx.state.data_dir`) rather than in the skill's own `scripts/` folder.
This trades away one nice-to-have — a cached plan traveling with the skill
automatically if a user exports/shares it — for zero core surface, which
is the right trade for a standalone plugin whose only stable dependency
should be the `PluginContext` ABC itself.

### 3. Invalidation

The manifest records a hash of the source `SKILL.md` at capture time
(read via normal file access — a plugin can read `~/.hermes/skills/`
directly, no special API needed). If the skill file changes, cached plans
under it are marked stale (not deleted) and skipped until manually
reviewed — the same "fail closed, never silently replay a target that
could be wrong" posture used for the plugin config/state bridge's write
validation.

### 4. Replay: explicit plugin-registered CLI command, not automatic

```
hermes spc replay <skill-name> [--plan <name>] -- <substituted args>
```

Registered via `ctx.register_cli_command()` — part of the generic plugin
surface (`CONTRIBUTING.md`), not a core `hermes skills replay`
subcommand as originally sketched. The agent invokes it the same way it
invokes any other `hermes <subcommand>` via `terminal` — guided by a short
addition to the skill's own instructions ("if a matching cached plan
exists, prefer replaying it over re-deriving the procedure"), not a new
tool. The command:

1. Loads the plan manifest, resolves slots from the given args (or asks the
   model to resolve them — still cheaper than re-deriving the whole plan).
2. Prints the resolved script/tool-call sequence for the user/approval gate
   to see before running — same command-approval path a freshly generated
   script goes through.
3. Executes it, reports success/failure back to the agent turn as normal
   tool output.

Fingerprint matching ("is this new request similar enough to replay this
cached plan") is deliberately **not automatic** in v0. The agent decides
whether to call `hermes skills replay` the same way it decides whether to
call any other command — guided by the skill text noting the cache exists.
Automatic keyword/embedding matching (the actual APC mechanism) is a
follow-up once there's a corpus of real captured plans to tune matching
against, not a v0 commitment.

## Footprint Ladder note (historical — kept for the reasoning trail)

The table below reflects the original in-tree framing and the reasoning
that led to it. As of open question #3's resolution (2026-08-19), SPC
ships as a **standalone plugin with zero core surface** — rungs 2-4 are
now all satisfied entirely *within* the plugin (`ctx.register_cli_command`,
`ctx.register_hook`, `ctx.state`), not as core additions. Kept here because
the ladder reasoning is still why the design looks the way it does; it's
no longer "which rung of core do we touch" but "confirmed: none."

| Rung | Used in-tree? | Why |
|---|---|---|
| 1. Extend existing code | No | Superseded — see §2/§4 above; uses stable plugin ABI, not core write paths |
| 2. CLI command + skill | **No core change** | `hermes spc replay` is plugin-registered (`ctx.register_cli_command`), lives in the plugin's own namespace |
| 3. Service-gated tool | No | Not needed — replay is a shell command, not a structured tool call |
| 4. Plugin | **Yes — this is the whole implementation** | Recording (`on_session_end`), promotion check (pre-invocation hook), storage (`ctx.state`), and replay (`ctx.register_cli_command`) are all plugin-owned |
| 5. MCP server | No | No cross-host reuse case yet |
| 6. New core tool | **No** | Explicitly rejected — the whole point is to avoid adding schema weight for a capability most users won't exercise |

## Open questions to workshop before this goes near upstream

Historical framing — see note above the ladder table: SPC is not headed
upstream as a core contribution. The "open questions" below are kept as
the design-decision record; items 1-3 are resolved.

1. ~~**Where does the "propose caching this" prompt live**~~ — **RESOLVED,
   2026-08-18.** The original framing (a single choice between
   `on_session_end` auto-proposal vs. a fully manual `/skills cache-plan`
   command) turned out to conflate two separate decisions that the
   slot-verification design (question #2, resolved below) already forces
   apart, since slot identification needs a diff across ≥2 real runs and
   can't fire off a single session:

   - **Recording** (evidence-journaling, cheap, per-run) happens silently
     at `on_session_end` whenever an opted-in skill's tool calls all
     succeeded — no prompt, no promotion, just saving the raw sequence.
   - **Promotion** (turning ≥2 recorded runs into an actual replayable
     cached plan) is checked lazily, at the *start* of the next invocation
     of a skill that already has ≥2 recorded runs and no promoted plan.
     The agent asks once, inline, right before it would otherwise
     re-derive the procedure from scratch.

   Two alternatives were considered and rejected: promoting immediately
   at the 2nd run's `on_session_end` (interrupts the user mid-task with an
   off-topic housekeeping question at the exact moment they're focused on
   the real work) and a periodic background scan job (new speculative
   infra with no concrete consumer yet, against `AGENTS.md`'s explicit
   guidance). The lazy-at-next-invocation check costs nothing new — it
   reuses the skill-dispatch path the plugin already has to touch for
   recording — and the prompt only ever surfaces when the user is already
   about to run that exact skill again, so it reads as contextually
   relevant rather than a context-switch. See "1. Capture" above for the
   updated proposal text.
2. **Slot extraction quality** — a cheap auxiliary-model pass may mis-slot
   values that look like literals but are load-bearing (e.g. a unit string).
   Needs a concrete eval before trusting it on anything safety-relevant
   (see `AGENTS.md` punch-list item #1 — this user has already hit a silent
   unit-misinterpretation bug in a downstream tool; a caching layer must not
   add a second one).

   **Proposed answer — don't let a model guess slots, verify them
   symbolically first:**

   The single-shot "ask a model which values look like parameters" approach
   is exactly the shape of failure that produced the FreeCAD unit bug: a
   model can plausibly mis-generalize a load-bearing constant (a unit
   string, a solver flag, a precision digit count) as safely swappable data.
   Don't give the model that judgment call at all where it can be avoided.

   - **Slot *identification* is symbolic, not model-judged.** Require at
     least two successful captures of the same skill before a plan is
     eligible for caching at all (this is a `cache` state transition already
     implicit in the "propose caching" flow in step 1 — a plan needs N≥2
     real runs before promotion, not one). Anti-unify / AST-diff the two
     tool-call sequences: **only positions that actually varied across the
     two real runs are candidate slots.** A value that was constant across
     every observed run stays a literal, full stop — a model is never asked
     to guess "is this safe to generalize" on unseen data, because the slot
     boundary comes from empirical evidence, not inference. This also
     directly catches the class of bug in punch-list item #1: a unit
     string (`"MPa"`) is constant across every run of the same skill and
     therefore is never proposed as a slot; a mesh size or file path that
     legitimately changes run-to-run is.
   - **Slot *labeling/naming* (a much lower-stakes task — assigning a
     human-readable name and JSON schema type to an already-proven slot,
     not deciding whether it's a slot) is a good fit for a cheap
     structured-output model call**, since a wrong label is a cosmetic/UX
     problem, not a silent-wrong-value problem. Candidates worth spiking,
     roughly best-fit-first for *this* sub-task specifically:
     - **DeepSeek-V4 (Flash, free via HF)** — already this user's
       fallback-chain model, proven tool-call reliability, zero new vendor
       or cost to introduce.
     - **MiniMax M2.5** — independently benchmarked (2026) as unusually
       disciplined at bare, unwrapped JSON output (98.6% quality, 100%
       format compliance across a 38-task structured-output benchmark) at
       ~$0.07/run — exactly the profile wanted for a machine-parsed plan
       manifest with no human reading the raw output.
     - **GLM 5.1/5.2** — MIT-licensed, structured-coding benchmarks
       competitive with closed frontier models; fits this user's ongoing
       provider-consolidation goal (z.AI coding plan evaluation already
       underway).
   - **Final template/script emission is a plausible fit for a diffusion
     code model** (Mercury Coder, LLaDA 2), spiked separately from the
     labeling step and purely as a latency optimization, not a
     safety-relevant one. The task at that point — fill in already-verified
     slot positions in an already-fixed-structure script — is closer to
     masked inpainting than open-ended left-to-right generation, which is
     the diffusion-LM architectural strength (bidirectional refinement,
     revise-any-position, reported 2–10x latency win over autoregressive
     models at this profile). This is unproven for the slot-*judgment* step
     specifically (no benchmark evidence yet of diffusion coders reasoning
     about unit-bearing vs. freely-swappable values) — treat it strictly as
     an optional final-stage speed optimization once slots are already
     symbolically verified and labeled, never as a replacement for the
     verification step above.

   Net effect: a three-stage pipeline where the only stage touching a raw
   LLM judgment call is the lowest-stakes one.

   ```
   symbolic diff across ≥2 captured runs   → WHICH positions are slots (no LLM)
        ↓
   cheap structured-output LLM              → label/name slots, write JSON manifest
   (DeepSeek-V4-Flash / MiniMax M2.5 / GLM 5.x)
        ↓
   (optional, latency-motivated) diffusion coder → fast final template/script emission
   (Mercury Coder / LLaDA 2)
   ```

   This still needs a concrete eval once a prototype exists — the above is
   a proposed design, not a validated one.
3. ~~**Does this belong in core `plugins/` at all**~~ — **RESOLVED,
   2026-08-19: standalone plugin, not in-tree.** SPC is exactly the shape
   `CONTRIBUTING.md` describes for standalone placement — niche, not
   broadly needed by most users, and durability against a fast-moving core
   matters more here than it would for something with a maintainer
   sponsor. Concretely:

   - **No core change required at all**, on reflection — the "one piece
     that plausibly needs a core change" flagged in the original framing
     (extending `skill_manage`'s write allowlist with a `cache/`
     subfolder) turns out to be avoidable. Plan manifests don't need to
     live physically inside the skill's own directory; they can use the
     plugin state facade (`ctx.state`, documented in
     `docs/rfcs/plugin-config-state-bridge.md`) instead — atomic
     read-modify-write, profile-scoped, quota'd, already generally
     available to any plugin with zero core involvement. This trades away
     one nice-to-have (a cached plan traveling with the skill if a user
     exports/shares it) for a real gain: **zero core surface**, so the
     plugin has nothing that can break on an upstream refactor beyond the
     stable `PluginContext` ABC itself.
   - The CLI replay command doesn't need a core `hermes skills replay`
     subcommand either — `ctx.register_cli_command()` is already part of
     the generic plugin surface (`CONTRIBUTING.md`, "Third-Party Product
     Integrations"), so the plugin registers its own `hermes spc replay`
     (or similar) entirely within its own namespace.
   - Recording and promotion hooks (`on_session_end`, the pre-invocation
     promotion check) use `ctx.register_hook()`, also fully generic —
     nothing SPC needs is special-cased in core.
   - Net result: SPC ships as a **standalone plugin repo**
     (`~/.hermes/plugins/skill-plan-cache/` or a pip entry point),
     following the exact pattern `CONTRIBUTING.md` lays out for
     third-party/niche capability, using only `register_hook`,
     `register_cli_command`, `ctx.state`, and `ctx.get_config`/
     `ctx.set_config` (per-plugin settings namespace, e.g. which skills
     have recording opted in) — all stable, already-shipped plugin ABI.
     If real usage later surfaces a genuine need the plugin surface
     doesn't cover, that becomes its own narrow, justified request to
     widen the generic plugin surface (per `CONTRIBUTING.md`'s explicit
     guidance: "never special-case your plugin in core") — not a
     standing ask to fold SPC itself into the tree.

   This RFC document now describes the plugin's design, not an in-tree
   contribution — nothing here is intended to become a PR against
   `NousResearch/hermes-agent` core. Promoting the plugin (once built) in
   the Nous Research Discord `#plugins-skills-and-skins` channel, per
   `CONTRIBUTING.md`, is the intended distribution path.
4. **Multi-skill plans** — real workflows chain skills (e.g. FreeCAD skill →
   Blender skill). Out of scope for v0; v0 is single-skill only.

## Concrete dogfood candidate

The user's own FreeCAD → Gmsh → CalculiX FEA pipeline (`cad-fem-integration-
testing`, `cad-agent-frontier` skills) has exactly the repeated-procedure /
varying-parameters shape this targets: mesh + solve steps that are
byte-identical in structure across many runs, differing only in geometry
input and load values. A v0 prototype should be built against that skill
first, not built speculatively.
