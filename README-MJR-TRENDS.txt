# MJR Advertising Trends — GitHub Setup

This version avoids PHP on the Media Jobs Report website.

## What it does

GitHub Actions privately stores the Census API key, calls the U.S. Census Bureau Monthly Retail Trade API, generates `mjr-advertising-trends.json`, and commits that safe JSON file back to the repository.

The API key never appears in the JSON or in the public MJR Sales Manager HTML.

## Install in the existing MJR GitHub repository

Upload these two files at the repository root / workflow path:

- `generate-advertising-trends.js`
- `.github/workflows/mjr-advertising-trends.yml`

## Add the Census key

In GitHub:

Settings → Secrets and variables → Actions → New repository secret

Name it exactly:

`CENSUS_API_KEY`

Paste the Census API key as its value.

## Run the first test

GitHub → Actions → MJR Advertising Trends → Run workflow

If successful, a new file will appear in the repository root:

`mjr-advertising-trends.json`

## Connect Sales Manager

MJR Sales Manager V1.7 currently expects:

`/images/mjr-advertising-trends.json`

There are two easy deployment choices:

### Choice A — Recommended for the final site
Have the GitHub workflow publish/copy the generated JSON into the Media Jobs Report `/images/` directory using the same deployment mechanism already used by other MJR-generated files.

Then no Sales Manager code needs to change.

### Choice B — Quick live test
Point `TREND_FEED_URL` in Sales Manager to the public raw GitHub URL for `mjr-advertising-trends.json`.

This is useful to test the feature before wiring an automatic website upload.

## Schedule

The included workflow runs twice daily and can also be run manually. The Census seasonality calculation itself changes slowly, so twice daily is more than enough; later it can be reduced to once daily or only after Census updates.

## Important

This is an MJR-derived seasonality model. It does not copy the RAB chart. It computes monthly shares from Census source data and converts them into forward-looking prospecting signals.
