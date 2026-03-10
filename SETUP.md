# Setup Guide

This project runs a local FastAPI app for browsing saved GitHub issue and pull-request matches and launching new scans.

## Requirements

- Windows PowerShell
- Python 3.11 or newer
- A GitHub token for higher API rate limits

## 1. Open the project directory

```powershell
Set-Location 'C:\Users\hp\Desktop\ReveloAutomation'
```

## 2. Create a virtual environment

```powershell
python -m venv .venv
```

## 3. Activate the virtual environment

```powershell
.\.venv\Scripts\activate
```

If PowerShell blocks activation, run this once in the current shell and try again:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## 4. Install dependencies

```powershell
pip install -r requirements.txt
```

## 5. Create the environment file

Create `.env` in the project root with at least:

```env
GITHUB_TOKEN=your_github_token_here
```

The app can run without a token, but scans will hit GitHub rate limits much sooner.

## 6. Start the web app

Use either entrypoint from the project root:

```powershell
uvicorn webapp:app --reload
```

or:

```powershell
uvicorn app.main:app --reload
```

## 7. Open the dashboard

Open this URL in your browser:

```text
http://127.0.0.1:8000
```

## 8. Optional CLI usage

Run the scanner directly:

```powershell
python .\github_issue_pr_finder.py
```

Scan one repository:

```powershell
python .\github_issue_pr_finder.py --repo pallets/flask
```

Run nonstop discovery until stopped:

```powershell
$env:RUN_UNTIL_STOP='1'
python .\github_issue_pr_finder.py
```

## 9. Stop the app

Press `Ctrl+C` in the terminal running Uvicorn.

## Troubleshooting

- Make sure you are inside `C:\Users\hp\Desktop\ReveloAutomation` before running commands.
- If the dashboard looks stale after code changes, restart Uvicorn.
- If scans fail too quickly, confirm `GITHUB_TOKEN` is present in `.env`.
- If package installation fails, upgrade pip first with `python -m pip install --upgrade pip`.