# Profile maintenance

The profile uses locally generated SVGs instead of live third-party stats cards. The generator runs on **Python 3.12 using only the standard library**; no pip dependencies are required.

## Refresh locally

From the repository root:

```sh
python3 scripts/generate_stats.py --username mehroj-r
```

Optionally set `GITHUB_TOKEN` or `GH_TOKEN` for higher GitHub REST API rate limits. With an authenticated GitHub CLI:

```sh
GH_TOKEN="$(gh auth token)" python3 scripts/generate_stats.py --username mehroj-r
```

The GitHub CLI is only a convenience for obtaining a local token, not a generator dependency. Never commit tokens or put them in generated files.

Run the tests:

```sh
python3 -m unittest discover -s tests
```

## Sources and metric scope

- **Contributions:** the public contribution calendar is fetched anonymously from annual GitHub contribution pages and parsed with the standard library's `HTMLParser`. Tokens do not grant access to private calendars. Counts reflect publicly visible GitHub contributions, which may include anonymous private totals only if the account has opted into public visibility.
- **Activity window:** contributions, active days (days with at least one contribution), and the longest consecutive contribution streak are measured within the last 365 days. The streak is capped to that window, not a lifetime record.
- **Repositories and stars:** GitHub REST repository endpoints supply current owned public, non-fork repositories and their star counts. “Original public repositories” means these non-fork repositories; stars are summed across them. Private repositories and forks are excluded, even when a token is supplied.
- **Languages:** public REST language endpoints supply default-branch byte counts for those same repositories. GitHub Linguist determines language classification and generated/vendored handling; this is not an independent scan of every file. Language count is the number of distinct languages with reported bytes. Byte composition describes repository contents, not expertise or time spent coding.

Calendar shades use up to four quantile buckets of nonzero daily contribution counts, not a linear intensity scale. The legend states explicit numeric count ranges; the [generated data tables](github-stats.md) include every daily count.

## Responsive graphics

The refresh writes 16 SVGs under `images/generated/`: desktop `{name}-{light,dark}.svg` and mobile `{name}-{light,dark}-mobile.svg` variants for `banner`, `stats`, `activity`, and `languages`, plus the readable tables in [github-stats.md](github-stats.md).

Each README `<picture>` selects dark mobile first with `(prefers-color-scheme: dark) and (max-width: 540px)`, then light mobile with `(max-width: 540px)`, then dark desktop with `(prefers-color-scheme: dark)`. The default image is light desktop; all images occupy full-width rows.

Mobile graphics reflow rather than simply shrink: stats use two columns, the activity heatmap uses two chronological blocks, language labels sit above their bars, and the banner omits its diagram.

If fetching or validating data fails, the refresh stops and previously published assets remain unchanged. Do not replace a failed refresh with zero values or publish partially refreshed output.

## Automated refresh

[`.github/workflows/update-profile.yml`](../.github/workflows/update-profile.yml) runs daily and can also be started manually through GitHub Actions. It generates the assets and tables, committing changes only from this allowlist:

- `images/generated/`
- `docs/github-stats.md`

README and maintenance edits remain manual. The workflow uses the built-in `GITHUB_TOKEN`; no extra secrets are needed. Repository settings must allow Actions write permission (`contents: write`). Branch protection can block automated commits, and GitHub may disable scheduled workflows in inactive public repositories; re-enable the schedule or run it manually when needed.
