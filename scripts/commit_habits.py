#!/usr/bin/env python3
"""Write commit habits and where commits went into README.md, without dependencies.

Commits are cached in data/commit-habits.json so that repositories you lose access to
keep counting. The repository is public, so the cache holds, per commit, a hash of its id,
the UTC hour it was authored and a hash of its repository, plus the owner of each such
repository: no repository names, messages or commit ids.

Repositories that were never cached before access was lost can be imported once from a
local clone with --import-git; their commits then stay in the cache like any other.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
DAY_TIMES = ["🌞 Morning", "🌆 Daytime", "🌃 Evening", "🌙 Night"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
RETRYABLE = (403, 429, 500, 502, 503, 504)
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
HOUR = re.compile(r"\d{4}-\d\d-\d\dT\d\d:00Z")
OTHER_BELOW = 1.0  # organizations with a smaller share of commits (in percent) join "Other"
BAR = 12
LOGOS = Path("images", "owners")  # next to README.md; hand-picked logos can be dropped in here
LOGO_TYPES = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg"}
MAX_LOGO = 512 * 1024

PAGE = "pageInfo { hasNextPage endCursor }"
OWNER = "owner { __typename login ... on Organization { name } ... on User { name } }"
USER_ID = "query($login: String!) { user(login: $login) { id } }"
ACCESSIBLE_REPOSITORIES = f"""
query($login: String!, $cursor: String) {{
  user(login: $login) {{
    repositories(first: 100, after: $cursor, isFork: false,
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {{
      {PAGE} nodes {{ {OWNER} name defaultBranchRef {{ name }} }}
    }}
  }}
}}"""
CONTRIBUTED_REPOSITORIES = f"""
query($login: String!, $cursor: String) {{
  user(login: $login) {{
    repositoriesContributedTo(first: 100, after: $cursor, includeUserRepositories: false,
                              contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {{
      {PAGE} nodes {{ {OWNER} name isFork defaultBranchRef {{ name }} }}
    }}
  }}
}}"""
HISTORY = f"""
query($owner: String!, $name: String!, $ref: String!, $author: ID!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    ref(qualifiedName: $ref) {{
      target {{
        ... on Commit {{ history(first: 100, after: $cursor, author: {{ id: $author }}) {{ {PAGE} nodes {{ oid authoredDate }} }} }}
      }}
    }}
  }}
}}"""


class InaccessibleRepository(Exception):
    pass


def graphql(query, token, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"User-Agent": "mehroj-r-profile", "Authorization": f"Bearer {token}"}
    for attempt in range(5):
        try:
            with urlopen(Request("https://api.github.com/graphql", body, headers), timeout=60) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in RETRYABLE or attempt == 4:
                raise RuntimeError(f"GitHub GraphQL request failed ({status})") from None
        except URLError:
            if attempt == 4:
                raise RuntimeError("GitHub GraphQL request failed (network)") from None
        time.sleep(2 ** attempt * 5)
    if payload.get("errors"):
        # Messages can name private repositories and Actions logs are public; report types only.
        types = sorted({error.get("type", "UNKNOWN") for error in payload["errors"]})
        if payload.get("data") and set(types) <= {"NOT_FOUND", "FORBIDDEN"}:
            raise InaccessibleRepository()
        raise RuntimeError(f"GitHub GraphQL returned errors: {', '.join(types)}")
    return payload["data"]


def paginate(query, token, path, **variables):
    cursor = None
    while True:
        connection = graphql(query, token, cursor=cursor, **variables)
        for key in path:
            connection = connection[key] if connection else None
        if connection is None:
            return
        yield connection["nodes"]
        if not connection["pageInfo"]["hasNextPage"]:
            return
        cursor = connection["pageInfo"]["endCursor"]


def owner_profile(login, token=None):
    # Public profile of an owner whose repositories are no longer listed, e.g. imported ones.
    headers = {"User-Agent": "mehroj-r-profile", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(f"https://api.github.com/users/{quote(login)}", headers=headers), timeout=30) as response:
            profile = json.load(response)
    except (HTTPError, URLError):
        return None
    return {"type": profile.get("type"), "name": profile.get("name")}


def repositories(username, token):
    author = graphql(USER_ID, token, login=username)["user"]["id"]
    found, owners = {}, {}
    for query, field in ((ACCESSIBLE_REPOSITORIES, "repositories"),
                         (CONTRIBUTED_REPOSITORIES, "repositoriesContributedTo")):
        for nodes in paginate(query, token, ["user", field], login=username):
            for node in nodes:
                if node.get("isFork") or not node["defaultBranchRef"]:
                    continue
                owner = node["owner"]
                owners[owner["login"]] = {"type": owner["__typename"], "name": owner.get("name")}
                found.setdefault((owner["login"], node["name"]), node["defaultBranchRef"]["name"])
    # The profile repository's one commit is replaced on every refresh (publish_single_commit.sh),
    # so reading it would count a new commit each night. Its earlier commits stay cached.
    found = {key: branch for key, branch in found.items() if key[0].lower() != username.lower()
             or key[1].lower() != username.lower()}
    return author, found, owners


def commit_key(oid):
    # The cache is public: store a one-way hash, never the commit id of a private repository.
    return sha256(oid.encode()).hexdigest()[:16]


def repository_key(owner, name):
    # Lets repositories be counted per owner without publishing private repository names.
    return sha256(f"{owner}/{name}".lower().encode()).hexdigest()[:12]


def authored_hour(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def remember(cache, oid, authored, repository):
    commits, key = cache["commits"], commit_key(oid)
    if key not in commits:
        commits[key] = [authored_hour(authored), repository]
        return True
    if commits[key][1] is None:
        commits[key][1] = repository  # cached before repositories were tracked
    return False


def authored_dates(owner, name, default_branch, author, token, cache, full=False):
    # Default branches only: feature branches repeat their history, and unmerged work is left out.
    # Keyed by commit, so a commit shared by two repositories is still counted once.
    repository = repository_key(owner, name)
    cache["repositories"][repository] = owner
    pages = paginate(HISTORY, token, ["repository", "ref", "target", "history"],
                     owner=owner, name=name, ref=f"refs/heads/{default_branch}", author=author)
    for nodes in pages:
        fresh = [remember(cache, node["oid"], node["authoredDate"], repository) for node in nodes]
        # History is newest first: once a whole page is cached, so is everything older.
        if not any(fresh) and not full:
            break


def valid_cache(cache):
    repositories, owners, commits = cache["repositories"], cache["owners"], cache["commits"]
    return (
        all(re.fullmatch(r"[0-9a-f]{12}", key) and isinstance(owner, str) and LOGIN.fullmatch(owner)
            for key, owner in repositories.items())
        and all(LOGIN.fullmatch(login) and isinstance(info, dict) for login, info in owners.items())
        and all(re.fullmatch(r"[0-9a-f]{16}", key) and isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and HOUR.fullmatch(value[0])
                and (value[1] is None or value[1] in repositories)
                for key, value in commits.items())
    )


def load_cache(path):
    if not path.exists():
        return {"owners": {}, "repositories": {}, "commits": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version") if isinstance(data, dict) else None
    if version == 1 and isinstance(data.get("commits"), dict):
        # Version 1 did not track repositories; a --full run attributes these commits.
        data = {"owners": {}, "repositories": {},
                "commits": {key: [hour, None] for key, hour in data["commits"].items()}}
    elif version != 2 or not all(isinstance(data.get(field), dict) for field in ("owners", "repositories", "commits")):
        data = None
    # A broken cache must stop the run: starting over would drop commits from lost repositories.
    if data is None or not valid_cache(data):
        raise ValueError(f"{path} is not a valid commit cache")
    return {field: data[field] for field in ("owners", "repositories", "commits")}


def prune(cache):
    # Only owners and repositories that commits point to are published.
    used = {repository for _, repository in cache["commits"].values() if repository}
    cache["repositories"] = {key: owner for key, owner in cache["repositories"].items() if key in used}
    owners = set(cache["repositories"].values())
    cache["owners"] = {login: info for login, info in cache["owners"].items() if login in owners}


def cache_text(cache):
    meta = {"version": 2, "owners": dict(sorted(cache["owners"].items())),
            "repositories": dict(sorted(cache["repositories"].items()))}
    commits = sorted(cache["commits"].items(), key=lambda item: (item[1][0], item[0]))
    # One commit per line, oldest first, so nightly diffs are just the lines appended.
    body = ",\n".join(f"  {json.dumps(key)}: {json.dumps(value)}" for key, value in commits)
    return json.dumps(meta, indent=1, ensure_ascii=False)[:-2] + ',\n "commits": {\n' + body + "\n }\n}\n"


def fill_owners(cache, known, token=None):
    cache["owners"].update(known)
    for login in set(cache["repositories"].values()) - set(cache["owners"]):
        profile = owner_profile(login, token)
        if profile:
            cache["owners"][login] = profile


def habits(dates, zone):
    day_times, weekdays = [0] * 4, [0] * 7
    for value in dates:
        moment = datetime.fromisoformat(value).astimezone(zone)
        day_times[(moment.hour // 6 + 3) % 4] += 1  # 06-12 morning, ..., 00-06 night
        weekdays[moment.weekday()] += 1
    return day_times, weekdays


def rows(names, counts):
    total = sum(counts)
    lines = []
    for name, count in zip(names, counts):
        percent = round(count / total * 100, 2) if total else 0
        filled = round(percent / 4)
        bar = "█" * filled + "░" * (25 - filled)
        lines.append(f"{name:<25}{f'{count} commits':<20}{bar}   {percent:05.2f} %")
    return "\n".join(lines)


def section(day_times, weekdays):
    if not sum(day_times):
        raise ValueError("No commits found; refusing to publish empty habits")
    title = "I'm an Early 🐤" if sum(day_times[:2]) >= sum(day_times[2:]) else "I'm a Night 🦉"
    busiest = WEEKDAYS[weekdays.index(max(weekdays))]
    return (f"**{title}**\n\n```text\n{rows(DAY_TIMES, day_times)}\n```\n\n"
            f"📅 **I'm Most Productive on {busiest}**\n\n```text\n{rows(WEEKDAYS, weekdays)}\n```")


def owner_totals(cache, username):
    """Commits per owner, with small organizations, other people's repositories and
    unattributed commits merged into a final "Other" entry (owner None)."""
    totals = {}
    for _, repository in cache["commits"].values():
        owner = cache["repositories"].get(repository)
        entry = totals.setdefault(owner, {"commits": 0, "repositories": set()})
        entry["commits"] += 1
        entry["repositories"].add(repository)
    total = sum(entry["commits"] for entry in totals.values())
    shown, other = [], None
    for owner, entry in totals.items():
        organization = cache["owners"].get(owner, {}).get("type") == "Organization"
        if owner == username or (organization and entry["commits"] / total * 100 >= OTHER_BELOW):
            shown.append((owner, entry))
        elif other is None:
            other = dict(entry, repositories=set(entry["repositories"]))
        else:
            other["commits"] += entry["commits"]
            other["repositories"] |= entry["repositories"]
    shown.sort(key=lambda item: (-item[1]["commits"], item[0]))
    return shown + ([(None, other)] if other else []), total


def logo_path(directory, login):
    return next((path for path in (directory / f"{login}{suffix}" for suffix in LOGO_TYPES.values())
                 if path.exists()), None)


def download_avatar(login):
    request = Request(f"https://github.com/{quote(login)}.png?size=80", headers={"User-Agent": "mehroj-r-profile"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(MAX_LOGO + 1)
    except (HTTPError, URLError):
        return None


def save_logos(logins, directory):
    # A saved logo survives an organization changing its avatar or disappearing. Files that
    # already exist, including hand-picked logos, are never replaced.
    for login in logins:
        if logo_path(directory, login):
            continue
        data = download_avatar(login) or b""
        suffix = next((suffix for magic, suffix in LOGO_TYPES.items() if data.startswith(magic)), None)
        if suffix and len(data) <= MAX_LOGO:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{login}{suffix}.tmp"
            temporary.write_bytes(data)
            temporary.replace(directory / f"{login}{suffix}")


def load_names(path):
    """Display names that replace an owner's GitHub name in the table, e.g. a shorter brand name."""
    if not path.exists():
        return {}
    names = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(names, dict) or not all(
        LOGIN.fullmatch(login) and isinstance(name, str) and 0 < len(name.strip()) <= 60
        for login, name in names.items()
    ):
        raise ValueError(f"{path} must map owner logins to display names")
    return names


