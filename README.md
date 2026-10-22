# mwpull

Pull or sample documents from a Meltwater search into a CSV (opens in Excel) or JSONL file, using Meltwater's Export API.

**New to this? Start with the [step-by-step guide (PDF)](https://github.com/willdaniel-MW/mwpull/releases/latest).** It assumes no technical background.

You need a Meltwater API key and your company ID. Ask your Meltwater admin, or see Meltwater's [developer docs](https://developer.meltwater.com).

## Install

Requires Python 3.9+ and [pipx](https://pipx.pypa.io) (`brew install pipx && pipx ensurepath`).

```bash
pipx install git+https://github.com/willdaniel-MW/mwpull.git
mwpull --version
pipx upgrade mwpull      # later, to update
```

The repo is public, so no GitHub login is needed.

## First use

```bash
mwpull profile add acme --set-default          # asks for your API key (hidden) and company ID
mwpull pull --search-id <ID> --months 3 --per-slice 5000 --dry-run     # preview, creates nothing
mwpull pull --search-id <ID> --months 3 --per-slice 5000               # run it
```

Your key is stored only in `~/.meltwater/profiles.json` (readable by you alone). It is never written to run folders.

**No search ID yet?** Save a boolean query as a new search and pull from it in one step:

```bash
mwpull pull --boolean '"acme" AND (launch OR recall)' --search-name "Acme launch" --months 3 --per-slice 5000
mwpull search create --name "Acme launch" --boolean '"acme" AND (launch OR recall)'   # or just create it
mwpull search create        # simplest: asks for the name, then the boolean (paste it as-is, no quoting)
```

On the command line, wrap the boolean in **single** quotes (it can contain double quotes); `--boolean` with no value, as in `mwpull pull --boolean --months 3 ...`, asks you to paste it.

mwpull prints the new search's ID so you can reuse it. Search names must be unique in your account, and the new search appears in Meltwater like any other.

Set where output goes once (override per run with `--out-dir`):

```bash
mwpull config set out_dir ~/Documents/pulls      # also: config set format jsonl | config list | config unset out_dir
```

Each run gets its own folder (`<out_dir>/<name>/`) with the data file, a manifest of what was run, and its progress. `mwpull resume <run folder>` continues an interrupted run.

## How pulls work

`mwpull pull` uses the [Export API](https://developer.meltwater.com/guides/listening/exporting-mentions) (`POST /v3/exports/one-time`): one request per date slice returns thousands of documents (up to 2,000,000 per export) without using search calls.

- One export per month by default; change with `--split week|day|none`.
- Size it with `--per-slice N` (each slice), `--max-docs N` (total, split by days), or give no size to export everything (asks first).
- `--mode per-day` = daily slices, `random` = one export, `proportional`/`all` = monthly.
- Samples are random and approximate (about ±10% per export). Exports have no language filter.
- **One export can cover at most 12 months** (unless your contract says otherwise); mwpull refuses longer ones. Use `--split month`, or `--allow-long-window` if your contract allows it.
- Exports take about a minute to an hour each. Meltwater deletes finished export data after 30 days, but mwpull downloads it for you right away.
- `--resume` never creates a second export for a slice that already has one.

CSVs start with a UTF-8 byte-order mark so Excel shows non-English text correctly. For scripts that read plain UTF-8 (pandas, `csv`), add `--pandas`.

Need exact per-day quotas? Use the search API: `mwpull pull --via search --mode per-day --per-day 100 ...`. It returns at most 100 documents per call (1,000 per query), so large pulls spend most of a day's call budget.

## Usage limits

See Meltwater's [usage-limits guide](https://developer.meltwater.com/guides/getting-started/usage-limits) for the current numbers. In short:

- Earned-media search: 5 calls/second and 100 calls/minute (mwpull paces itself at 90/minute by default).
- A daily call allowance (search calls count) and a monthly export allowance, both set by contract.
- A monthly **document** allowance, shared by exports and searches. Allowances are per company: everyone using the same company ID shares them, and a new API key doesn't reset them.
- Export creation: 20 per minute.

```bash
mwpull profile limits acme --calls-per-day 100 --docs-per-month 100000   # your contract's numbers; 0 clears a limit
mwpull usage --profile acme --remote      # budgets, what's been used, and when a block resets
mwpull pull ... --wait                    # if a daily limit stops the run, sleep until the reset and continue
```

mwpull tracks usage per company, learns limits from Meltwater's `RateLimit-*` response headers, stops before exceeding a budget, and tells you when a limit resets. Progress is always saved.

## Updates

`pull`, `resume` and `probe` check (at most once a day, 3-second timeout, silent on any failure) whether a newer `vX.Y.Z` tag exists on GitHub and print one line to stderr if so. It never blocks a run.
`mwpull check-update` checks right now. Opt out with `mwpull --no-update-check pull ...` or `MWPULL_NO_UPDATE_CHECK=1`.
To update, run `pipx upgrade mwpull`, or the command the notice prints.

## Develop

```bash
git clone https://github.com/willdaniel-MW/mwpull.git && cd mwpull
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

To release: bump `version` in `pyproject.toml` and `src/mwpull/__init__.py`, then `git tag vX.Y.Z && git push --tags`, and attach the guide PDF to the GitHub release.

Layout: `src/mwpull/` (`cli`, `client`, `export`, `fetch`, `sampling`, `runner`, `limits`, `output`, `profiles`, `config`, `update`).
