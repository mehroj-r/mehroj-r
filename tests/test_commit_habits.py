import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

SPEC = importlib.util.spec_from_file_location(
    "commit_habits", Path(__file__).resolve().parents[1] / "scripts" / "commit_habits.py"
)
habits = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(habits)

TASHKENT = ZoneInfo("Asia/Tashkent")
README = ("intro\n<!--START_SECTION:commit-habits-->\nold\n<!--END_SECTION:commit-habits-->\n"
          "<!--START_SECTION:commit-owners-->\nold\n<!--END_SECTION:commit-owners-->\nfooter\n")


def page(nodes, next_cursor=None):
    return {"pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}, "nodes": nodes}


def commit(oid, date="2026-10-19T10:00:00Z"):
    return {"oid": oid, "authoredDate": date}


def keys(*oids):
    return sorted(habits.commit_key(oid) for oid in oids)


def new_cache(**commits):
    """A cache whose commits (oid=repository key or None) were authored on 2026-10-01."""
    cache = {"owners": {}, "repositories": {}, "commits": {}}
    for oid, repository in commits.items():
        if repository:
            cache["repositories"][repository] = "mehroj-r"
        cache["commits"][habits.commit_key(oid)] = ["2026-10-01T10:00Z", repository]
    return cache


def git(repository, *args, **env):
    # Isolated from the machine's git config (signing, hooks, default branch).
    base = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}
    return subprocess.run(["git", "-C", str(repository), *args], env=base | env,
                          capture_output=True, text=True, check=True).stdout.strip()