def owners_section(cache, username, logos=None, names=None):
    lines = ["| | Where | Commits | Share | Repos |", "| :-: | --- | ---: | --- | ---: |"]
    entries, total = owner_totals(cache, username)
    for owner, entry in entries:
        if owner is None:
            avatar, label = "👥", "Other"
        else:
            name = ("Personal projects" if owner == username else (names or {}).get(owner)
                    or cache["owners"].get(owner, {}).get("name") or owner)
            # Display names come from GitHub: keep them from breaking the table or the link.
            name = re.sub(r"([\\|\[\]<>*_`])", r"\\\1", name)
            path = logo_path(logos, owner) if logos else None
            source = (LOGOS / path.name).as_posix() if path else f"https://github.com/{owner}.png?size=40"
            avatar = f'<img src="{source}" width="20" height="20" alt="">'
            label = f"**[{name}](https://github.com/{owner})**"
        percent = entry["commits"] / total * 100
        filled = round(percent / 100 * BAR)
        share = f"`{'█' * filled}{'░' * (BAR - filled)}` {f'{percent:.0f}%' if percent >= 1 else '<1%'}"
        repos = len(entry["repositories"] - {None}) or "–"
        lines.append(f"| {avatar} | {label} | {entry['commits']:,} | {share} | {repos} |")
    return "\n".join(lines)


