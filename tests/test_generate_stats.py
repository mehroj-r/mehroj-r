from datetime import date, timedelta
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

SPEC = importlib.util.spec_from_file_location(
    "generate_stats", Path(__file__).resolve().parents[1] / "scripts" / "generate_stats.py"
)
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)


def repository(name="example", **overrides):
    repo = {
        "name": name, "full_name": f"mehroj-r/{name}", "owner": {"login": "mehroj-r"},
        "private": False, "fork": False, "stargazers_count": 3,
    }
    return repo | overrides


def calendar_html(days):
    return "".join(
        f'<td data-date="{day}" id="day-{index}"></td>'
        f'<tool-tip for="day-{index}">{"No" if count == 0 else count} '
        f'contributions on <span>some date.</span></tool-tip>'
        for index, (day, count) in enumerate(days)
    )


def days_for(end, count=1):
    return [
        {"date": (end - timedelta(days=364 - index)).isoformat(), "count": count}
        for index in range(365)
    ]


class PublicDataTests(unittest.TestCase):
    def test_repository_pagination_and_public_owner_filter(self):
        first = [repository(str(index)) for index in range(100)]
        second = [repository("fork", fork=True), repository("private", private=True),
                  repository("elsewhere", owner={"login": "someone-else"}), repository("last")]
        with patch.object(stats, "fetch_json", side_effect=[first, second]) as fetch:
            result = stats.public_repositories("mehroj-r", "token")
        self.assertEqual(len(result), 101)
        self.assertEqual(result[-1]["name"], "last")
        self.assertIn("page=2", fetch.call_args[0][0])

    def test_languages_are_byte_sums_and_deterministically_sorted(self):
        with patch.object(stats, "fetch_json", side_effect=[{"Python": 20, "Dart": 30}, {"Python": 20}]):
            result = stats.language_totals([repository("one"), repository("two")])
        self.assertEqual(list(result.items()), [("Python", 40), ("Dart", 30)])

    def test_invalid_language_response_is_not_zero_filled(self):
        with patch.object(stats, "fetch_json", return_value={"Python": -1}):
            with self.assertRaises(ValueError):
                stats.language_totals([repository()])

    def test_repository_url_cannot_redirect_token(self):
        with self.assertRaises(ValueError):
            stats.language_totals([repository(full_name="https://evil.example")], "secret")

    def test_installation_token_denial_falls_back_to_public_request(self):
        error = HTTPError("https://api.github.com/example", 403, "denied", {}, None)
        with patch.object(stats, "urlopen", side_effect=error) as opener:
            with self.assertRaises(RuntimeError):
                stats.fetch("https://api.github.com/example", "secret")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args_list[0].args[0].get_header("Authorization"), "Bearer secret")
        self.assertIsNone(opener.call_args_list[1].args[0].get_header("Authorization"))

    def test_calendar_counts_zero_singular_commas_and_nested_text(self):
        parser = stats.ContributionParser()
        parser.feed(calendar_html([("2026-01-01", 0), ("2026-01-02", 1), ("2026-01-03", "1,234")]))
        self.assertEqual(list(parser.counts().values()), [0, 1, 1234])

    def test_calendar_markup_change_fails_closed(self):
        parser = stats.ContributionParser()
        parser.feed('<td data-date="2026-01-01" id="one"></td>')
        with self.assertRaises(ValueError):
            parser.counts()
        with self.assertRaises(ValueError):
            stats.ContributionParser().counts()

    def test_calendar_rejects_duplicate_dates(self):
        parser = stats.ContributionParser()
        parser.feed(calendar_html([("2026-01-01", 1), ("2026-01-01", 2)]))
        with self.assertRaises(ValueError):
            parser.counts()

    def test_calendar_is_anonymous_and_filters_year_and_leap_day(self):
        end = date(2024, 3, 1)
        days = days_for(end)
        html = calendar_html([(item["date"], item["count"]) for item in days])
        with patch.object(stats, "fetch", return_value=html) as fetch:
            result = stats.contribution_days("mehroj-r", end)
        self.assertEqual(len(result), 365)
        self.assertIn("2024-02-29", [item["date"] for item in result])
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(len(call.args) == 1 and not call.kwargs for call in fetch.call_args_list))

    def test_missing_calendar_date_fails(self):
        with patch.object(stats, "fetch", return_value=calendar_html([("2026-01-01", 0)])):
            with self.assertRaises(ValueError):
                stats.contribution_days("mehroj-r", date(2026, 1, 1))

    def test_metric_and_streak_math(self):
        days = [{"count": count} for count in (2, 1, 0, 3, 5, 7, 0)]
        result = stats.summarize(days, [repository()], {"Python": 10, "Empty": 0})
        self.assertEqual(list(result.values()), [18, 5, 3, 1, 3, 1])


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.days = days_for(date(2026, 10, 4))
        self.metrics = stats.summarize(self.days, [repository()], {"Python": 75, "Dart": 25})

    def test_svg_output_is_valid_and_self_contained_in_both_themes(self):
        for theme in stats.THEMES.values():
            graphics = [stats.banner(theme), stats.stats_card(self.metrics, self.days, theme),
                        stats.activity_card(self.days, theme), stats.languages_card({"Python": 75, "Dart": 25}, theme)]
            for graphic in graphics:
                root = ET.fromstring(graphic)
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertIn('<title id="title">', graphic)
                self.assertNotIn("<script", graphic)
                self.assertNotIn("href=", graphic)

    def test_svg_and_markdown_escape_untrusted_language_text(self):
        name = '<script>alert("x")</script>|bad'
        graphic = stats.languages_card({name: 10}, stats.THEMES["light"])
        ET.fromstring(graphic)
        self.assertNotIn("<script>", graphic)
        table = stats.data_table("mehroj-r", self.days, self.metrics, [], {name: 10})
        self.assertIn("&#124;bad", table)
        self.assertNotIn("<script>", table)

    def test_language_tail_is_other_and_totals_are_preserved(self):
        languages = {f"Language {index}": index + 1 for index in range(10)}
        result = stats.displayed_languages(languages)
        self.assertEqual(len(result), 6)
        self.assertEqual(result[-1][0], "Other")
        self.assertEqual(sum(size for _, size in result), sum(languages.values()))

    def test_empty_data_has_valid_svg_and_no_division_by_zero(self):
        for theme in stats.THEMES.values():
            ET.fromstring(stats.languages_card({}, theme))
            ET.fromstring(stats.activity_card(days_for(date(2026, 10, 4), 0), theme))
        self.assertEqual(stats.heat_level(0, []), 0)
        self.assertEqual(stats.heat_level(1, [1]), 1)

    def test_quantile_heatmap_levels_are_labeled_and_monotonic(self):
        thresholds = stats.contribution_thresholds([{"count": value} for value in (0, 1, 2, 3, 4, 5, 100)])
        levels = [stats.heat_level(value, thresholds) for value in (0, 1, 2, 3, 4, 5, 100)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(thresholds[-1], 100)
        self.assertLessEqual(len(thresholds), 4)

    def test_heatmap_geometry_stays_inside_canvas(self):
        for weekday in range(7):
            days = days_for(date(2026, 10, 4) + timedelta(days=weekday))
            root = ET.fromstring(stats.activity_card(days, stats.THEMES["light"]))
            cells = root.findall('.//{http://www.w3.org/2000/svg}g/{http://www.w3.org/2000/svg}rect')
            for cell in cells:
                self.assertLessEqual(float(cell.attrib["x"]) + float(cell.attrib["width"]), 960)
                self.assertLessEqual(float(cell.attrib["y"]) + float(cell.attrib["height"]), 315)

    def test_fetch_failure_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "docs" / "github-stats.md"
            old.parent.mkdir()
            old.write_text("previous successful output")
            with patch.object(stats, "public_repositories", side_effect=RuntimeError("offline")):
                with self.assertRaises(RuntimeError):
                    stats.generate("mehroj-r", date(2026, 10, 4), root)
            self.assertEqual(old.read_text(), "previous successful output")

    def test_success_writes_only_expected_output_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(stats, "public_repositories", return_value=[]), \
                 patch.object(stats, "language_totals", return_value={}), \
                 patch.object(stats, "contribution_days", return_value=self.days), \
                 patch("builtins.print"):
                stats.generate("mehroj-r", date(2026, 10, 4), root)
            files = [path for path in root.rglob("*") if path.is_file()]
            self.assertEqual(len(files), 17)
            self.assertEqual(len(list((root / "images" / "generated").glob("*.svg"))), 16)
            self.assertFalse(list(root.rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
