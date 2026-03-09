# x-agent — Autonomous X/Twitter Agent

Persona-driven X/Twitter engagement agent using Playwright browser automation.

## Quick Setup (Windows)

```powershell
# 1. Clone
git clone https://github.com/raiseabotter/x-agent.git
cd x-agent

# 2. Install dependencies
pip install playwright anthropic pyyaml
python -m playwright install chromium

# 3. Set API key
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# 4. Extract cookies from Chrome (requires Chrome to be logged into X)
python scripts/setup_cookies.py --from-chrome "Default" --cookie-file data/nagi_x_cookies.json

# 5. Dry run (test without executing actions)
python run.py --dry-run

# 6. Run for real
python run.py
```

## Commands

```powershell
python run.py --config configs/nagi.yaml          # Start agent
python run.py --config configs/nagi.yaml --dry-run # Dry run (log only)
python run.py --status                             # Check status
python run.py --stop                               # Stop agent
```

## Cookie Setup

The agent uses browser cookies (not API keys) to interact with X.
You must be logged into X in Chrome first, then extract cookies:

```powershell
# From Chrome profile (recommended):
python scripts/setup_cookies.py --from-chrome "Default"

# If using a non-default Chrome profile:
python scripts/setup_cookies.py --from-chrome "Profile 1"

# Manual login (fallback):
python scripts/setup_cookies.py --manual
```

## Configuration

Edit `configs/nagi.yaml` to adjust:
- `autonomy.level`: `manual` / `semi` / `full`
- `autonomy.max_actions_per_day`: daily action budget
- `timing.cycle_interval_*_minutes`: cycle frequency
- `browser.headless`: set `false` to watch the browser