def section_pattern(name):
    # The section may be empty, as when its markers were just added.
    return re.compile(rf"(<!--START_SECTION:{name}-->\n)(?:.*?\n)?(<!--END_SECTION:{name}-->)", re.S)


def replace_section(readme, name, content):
    pattern = section_pattern(name)
    if not pattern.search(readme):
        raise ValueError(f"README.md is missing the {name} section markers")
    return pattern.sub(lambda match: match[1] + content + "\n" + match[2], readme, count=1)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def tally(cache):
    """Commit counts that must never go down: in total, per repository and per owner."""
    counts = Counter({"total": len(cache["commits"])})
    for _, repository in cache["commits"].values():
        if repository:
            counts["repository", repository] += 1
            counts["owner", cache["repositories"][repository]] += 1
    return counts


def check_never_lower(before, after):
    # Commits are only ever added, so a lower count than the loaded cache means a bug, never real data.
    dropped = Counter(key if key == "total" else key[0] for key, count in before.items() if after[key] < count)
    if dropped:
        # Counts by kind only: owner logins and repository hashes stay out of the public logs.
        details = ", ".join(f"{kind} ×{number}" for kind, number in sorted(dropped.items()))
        raise ValueError(f"Refusing to publish: commit counts would go down ({details})")


def publish(cache, zone, username, readme, path, before):
    prune(cache)
    check_never_lower(before, tally(cache))
    text = readme.read_text(encoding="utf-8")
    text = replace_section(text, "commit-habits", section(*habits((hour for hour, _ in cache["commits"].values()), zone)))
    if section_pattern("commit-owners").search(text):
        logos = readme.parent / LOGOS
        save_logos([owner for owner, _ in owner_totals(cache, username)[0] if owner], logos)
        names = load_names(path.parent / "owner-names.json")
        text = replace_section(text, "commit-owners", owners_section(cache, username, logos, names))
    write(path, cache_text(cache))
    write(readme, text)