def git_commit(repository, email, date):
    if not (repository / ".git").exists():
        git(repository, "init", "-q", "-b", "main")
    identity = {"GIT_AUTHOR_NAME": "Someone", "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": "Someone", "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}
    git(repository, "commit", "-q", "--allow-empty", "-m", email, **identity)
    return git(repository, "rev-parse", "HEAD")


class FakeGitHub:
    """Answers history queries from in-memory pages per branch and records what was asked."""

    def __init__(self, history):
        self.history, self.requests = history, []

    def __call__(self, query, token, **variables):
        branch = variables["ref"].removeprefix("refs/heads/")
        index = int(variables["cursor"] or 0)
        self.requests.append((variables["name"], branch, index))
        pages = self.history[branch]
        cursor = str(index + 1) if index + 1 < len(pages) else None
        return {"repository": {"ref": {"target": {"history": page(pages[index], cursor)}}}}


def profile_api(repository_names, history):
    """Fake GraphQL API: the listed repositories of mehroj-r, all on `main`; "secret" is unreadable."""
    history_api = FakeGitHub(history)
    owner = {"__typename": "User", "login": "mehroj-r", "name": "Mehroj"}

    def fake(query, token, **variables):
        if query is habits.USER_ID:
            return {"user": {"id": "author-id"}}
        if query is habits.ACCESSIBLE_REPOSITORIES:
            return {"user": {"repositories": page([
                {"owner": owner, "name": name, "defaultBranchRef": {"name": "main"}} for name in repository_names
            ])}}
        if query is habits.CONTRIBUTED_REPOSITORIES:
            return {"user": {"repositoriesContributedTo": page([])}}
        if variables["name"] == "secret":
            raise habits.InaccessibleRepository()
        return history_api(query, token, **variables)

    return fake


class HabitTests(unittest.TestCase):
    def test_commits_are_bucketed_in_local_time(self):
        day_times, weekdays = habits.habits([
            "2026-10-19T00:00Z",  # 05:00 Monday in Tashkent: night
            "2026-10-19T02:00Z",  # 07:00 Monday: morning
            "2026-10-19T10:00Z",  # 15:00 Monday: daytime
            "2026-10-19T19:00Z",  # 00:00 Tuesday: night
            "2026-10-19T13:00Z",  # 18:00 Monday: evening
        ], TASHKENT)
        self.assertEqual(day_times, [1, 1, 1, 2])
        self.assertEqual(weekdays, [4, 1, 0, 0, 0, 0, 0])

    def test_authored_time_is_kept_to_the_utc_hour(self):
        self.assertEqual(habits.authored_hour("2026-10-19T15:47:12+05:00"), "2026-10-19T10:00Z")

    def test_rows_keep_fixed_columns(self):
        lines = habits.rows(["🌞 Morning", "🌙 Night"], [3, 1]).splitlines()
        self.assertEqual(lines[0], f"{'🌞 Morning':<25}{'3 commits':<20}{'█' * 19}{'░' * 6}   75.00 %")
        self.assertTrue(lines[1].endswith(f"{'█' * 6}{'░' * 19}   25.00 %"))

    def test_section_titles_follow_the_majority(self):
        self.assertIn("I'm an Early 🐤", habits.section([2, 1, 1, 2], [0, 0, 5, 1, 0, 0, 0]))
        self.assertIn("I'm a Night 🦉", habits.section([1, 1, 1, 2], [0, 0, 5, 1, 0, 0, 0]))
        self.assertIn("Most Productive on Wednesday", habits.section([1, 0, 0, 0], [0, 0, 5, 1, 0, 0, 0]))

    def test_empty_history_is_not_published(self):
        with self.assertRaises(ValueError):
            habits.section([0, 0, 0, 0], [0] * 7)

    def test_only_the_marked_section_is_replaced(self):
        updated = habits.replace_section(README, "commit-habits", "new")
        self.assertEqual(updated, README.replace("\nold\n", "\nnew\n", 1))
        with self.assertRaises(ValueError):
            habits.replace_section("no markers here", "commit-habits", "new")

    def test_an_empty_section_is_filled(self):
        empty = "a\n<!--START_SECTION:commit-owners-->\n<!--END_SECTION:commit-owners-->\nb\n"
        self.assertEqual(habits.replace_section(empty, "commit-owners", "table"),
                         "a\n<!--START_SECTION:commit-owners-->\ntable\n<!--END_SECTION:commit-owners-->\nb\n")

    def test_the_profile_repository_itself_is_not_read(self):
        api = profile_api(["mehroj-r", "open"], {})
        with patch.object(habits, "graphql", api):
            _, found, _ = habits.repositories("mehroj-r", "token")
        self.assertEqual(list(found), [("mehroj-r", "open")])

    def test_repositories_reached_through_organizations_are_listed(self):
        self.assertIn("affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]", habits.ACCESSIBLE_REPOSITORIES)


class CacheTests(unittest.TestCase):
    def setUp(self):
        # Publishing saves owner logos; tests must never download real avatars.
        patcher = patch.object(habits, "download_avatar", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_default_branches_are_read_and_shared_commits_count_once(self):
        fake = FakeGitHub({
            "main": [[commit("c3"), commit("c2")], [commit("c1")]],
            "trunk": [[commit("c1"), commit("t1")]],
        })
        cache = new_cache()
        with patch.object(habits, "graphql", fake):
            habits.authored_dates("mehroj-r", "one", "main", "author-id", "token", cache)
            habits.authored_dates("mehroj-r", "copy", "trunk", "author-id", "token", cache)
        self.assertEqual(sorted(cache["commits"]), keys("c1", "c2", "c3", "t1"))
        self.assertEqual(cache["commits"][habits.commit_key("c1")][1], habits.repository_key("mehroj-r", "one"))
        self.assertEqual(fake.requests, [("one", "main", 0), ("one", "main", 1), ("copy", "trunk", 0)])

    def test_walk_stops_at_the_first_fully_cached_page(self):
        fake = FakeGitHub({"main": [[commit("c4"), commit("c3")], [commit("c2"), commit("c1")], [commit("c0")]]})
        repository = habits.repository_key("mehroj-r", "repo")
        cache = new_cache(c3=repository, c2=repository, c1=repository)
        with patch.object(habits, "graphql", fake):
            habits.authored_dates("mehroj-r", "repo", "main", "author-id", "token", cache)
        self.assertEqual(sorted(cache["commits"]), keys("c1", "c2", "c3", "c4"))
        self.assertEqual(fake.requests, [("repo", "main", 0), ("repo", "main", 1)])

    def test_full_scan_reads_to_the_end_and_attributes_older_commits(self):
        # The middle page adds nothing new, which would stop an incremental run before c0.
        fake = FakeGitHub({"main": [[commit("c3"), commit("c2")], [commit("c1")], [commit("c0")]]})
        cache = new_cache(c2=None, c1=None, c0=None)
        with patch.object(habits, "graphql", fake):
            habits.authored_dates("Some-Org", "repo", "main", "author-id", "token", cache, full=True)
        repository = habits.repository_key("Some-Org", "repo")
        self.assertEqual({value[1] for value in cache["commits"].values()}, {repository})
        self.assertEqual(cache["repositories"], {repository: "Some-Org"})
        self.assertEqual(fake.requests, [("repo", "main", 0), ("repo", "main", 1), ("repo", "main", 2)])

    def test_commits_from_lost_repositories_keep_counting(self):
        lost_repository = habits.repository_key("Lost-Org", "gone")
        lost = habits.commit_key("commit-in-a-repository-we-lost")
        with tempfile.TemporaryDirectory() as directory:
            readme, path = Path(directory) / "README.md", Path(directory) / "data" / "commit-habits.json"
            readme.write_text(README, encoding="utf-8")
            path.parent.mkdir()
            path.write_text(json.dumps({
                "version": 2, "owners": {"Lost-Org": {"type": "Organization", "name": "Lost Org"}},
                "repositories": {lost_repository: "Lost-Org"},
                "commits": {lost: ["2026-10-19T20:00Z", lost_repository]},
            }), encoding="utf-8")
            api = profile_api(["open", "secret"], {"main": [[commit("a" * 40, "2026-10-19T15:00:00Z")]]})
            with patch.object(habits, "graphql", api), patch.object(habits, "owner_profile", return_value=None), \
                    patch("builtins.print") as printed:
                habits.generate("mehroj-r", TASHKENT, readme, path, "token")
            stored = path.read_text(encoding="utf-8")
            # The unreadable repository has no commits, so neither its key nor anything about it is kept.
            self.assertEqual(json.loads(stored)["repositories"], {
                lost_repository: "Lost-Org", habits.repository_key("mehroj-r", "open"): "mehroj-r",
            })
            self.assertEqual(json.loads(stored)["commits"], {
                lost: ["2026-10-19T20:00Z", lost_repository],
                habits.commit_key("a" * 40): ["2026-10-19T15:00Z", habits.repository_key("mehroj-r", "open")],
            })
            text = readme.read_text(encoding="utf-8")
            self.assertIn("I'm a Night 🦉", text)
            self.assertIn("**[Lost Org](https://github.com/Lost-Org)**", text)
        for private in ("a" * 40, "secret", '"open"', "gone"):
            self.assertNotIn(private, stored)
        message = printed.call_args.args[0]
        self.assertEqual(message, "Counted 2 commits (1 new, 0 without a repository) after checking "
                                  "1 repositories (1 unreadable).")
        self.assertNotIn("secret", message)

    def test_a_broken_cache_stops_the_run_before_anything_changes(self):
        unknown_repository = json.dumps({"version": 2, "owners": {}, "repositories": {},
                                         "commits": {"0" * 16: ["2026-10-19T20:00Z", "a" * 12]}})
        with tempfile.TemporaryDirectory() as directory:
            readme, path = Path(directory) / "README.md", Path(directory) / "commit-habits.json"
            readme.write_text(README, encoding="utf-8")
            for broken in ("{not json", '{"version": 3, "owners": {}, "repositories": {}, "commits": {}}',
                           '{"version": 1, "commits": {"raw-oid": "x"}}', unknown_repository):
                path.write_text(broken, encoding="utf-8")
                with patch.object(habits, "graphql", side_effect=AssertionError("no requests")), \
                        self.assertRaises(ValueError):
                    habits.generate("mehroj-r", TASHKENT, readme, path, "token")
                self.assertEqual(path.read_text(encoding="utf-8"), broken)
            self.assertEqual(readme.read_text(encoding="utf-8"), README)

    def test_version_1_cache_is_upgraded_without_losing_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commit-habits.json"
            path.write_text(json.dumps({"version": 1, "commits": {"0" * 16: "2026-10-19T20:00Z"}}), encoding="utf-8")
            cache = habits.load_cache(path)
            self.assertEqual(cache["commits"], {"0" * 16: ["2026-10-19T20:00Z", None]})
            path.write_text(habits.cache_text(cache), encoding="utf-8")
            self.assertEqual(habits.load_cache(path), cache)

    def test_cache_is_written_oldest_first_one_commit_per_line(self):
        cache = new_cache()
        cache["commits"] = {"b" * 16: ["2026-10-20T01:00Z", None], "a" * 16: ["2026-10-21T01:00Z", None],
                            "c" * 16: ["2026-10-19T01:00Z", None]}
        text = habits.cache_text(cache)
        self.assertEqual(list(json.loads(text)["commits"]), ["c" * 16, "b" * 16, "a" * 16])
        self.assertIn('  "b' + "b" * 15 + '": ["2026-10-20T01:00Z", null],\n', text)

    def publish_after(self, change):
        """Publish a two-org cache after `change` edits it; returns the error, files untouched on failure."""
        cache = new_cache(c1="a" * 12, c2="a" * 12, c3=None)
        cache["repositories"] |= {"a" * 12: "Org-A", "b" * 12: "Org-B"}
        cache["owners"] = {"Org-A": {"type": "Organization", "name": "A"}, "Org-B": {"type": "Organization", "name": "B"}}
        with tempfile.TemporaryDirectory() as directory:
            readme, path = Path(directory) / "README.md", Path(directory) / "commit-habits.json"
            readme.write_text(README, encoding="utf-8")
            path.write_text(habits.cache_text(cache), encoding="utf-8")
            cache = habits.load_cache(path)
            before = habits.tally(cache)
            change(cache)
            try:
                habits.publish(cache, TASHKENT, "mehroj-r", readme, path, before)
            except ValueError as error:
                self.assertEqual(readme.read_text(encoding="utf-8"), README)
                self.assertEqual(habits.load_cache(path)["commits"], new_cache(c1="a" * 12, c2="a" * 12, c3=None)["commits"])
                return error
        return None

    def test_publishing_refuses_when_a_count_would_go_down(self):
        lost = self.publish_after(lambda cache: cache["commits"].pop(habits.commit_key("c1")))
        self.assertEqual(str(lost), "Refusing to publish: commit counts would go down (owner ×1, repository ×1, total ×1)")
        moved = self.publish_after(lambda cache: cache["commits"][habits.commit_key("c1")].__setitem__(1, "b" * 12))
        self.assertEqual(str(moved), "Refusing to publish: commit counts would go down (owner ×1, repository ×1)")
        for error in (lost, moved):
            self.assertNotIn("Org-A", str(error))
            self.assertNotIn("a" * 12, str(error))

    def test_linking_unattributed_commits_is_not_a_decrease(self):
        self.assertIsNone(self.publish_after(lambda cache: cache["commits"][habits.commit_key("c3")].__setitem__(1, "b" * 12)))

    def test_only_owners_and_repositories_with_commits_are_kept(self):
        cache = new_cache(c1="a" * 12)
        cache["repositories"] |= {"b" * 12: "someone-else"}
        cache["owners"] = {"mehroj-r": {"type": "User", "name": None}, "someone-else": {"type": "User", "name": None}}
        habits.prune(cache)
        self.assertEqual(cache["repositories"], {"a" * 12: "mehroj-r"})
        self.assertEqual(list(cache["owners"]), ["mehroj-r"])


class OwnerTableTests(unittest.TestCase):
    def cache(self):
        cache = {"owners": {
            "Big-Org": {"type": "Organization", "name": "Big | Org"},
            "mehroj-r": {"type": "User", "name": "Mehroj"},
            "Tiny-Org": {"type": "Organization", "name": "Tiny"},
            "friend": {"type": "User", "name": "Friend"},
        }, "repositories": {"1" * 12: "Big-Org", "2" * 12: "Big-Org", "3" * 12: "mehroj-r",
                            "4" * 12: "Tiny-Org", "5" * 12: "friend"}, "commits": {}}
        plan = [("1", 100, "2025-03-01T10:00Z"), ("2", 50, "2026-10-10T10:00Z"), ("3", 45, "2026-01-05T10:00Z"),
                ("4", 1, "2024-02-01T10:00Z"), ("5", 4, "2024-05-01T10:00Z")]
        for repository, count, hour in plan:
            for index in range(count):
                cache["commits"][habits.commit_key(f"{repository}-{index}")] = [hour, repository * 12]
        return cache

    def test_organizations_personal_and_other_rows(self):
        table = habits.owners_section(self.cache(), "mehroj-r").splitlines()
        self.assertEqual(len(table), 5)
        self.assertIn(r"**[Big \| Org](https://github.com/Big-Org)** | 150 | `█████████░░░` 75% | 2 |",
                      table[2])
        self.assertIn("**[Personal projects](https://github.com/mehroj-r)** | 45 | `███░░░░░░░░░` 22% | 1 |",
                      table[3])
        # A 0.5% organization and another person's repository are grouped.
        self.assertEqual(table[4], "| 👥 | Other | 5 | `░░░░░░░░░░░░` 2% | 2 |")

    def test_display_name_overrides_win(self):
        table = habits.owners_section(self.cache(), "mehroj-r", names={"Big-Org": "Big"}).splitlines()
        self.assertIn("**[Big](https://github.com/Big-Org)**", table[2])
        self.assertIn("**[Personal projects](https://github.com/mehroj-r)**", table[3])

    def test_a_malformed_names_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner-names.json"
            self.assertEqual(habits.load_names(path), {})
            for broken in ('["iDeal"]', '{"Big-Org": ""}', '{"not a login!": "Name"}', '{"Big-Org": 5}'):
                path.write_text(broken, encoding="utf-8")
                with self.assertRaises(ValueError):
                    habits.load_names(path)
            path.write_text('{"iDeal-Property-ERP": "iDeal"}', encoding="utf-8")
            self.assertEqual(habits.load_names(path), {"iDeal-Property-ERP": "iDeal"})

    def test_unattributed_commits_are_other(self):
        cache = new_cache(c1=None)
        table = habits.owners_section(cache, "mehroj-r").splitlines()
        self.assertEqual(table[2], "| 👥 | Other | 1 | `████████████` 100% | – |")


class LogoTests(unittest.TestCase):
    PNG, JPEG = b"\x89PNG\r\n\x1a\n" + b"png", b"\xff\xd8\xff" + b"jpeg"

    def test_saved_logos_replace_github_avatars_in_the_table(self):
        cache = OwnerTableTests.cache(self)
        with tempfile.TemporaryDirectory() as directory:
            logos = Path(directory)
            (logos / "Big-Org.png").write_bytes(self.PNG)
            table = habits.owners_section(cache, "mehroj-r", logos).splitlines()
        self.assertIn('<img src="images/owners/Big-Org.png" width="20" height="20" alt="">', table[2])
        self.assertIn('<img src="https://github.com/mehroj-r.png?size=40" width="20" height="20" alt="">', table[3])

    def test_logos_are_saved_once_and_hand_picked_ones_are_kept(self):
        answers = {"Org-B": self.PNG, "Org-C": b"<html>not an image</html>", "Org-D": self.JPEG,
                   "Org-E": self.PNG + b"x" * habits.MAX_LOGO, "Org-F": None}
        with tempfile.TemporaryDirectory() as directory:
            logos = Path(directory) / "images" / "owners"
            logos.mkdir(parents=True)
            (logos / "Org-A.png").write_bytes(b"hand-picked")
            with patch.object(habits, "download_avatar", side_effect=answers.get) as download:
                habits.save_logos(["Org-A", *answers], logos)
                habits.save_logos(["Org-B"], logos)
            self.assertEqual(sorted(path.name for path in logos.iterdir()), ["Org-A.png", "Org-B.png", "Org-D.jpg"])
            self.assertEqual((logos / "Org-A.png").read_bytes(), b"hand-picked")
            self.assertEqual((logos / "Org-D.jpg").read_bytes(), self.JPEG)
        self.assertEqual([call.args[0] for call in download.call_args_list], list(answers))


class PublishTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_single_commit.sh"

    def publish(self, clone):
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}
        return subprocess.run(["bash", str(self.SCRIPT), "main"], cwd=clone, env=env, capture_output=True, text=True)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        work = self.root / "seed"
        work.mkdir()
        git_commit(work, "me@example.com", "2026-10-01T10:00:00+05:00")
        (work / "README.md").write_text("one\n")
        git(work, "add", "README.md")
        git_commit(work, "bot@example.com", "2026-10-02T10:00:00+05:00")
        git(work, "push", "-q", str(self.remote), "main")

    def clone(self, name):
        git(self.root, "clone", "-q", str(self.remote), name)
        return self.root / name

    def test_history_becomes_one_epoch_commit_by_the_original_author(self):
        work = self.clone("work")
        (work / "README.md").write_text("two\n")
        git(work, "add", "README.md")
        result = self.publish(work)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "1")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%at %ct|%s", "main"),
                         "Someone <me@example.com>|Someone <me@example.com>|0 0|Initial commit")
        self.assertEqual(git(self.remote, "show", "main:README.md"), "two")

        again = self.clone("again")
        result = self.publish(again)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to publish", result.stdout)
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(again, "rev-parse", "HEAD"))

    def test_a_push_made_after_checkout_is_never_overwritten(self):
        work, other = self.clone("work"), self.clone("other")
        (other / "README.md").write_text("pushed meanwhile\n")
        git(other, "add", "README.md")
        git_commit(other, "me@example.com", "2026-10-03T10:00:00+05:00")
        git(other, "push", "-q", "origin", "main")
        result = self.publish(work)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.remote, "show", "main:README.md"), "pushed meanwhile")


