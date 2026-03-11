# Nagi Memory & Drift (ゆらぎ) System — Plan

**Date**: 2026-03-11
**Status**: Implementation

## Architecture

1. **Memory accumulation** — Each cycle, record observations (liked, saw, posted) to per-day JSONL
2. **Diary generation** — At start of new day, LLM summarizes previous day into Markdown diary
3. **Memory injection** — Last N days of diary summaries injected into system prompts
4. **Drift** — Emerges organically from memory injection (no explicit parameter mutation)

## File Layout

```
data/
  memory/
    nagi_obs_2026-03-11.jsonl   # today's raw observations
  diary/
    nagi_2026-03-11.md          # LLM-written diary (previous day)
```

## New File: `src/nagi_memory.py`

`NagiMemory` class with:
- `record_observation(decision, tweet)` — write to obs JSONL
- `record_post(content)` — write post to obs JSONL
- `maybe_generate_diary()` — generate previous day's diary if new day detected
- `get_memory_block()` — return compact text for prompt injection

## Changes to `src/x_agent.py`

5 injection points:
1. `__init__`: instantiate NagiMemory
2. `_run_cycle()` top: `maybe_generate_diary()`
3. `_run_cycle()` after like: `record_observation()`
4. `_maybe_spontaneous_post()` after post: `record_post()`
5. `_build_system_prompt()` + `_generate_spontaneous_post()`: inject memory block

## Config (`configs/nagi.yaml`)

```yaml
memory:
  enabled: true
  inject_days: 5
  max_obs_per_cycle: 3
  diary_max_tokens: 400
```

## Token Budget

~400 tokens added to system prompts (5 days × ~80 tokens/day). Negligible.

## Anti-repetition

- Diary prompt: "Note repetitive themes"
- Memory block header: "use to evolve, not repeat"
- Post prompt: "If same topic multiple days, explore different angle"