def origin_repository(clone):
    url = subprocess.run(["git", "-C", str(clone), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=False).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not match or not LOGIN.fullmatch(match[1]):
        raise ValueError(f"Cannot tell which GitHub repository {clone} is; pass --repository OWNER/NAME")
    return match[1], match[2]


def import_git(clone, ref, emails, cache, repository=None):
    owner, name = repository or origin_repository(clone)
    key = repository_key(owner, name)
    cache["repositories"][key] = owner
    log = subprocess.run(
        ["git", "-C", str(clone), "log", "--format=%H%x09%aI%x09%ae", "--end-of-options", ref, "--"],
        capture_output=True, text=True, check=False,
    )
    if log.returncode:
        raise RuntimeError(f"git log failed in {clone} for {ref}: {log.stderr.strip()}")
    wanted = {email.lower() for email in emails}
    found = new = 0
    for line in log.stdout.splitlines():
        oid, authored, email = line.split("\t")
        if email.lower() in wanted:
            found += 1
            new += remember(cache, oid, authored, key)
    return found, new


def import_clones(clones, ref, emails, zone, username, readme, path, repository=None, token=None):
    cache = load_cache(path)
    before = tally(cache)
    # Every clone is read before the cache or README is touched.
    results = [import_git(clone, ref, emails, cache, repository) for clone in clones]
    fill_owners(cache, {}, token)
    publish(cache, zone, username, readme, path, before)
    for clone, (found, new) in zip(clones, results):
        print(f"{clone.name}: {found} of your commits on {ref}, {new} new.")
    print(f"Counted {len(cache['commits'])} commits.")


def generate(username, zone, readme, path, token, full=False):
    cache = load_cache(path)
    before, cached = tally(cache), len(cache["commits"])
    author, found, owners = repositories(username, token)
    skipped = 0
    for (owner, name), default_branch in sorted(found.items()):
        try:
            authored_dates(owner, name, default_branch, author, token, cache, full)
        except InaccessibleRepository:
            skipped += 1
    fill_owners(cache, owners, token)
    # Everything is fetched before the cache or README is touched.
    publish(cache, zone, username, readme, path, before)
    unattributed = sum(1 for _, repository in cache["commits"].values() if repository is None)
    print(f"Counted {len(cache['commits'])} commits ({len(cache['commits']) - cached} new, "
          f"{unattributed} without a repository) after checking {len(found) - skipped} "
          f"repositories ({skipped} unreadable).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="mehroj-r")
    parser.add_argument("--timezone", default="Asia/Tashkent")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "commit-habits.json")
    parser.add_argument("--full", action="store_true",
                        help="Read every default branch to the end instead of stopping at cached history")
    parser.add_argument("--import-git", type=Path, action="append", metavar="CLONE",
                        help="Count your commits from a local clone instead of GitHub (repeatable)")
    parser.add_argument("--ref", default="HEAD", help="Branch or ref to read in --import-git clones")
    parser.add_argument("--email", action="append", default=[],
                        help="An email your commits use, for --import-git (repeatable)")
    parser.add_argument("--repository", metavar="OWNER/NAME",
                        help="GitHub repository of a single --import-git clone, if origin does not say")
    args = parser.parse_args()
    if not LOGIN.fullmatch(args.username):
        parser.error("Invalid GitHub username")
    try:
        zone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        parser.error(f"Unknown time zone: {args.timezone}")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if args.import_git:
        if not args.email:
            parser.error("--import-git needs at least one --email to recognise your commits")
        repository = None
        if args.repository:
            owner, _, name = args.repository.partition("/")
            if len(args.import_git) > 1 or not LOGIN.fullmatch(owner) or not name:
                parser.error("--repository takes OWNER/NAME and a single --import-git clone")
            repository = (owner, name)
        try:
            import_clones(args.import_git, args.ref, args.email, zone, args.username, args.readme,
                          args.cache, repository, token)
        except (RuntimeError, ValueError, OSError) as error:
            print(f"Commit habits import failed: {error}", file=sys.stderr)
            return 1
        return 0
    if not token:
        parser.error("Set GITHUB_TOKEN or GH_TOKEN; the GraphQL API requires a token")
    try:
        generate(args.username, zone, args.readme, args.cache, token, args.full)
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"Commit habits refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