class ImportTests(unittest.TestCase):
    def test_local_clone_import_counts_only_your_commits_on_the_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            mine = [git_commit(repository, "me@example.com", "2026-10-19T15:47:00+05:00"),
                    git_commit(repository, "Me@Example.com", "2026-10-20T01:10:00+05:00")]
            git_commit(repository, "colleague@example.com", "2026-10-20T02:00:00+05:00")
            git(repository, "switch", "-q", "-c", "side")
            git_commit(repository, "me@example.com", "2026-10-21T09:00:00+05:00")

            cache, origin = new_cache(), ("Some-Org", "repo")
            key = habits.repository_key(*origin)
            self.assertEqual(habits.import_git(repository, "main", ["me@example.com"], cache, origin), (2, 2))
            self.assertEqual(cache["commits"], {
                habits.commit_key(mine[0]): ["2026-10-19T10:00Z", key],
                habits.commit_key(mine[1]): ["2026-10-19T20:00Z", key],
            })
            self.assertEqual(habits.import_git(repository, "main", ["me@example.com"], cache, origin), (2, 0))
            with self.assertRaises(RuntimeError):
                habits.import_git(repository, "--output=/tmp/x", ["me@example.com"], cache, origin)

    def test_origin_remote_names_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            git(repository, "init", "-q")
            for url, expected in (("https://github.com/Deepen-Eco-System/erp-backend.git", ("Deepen-Eco-System", "erp-backend")),
                                  ("git@github.com:Some-Org/well-backend", ("Some-Org", "well-backend"))):
                git(repository, "remote", "remove", "origin") if git(repository, "remote") else None
                git(repository, "remote", "add", "origin", url)
                self.assertEqual(habits.origin_repository(repository), expected)
            git(repository, "remote", "set-url", "origin", "https://gitlab.com/someone/project.git")
            with self.assertRaises(ValueError):
                habits.origin_repository(repository)


if __name__ == "__main__":
    unittest.main()
