#!/usr/bin/env python3
"""Build profile graphics from publicly visible GitHub data, without dependencies."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
THEMES = {
    "light": {
        "surface": "#ffffff", "panel": "#f6f8fa", "ink": "#172033",
        "muted": "#526078", "line": "#d8dee8", "accent": "#2a78d6",
        "heat": ["#edf1f7", "#9ec5f4", "#5598e7", "#256abf", "#104281"],
    },
    "dark": {
        "surface": "#0d1117", "panel": "#161d29", "ink": "#eef2f8",
        "muted": "#9daec5", "line": "#2c3748", "accent": "#3987e5",
        "heat": ["#1e293b", "#184f95", "#256abf", "#5598e7", "#9ec5f4"],
    },
}


def fetch(url, token=None):
    headers = {"User-Agent": "mehroj-r-profile", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        status = error.code
        error.close()
        # Installation tokens can be restricted to their repository. Public
        # endpoints remain accessible anonymously; never fall back to private data.
        if token and status in (403, 404):
            return fetch(url)
        raise RuntimeError(f"GitHub request failed ({status}): {url}") from None


def fetch_json(url, token=None):
    return json.loads(fetch(url, token))


def public_repositories(username, token=None):
    repositories = []
    page = 1
    while True:
        batch = fetch_json(
            f"https://api.github.com/users/{username}/repos?per_page=100&type=owner&page={page}",
            token,
        )
        if not isinstance(batch, list):
            raise ValueError("GitHub did not return a repository list")
        repositories.extend(
            repo for repo in batch
            if not repo["private"] and not repo["fork"]
            and repo["owner"]["login"].lower() == username.lower()
        )
        if len(batch) < 100:
            return repositories
        page += 1


def language_totals(repositories, token=None):
    totals = Counter()
    for repo in repositories:
        # Construct the endpoint instead of forwarding credentials to response URLs.
        full_name = repo["full_name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            raise ValueError("Invalid repository name from GitHub")
        languages = fetch_json(f"https://api.github.com/repos/{full_name}/languages", token)
        if not isinstance(languages, dict) or any(
            not isinstance(size, int) or size < 0 for size in languages.values()
        ):
            raise ValueError("Invalid language byte counts from GitHub")
        totals.update(languages)
    return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


class ContributionParser(HTMLParser):
    """Read dated calendar cells and their text tooltips, not color intensities."""

    def __init__(self):
        super().__init__()
        self.cells = {}
        self.tooltips = {}
        self.tooltip_id = None
        self.tooltip_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-date" in attrs:
            self.cells[attrs["id"]] = date.fromisoformat(attrs["data-date"])
        if tag == "tool-tip":
            self.tooltip_id = attrs.get("for")
            self.tooltip_text = []

    def handle_data(self, data):
        if self.tooltip_id:
            self.tooltip_text.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.tooltip_id:
            self.tooltips[self.tooltip_id] = "".join(self.tooltip_text).strip()
            self.tooltip_id = None

    def counts(self):
        result = {}
        for cell_id, day in self.cells.items():
            text = self.tooltips.get(cell_id, "")
            match = re.match(r"(No|[\d,]+) contributions? on\b", text)
            if not match:
                raise ValueError(f"Contribution tooltip missing or changed for {day}")
            count = 0 if match[1] == "No" else int(match[1].replace(",", ""))
            if day in result:
                raise ValueError(f"Duplicate contribution date: {day}")
            result[day] = count
        if not result:
            raise ValueError("GitHub contribution calendar contains no dated cells")
        return result


def contribution_days(username, end):
    start = end - timedelta(days=364)
    counts = {}
    for year in range(start.year, end.year + 1):
        params = urlencode({"from": f"{year}-01-01", "to": f"{year}-12-31"})
        # Deliberately unauthenticated: show only what visitors can see, even
        # when the local REST token has access to private repositories.
        parser = ContributionParser()
        parser.feed(fetch(f"https://github.com/users/{username}/contributions?{params}"))
        counts.update(parser.counts())
    days = []
    for offset in range(365):
        day = start + timedelta(days=offset)
        if day not in counts:
            raise ValueError(f"Public contribution calendar is missing {day}")
        days.append({"date": day.isoformat(), "count": counts[day]})
    return days


def summarize(days, repositories, languages):
    longest = current = 0
    for day in days:
        current = current + 1 if day["count"] > 0 else 0
        longest = max(longest, current)
    return {
        "Contributions": sum(day["count"] for day in days),
        "Active days": sum(day["count"] > 0 for day in days),
        "Longest streak": longest,
        "Public originals": len(repositories),
        "Stars received": sum(repo["stargazers_count"] for repo in repositories),
        "Languages": sum(size > 0 for size in languages.values()),
    }


def text(x, y, value, theme, size=14, weight=400, muted=False, anchor="start"):
    color = theme["muted" if muted else "ink"]
    return (
        f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{escape(str(value))}</text>'
    )


def rect(x, y, width, height, fill, radius=0, extra=""):
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'rx="{radius}" fill="{fill}" {extra}/>'
    )


def svg(title, description, theme, height, content, width=960):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
        f'<title id="title">{escape(title)}</title>\n'
        f'<desc id="desc">{escape(description)}</desc>\n'
        '<g font-family="DejaVu Sans, Segoe UI, Arial, sans-serif">\n'
        + rect(1, 1, width - 2, height - 2, theme["surface"], 16,
               f'stroke="{theme["line"]}" stroke-width="1"')
        + content + "\n</g>\n</svg>\n"
    )


def banner(theme, compact=False):
    content = text(38, 52, "MEHROJ MAJIDOV  /  SOFTWARE ENGINEER", theme, 12, 600, True)
    content += text(36, 113, "Building the", theme, 45, 700)
    content += text(36, 166, "backend behind it.", theme, 45, 700)
    content += rect(38, 190, 48, 3, theme["accent"], 1)
    content += text(38, 228, "Python · Django · Uzbekistan", theme, 17, muted=True)
    if compact:
        return svg("Mehroj Majidov — software engineer", "Building the backend behind it. Python, Django, Uzbekistan.", theme, 270, content, 480)
    # A small technical illustration, deliberately not a performance chart.
    content += rect(664, 42, 258, 192, theme["panel"], 12)
    content += text(686, 73, "SYSTEM / BACKEND", theme, 11, 600, True)
    for index, label in enumerate(("API", "APPLICATION", "DATABASE")):
        y = 91 + index * 43
        content += rect(686, y, 214, 31, theme["surface"], 6,
                        f'stroke="{theme["line"]}"')
        content += rect(700, y + 12, 7, 7, theme["accent"], 2)
        content += text(718, y + 21, label, theme, 11, 600)
        if index < 2:
            content += f'<path d="M793 {y + 31}v12" stroke="{theme["line"]}" stroke-width="2"/>'
    return svg("Mehroj Majidov — software engineer", "Building the backend behind it. Python, Django, Uzbekistan.", theme, 270, content)


def stats_card(metrics, days, theme, compact=False):
    width, height = (480, 438) if compact else (960, 325)
    columns, step, card_width = (2, 230, 208) if compact else (3, 310, 286)
    content = text(24 if compact else 32, 37, "GITHUB, IN NUMBERS", theme, 12, 600, True)
    content += text(width - 32, 37, "PUBLICLY VISIBLE", theme, 11, 500, True, "end")
    for index, (label, value) in enumerate(metrics.items()):
        x = (24 if compact else 32) + (index % columns) * step
        y = 58 + (index // columns) * 118
        content += rect(x, y, card_width, 100, theme["panel"], 9)
        content += text(x + 19, y + 44, f"{value:,}", theme, 34, 700)
        content += text(x + 19, y + 70, label, theme, 14, 500)
        scope = "last 365 days" if index < 3 else "owned public nonfork repos"
        content += text(x + 19, y + 89, scope, theme, 10, muted=True)
    period = f'{days[0]["date"]} — {days[-1]["date"]}'
    if not compact:
        period += " · streak is consecutive contribution days within this window"
    content += text(32, height - 22, period, theme, 11, muted=True)
    description = "; ".join(f"{label}: {value:,}" for label, value in metrics.items())
    return svg("GitHub activity and public repository statistics", description, theme, height, content, width)


def contribution_thresholds(days):
    counts = sorted(day["count"] for day in days if day["count"] > 0)
    if not counts:
        return []
    return sorted({counts[min(len(counts) - 1, int(len(counts) * fraction))]
                   for fraction in (0.25, 0.5, 0.75, 1.0)})


def heat_level(count, thresholds):
    if count == 0:
        return 0
    for level, threshold in enumerate(thresholds, 1):
        if count <= threshold:
            return level
    raise ValueError("Contribution count exceeds the heatmap scale")


def activity_card(days, theme, compact=False):
    total = sum(day["count"] for day in days)
    thresholds = contribution_thresholds(days)
    width, height = (480, 486) if compact else (960, 315)
    step = 14 if compact else 16
    content = text(32, 39, "A YEAR OF BUILDING", theme, 12, 600, True)
    content += text(32, 70, f"{total:,} publicly visible contributions", theme, 20 if compact else 24, 600)
    content += text(32, 94, f'{days[0]["date"]} — {days[-1]["date"]}', theme, 12, muted=True)
    start = date.fromisoformat(days[0]["date"])
    sunday = start - timedelta(days=(start.weekday() + 1) % 7)
    last_month = None
    last_month_x = -100
    last_block = 0
    for day in days:
        day_date = date.fromisoformat(day["date"])
        offset = (day_date - sunday).days
        week = offset // 7
        block, column = divmod(week, 27) if compact else (0, week)
        x = (62 if compact else 72) + column * step
        y = 132 + block * 149 + (offset % 7) * step
        if block != last_block:
            last_month = None
            last_month_x = -100
        if day_date.month != last_month and x - last_month_x >= 43:
            content += text(x, 120 + block * 149, day_date.strftime("%b"), theme, 10, muted=True)
            last_month_x = x
        last_month, last_block = day_date.month, block
        level = heat_level(day["count"], thresholds)
        content += (
            f'<g><title>{day["date"]}: {day["count"]} contributions</title>'
            + rect(x, y, step - 3, step - 3, theme["heat"][level], 2) + '</g>'
        )
    for block in range(2 if compact else 1):
        for label, row in (("Mon", 1), ("Wed", 3), ("Fri", 5)):
            content += text(32, 142 + block * 149 + row * step, label, theme, 10, muted=True)
    caption = "Daily counts · nonzero days grouped in quantile buckets" if compact else "One square per day · intensity groups nonzero days into quantile buckets · counts below"
    content += text(32, 405 if compact else 270, caption, theme, 11, muted=True)
    legend = ["0"]
    lower = 1
    for upper in thresholds:
        legend.append(str(upper) if lower == upper else f"{lower}–{upper}")
        lower = upper + 1
    for level, label in enumerate(legend):
        x = 32 + (level % 3) * 145 if compact else 32 + level * 172
        y = 424 + (level // 3) * 25 if compact else 283
        content += rect(x, y, 13, 13, theme["heat"][level], 2)
        content += text(x + 21, y + 11, label, theme, 11, muted=True)
    return svg("Publicly visible GitHub contribution calendar", f"{total:,} contributions over 365 days. Daily counts are available in docs/github-stats.md.", theme, height, content, width)


def displayed_languages(languages):
    items = [(name, size) for name, size in languages.items() if size > 0]
    items.sort(key=lambda item: (-item[1], item[0]))
    if len(items) > 6:
        items = items[:5] + [("Other", sum(size for _, size in items[5:]))]
    return items


def languages_card(languages, theme, compact=False):
    items = displayed_languages(languages)
    total = sum(languages.values())
    canvas_width = 480 if compact else 960
    height = max(240, 161 + len(items) * 60 if compact else 141 + len(items) * 45)
    content = text(32, 39, "THE PUBLIC CODEBASE", theme, 12, 600, True)
    content += text(32, 69, "Languages, by code bytes", theme, 24, 600)
    scope = "Public originals · forks excluded · not skill ratings" if compact else "Owned public repositories · forks excluded · not a measure of proficiency"
    content += text(32, 92, scope, theme, 12, muted=True)
    for index, (name, size) in enumerate(items):
        y = 126 + index * (60 if compact else 45)
        share = size / total * 100
        content += text(32, y + 13, name, theme, 16 if compact else 14, 500)
        bar_x, bar_y, bar_width = (32, y + 25, 416) if compact else (188, y + 1, 610)
        content += rect(bar_x, bar_y, bar_width, 14, theme["panel"], 4)
        mark_width = bar_width * share / 100
        if mark_width > 0:
            # Square baseline; only the data end is rounded.
            radius = min(4, mark_width / 2)
            content += (
                f'<path d="M{bar_x} {bar_y}h{mark_width - radius:.3f}q{radius} 0 {radius} {radius}'
                f'v{14 - 2 * radius}q0 {radius} -{radius} {radius}H{bar_x}Z" fill="{theme["accent"]}"/>'
            )
        content += text(canvas_width - (32 if compact else 37), y + 13, f"{share:.1f}%", theme, 16 if compact else 14, 500, anchor="end")
    if not items:
        content += text(32, 142, "No public language data available.", theme, 15, muted=True)
    footer = f"{total:,} bytes · full breakdown in the linked data table" if compact else f"{total:,} bytes indexed by GitHub Linguist · complete breakdown in the linked data table"
    content += text(32, height - 21, footer, theme, 11, muted=True)
    return svg("Language composition of owned public nonfork repositories", "; ".join(f"{name}: {size:,} bytes" for name, size in items) or "No language data", theme, height, content, canvas_width)


def markdown_cell(value):
    return escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def data_table(username, days, metrics, repositories, languages):
    total_bytes = sum(languages.values())
    lines = [
        "# GitHub profile data", "",
        f"Account: [{username}](https://github.com/{username}). Updated: **{days[-1]['date']} (UTC)**.", "",
        f"Activity window: **{days[0]['date']} through {days[-1]['date']}**, inclusive (365 days).", "",
        "## Headline metrics", "", "| Metric | Value | Scope |", "| :--- | ---: | :--- |",
    ]
    for index, (label, value) in enumerate(metrics.items()):
        scope = "Displayed 365-day activity window" if index < 3 else "Current owned public nonfork repositories"
        lines.append(f"| {label} | {value:,} | {scope} |")
    lines += [
        "", "Contributions are taken from the **unauthenticated, publicly visible GitHub calendar**. "
        "They can include anonymized private activity only when the account has chosen to show it publicly. "
        "They are not a count of commits or hours worked. Active days have at least one contribution; "
        "the longest streak counts consecutive active days within this window, not a lifetime record.",
        "", "Repository, star, and language totals are current snapshots. Forks and private repositories are excluded; "
        "archived original repositories are included. Stars are GitHub star totals and can include self-stars.",
        "", "## Language composition", "", "Byte counts come from GitHub's public repository language API "
        "(default branch, as classified by GitHub Linguist). They do not measure proficiency, time spent, "
        "or lines of code. The graphic groups the tail into Other; every language is listed here.",
        "", "| Language | Bytes | Share |", "| :--- | ---: | ---: |",
    ]
    for name, size in languages.items():
        share = 100 * size / total_bytes if total_bytes else 0
        lines.append(f"| {markdown_cell(name)} | {size:,} | {share:.2f}% |")
    lines += ["", "## Public repositories included", "", "| Repository | Stars |", "| :--- | ---: |"]
    for repo in sorted(repositories, key=lambda repo: repo["name"].lower()):
        name = markdown_cell(repo["name"])
        lines.append(f"| [{name}](https://github.com/{repo['full_name']}) | {repo['stargazers_count']:,} |")
    lines += ["", "## Daily contributions", "", "Accessible text equivalent of the heatmap. "
              "Dates are ISO 8601; counts are publicly visible contributions, including zero days.",
              "", "| Date | Contributions |", "| :--- | ---: |"]
    lines.extend(f"| {day['date']} | {day['count']:,} |" for day in days)
    lines += ["", "## Sources and refresh", "",
              f"- [Public contribution calendar](https://github.com/{username}?tab=overview)",
              f"- [Public repository API](https://api.github.com/users/{username}/repos?per_page=100&type=owner)",
              "- Language API: `https://api.github.com/repos/OWNER/REPOSITORY/languages`",
              "- [Refresh instructions](maintenance.md)", ""]
    return "\n".join(lines)


def generate(username, end, root=ROOT, token=None):
    repositories = public_repositories(username, token)
    languages = language_totals(repositories, token)
    days = contribution_days(username, end)
    metrics = summarize(days, repositories, languages)
    outputs = {}
    for mode, theme in THEMES.items():
        for compact in (False, True):
            for name, graphic in {
                "banner": banner(theme, compact), "stats": stats_card(metrics, days, theme, compact),
                "activity": activity_card(days, theme, compact), "languages": languages_card(languages, theme, compact),
            }.items():
                ET.fromstring(graphic)
                suffix = f"{mode}-mobile" if compact else mode
                outputs[root / "images" / "generated" / f"{name}-{suffix}.svg"] = graphic
    outputs[root / "docs" / "github-stats.md"] = data_table(username, days, metrics, repositories, languages)
    # Fetch and validate everything before touching the last successful snapshot.
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    print(f"Generated {len(outputs)} files from publicly visible data for {username}.")
    print(json.dumps(metrics, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="mehroj-r")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(timezone.utc).date(),
                        help="Last contribution date (YYYY-MM-DD, default today UTC)")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.username):
        parser.error("Invalid GitHub username")
    if args.date > datetime.now(timezone.utc).date():
        parser.error("Reporting date must not be in the future")
    try:
        generate(args.username, args.date, token=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"Profile refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
