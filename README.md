# GitHub Issue/PR Finder

This script searches popular Python repositories for non-trivial closed issues that appear to be linked to merged pull requests.

It now supports:
- Resuming across runs
- Skipping repositories that were already scanned
- Saving matches immediately as they are found
- Loading `GITHUB_TOKEN` automatically from a local `.env` file
- Continuous scanning until you stop the process
- Scanning a specific GitHub repo on demand
- A browser UI for matches, targeted scans, and live scan logs
- English-only repository and issue filtering

## Files

- `github_issue_pr_finder.py`: main script
- `github_issue_pr_matches.json`: saved issue/PR matches, created after the first match is found
- `github_issue_pr_scan_state.json`: saved scan progress and scanned repositories, created as the scan advances
- `.env`: optional local environment variables

## Output Format

Each saved match includes:
- `repo`
- `issue_number`
- `issue_title`
- `issue_url`
- `pr_number`
- `pr_url`
- `files_changed`
- `issue_comments`
- `issue_closed_at`
- `pr_merged_at`
- `base_ref`
- `base_sha`
- `merge_commit_sha`
- `checkout_sha`
- `checkout_sha_source`

This gives you the possible issue plus its linked pull request URL in one place.
Use `checkout_sha` when you want to inspect the repository state before the fix was merged.

## Setup



```powershell
pip install requests
```

4. Add your GitHub token either as an environment variable or inside `.env`

Example `.env`:

```env
GITHUB_TOKEN=your_github_token_here
```

The script will still run without a token, but GitHub rate limits will be much stricter.

## Usage

### CLI

Run the script:

```powershell
python .\github_issue_pr_finder.py
```

Scan a specific repo and print only the qualified issues from that repo:

```powershell
python .\github_issue_pr_finder.py --repo pallets/flask
```

You can also pass a GitHub URL or repeat `--repo` for multiple repositories:

```powershell
python .\github_issue_pr_finder.py --repo https://github.com/huggingface/transformers --repo pytorch/pytorch
```

Run continuously until you stop it:

```powershell
$env:RUN_UNTIL_STOP='1'
python .\github_issue_pr_finder.py
```

### Web UI

Start the local web app:

```powershell
uvicorn webapp:app --reload
```

Compatibility entrypoint also supported:

```powershell
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000` in your browser.

The UI includes:
- A saved matches dashboard
- A repo input that triggers a targeted scan
- A live scanning log panel streamed from the backend
- Issue, PR, and checkout SHA details for each match

## English-only Filtering

By default, the scanner now accepts only:
- Repositories whose metadata looks English
- Issues whose title and body look English

This filter is applied to both the broad discovery scan and targeted `--repo` scans.

## Resume Behavior

On rerun, the script will:
- Load previously found matches from `github_issue_pr_matches.json`
- Load already scanned repositories from `github_issue_pr_scan_state.json`
- Skip repositories that were already completed
- Continue collecting until it reaches the target number of matches

When using `--repo`, the script scans the requested repo directly even if it was scanned earlier in the broad discovery mode.

In continuous mode, the script also:
- Tracks the current repository star bucket in the scan state
- Moves down through lower-star repositories after each bucket is exhausted
- Restarts from the highest-star repositories after a full sweep, pausing between sweeps

## Configurable Environment Variables

You can override the defaults without editing the script:

```env
GITHUB_TOKEN=your_github_token_here
TARGET_MATCHES=100
MIN_STARS=200
MIN_FILES_CHANGED=4
MAX_REPO_PAGES=10
MAX_PULL_PAGES_PER_REPO=5
REPOS_PER_PAGE=100
PULLS_PER_PAGE=100
REQUEST_TIMEOUT=30
RUN_UNTIL_STOP=0
FULL_SWEEP_PAUSE_SECONDS=900
ENGLISH_ONLY=1
```


## Notes

- Matches are saved immediately when found, so partial progress is preserved even if the run stops.
- A repository is only added to the scanned state after all configured pull-request pages for that repository are processed.
- The search logic looks for merged pull requests whose body references issues with `fixes`, `closes`, or `resolves` style keywords.
