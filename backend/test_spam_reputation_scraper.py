import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import spam_reputation_scraper as s


class TestPhones(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(s.normalize_phone("+34 612 345 678"), "612345678")
        self.assertEqual(s.normalize_phone("0034 912-345-678"), "912345678")
        self.assertEqual(s.normalize_phone("612345678"), "612345678")
        self.assertIsNone(s.normalize_phone("123456789"))
        self.assertIsNone(s.normalize_phone("61234567890"))

    def test_extract_with_separators(self):
        text = "Llamada de +34 612 345 678 y también 912-345-678."
        found = [p for p, _, _ in s.extract_phone_matches(text)]
        self.assertEqual(found, ["612345678", "912345678"])

    def test_context(self):
        self.assertEqual(
            s.classify_context("Reportado como Estafa", "discovery"),
            "explicit_spam",
        )
        self.assertEqual(
            s.classify_context("Número fiable", "explicit_spam"),
            "safe",
        )


class TestTellows(unittest.TestCase):
    def test_tellows_table_score(self):
        html = """
        <table>
          <tr><th>Pos</th><th>Número</th><th>Evaluaciones</th><th>Búsquedas</th><th>Score</th></tr>
          <tr><td>1</td><td>+34 612 345 678</td><td>4</td><td>1000</td><td>8</td></tr>
          <tr><td>2</td><td>+34 912 345 678</td><td>2</td><td>500</td><td>2</td></tr>
        </table>
        """
        src = s.Source(
            id="tellows",
            group="tellows",
            url="https://example.invalid",
            weight=0.95,
            parser="tellows",
        )
        ev = s.parse_tellows(src, html)
        kinds = {(e.phone, e.kind) for e in ev}
        self.assertIn(("612345678", "explicit_spam"), kinds)
        self.assertIn(("912345678", "safe"), kinds)


class TestScoring(unittest.TestCase):
    def _row(self, **kwargs):
        defaults = {
            "phone": "612345678",
            "first_seen": "2026-08-01T00:00:00+00:00",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "seen_runs": 1,
        }
        defaults.update(kwargs)
        return defaults

    def test_one_high_explicit_source_can_block(self):
        phone_row = self._row()
        ev = [{
            "source_group": "cleverdialer",
            "kind": "explicit_spam",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "max_weight": 0.95,
        }]
        score = s.score_phone(
            phone_row,
            ev,
            threshold=70,
            review_threshold=45,
            max_block_age_days=120,
            whitelist=set(),
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(score.status, "BLOCK")
        self.assertGreaterEqual(score.score, 70)

    def test_discovery_only_does_not_block(self):
        phone_row = self._row(seen_runs=3)
        ev = [{
            "source_group": "datostelefonicos",
            "kind": "discovery",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "max_weight": 0.25,
        }]
        score = s.score_phone(
            phone_row,
            ev,
            threshold=70,
            review_threshold=45,
            max_block_age_days=120,
            whitelist=set(),
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        self.assertNotEqual(score.status, "BLOCK")

    def test_two_independent_sources_boost(self):
        phone_row = self._row(seen_runs=2)
        ev = [
            {
                "source_group": "numerospam",
                "kind": "suspicious",
                "last_seen": "2026-08-25T00:00:00+00:00",
                "max_weight": 0.58,
            },
            {
                "source_group": "telefonospam",
                "kind": "explicit_spam",
                "last_seen": "2026-08-25T00:00:00+00:00",
                "max_weight": 0.82,
            },
        ]
        score = s.score_phone(
            phone_row,
            ev,
            threshold=70,
            review_threshold=45,
            max_block_age_days=120,
            whitelist=set(),
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(score.status, "BLOCK")

    def test_safe_source_reduces_score(self):
        phone_row = self._row(seen_runs=2)
        base = [{
            "source_group": "cleverdialer",
            "kind": "explicit_spam",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "max_weight": 0.95,
        }]
        with_safe = base + [{
            "source_group": "tellows",
            "kind": "safe",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "max_weight": 0.95,
        }]
        a = s.score_phone(
            phone_row, base, threshold=70, review_threshold=45,
            max_block_age_days=120, whitelist=set(),
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        b = s.score_phone(
            phone_row, with_safe, threshold=70, review_threshold=45,
            max_block_age_days=120, whitelist=set(),
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        self.assertLess(b.score, a.score)

    def test_whitelist_always_allows(self):
        phone_row = self._row()
        ev = [{
            "source_group": "cleverdialer",
            "kind": "explicit_spam",
            "last_seen": "2026-08-25T00:00:00+00:00",
            "max_weight": 0.95,
        }]
        score = s.score_phone(
            phone_row,
            ev,
            threshold=70,
            review_threshold=45,
            max_block_age_days=120,
            whitelist={"612345678"},
            now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(score.status, "ALLOW")


class TestDatabase(unittest.TestCase):
    def test_same_domain_pages_do_not_create_independent_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = s.ReputationDB(Path(tmp) / "test.sqlite3")
            try:
                evidence = [
                    s.Evidence(
                        "612345678", "page_a", "numerospam",
                        "https://numerospam.es/a", 0.58, "suspicious", "spam"
                    ),
                    s.Evidence(
                        "612345678", "page_b", "numerospam",
                        "https://numerospam.es/b", 0.58, "suspicious", "spam"
                    ),
                ]
                db.ingest_run(evidence, "2026-08-25T00:00:00+00:00")
                rows = db.evidence_for_phone("612345678")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["source_group"], "numerospam")
            finally:
                db.close()

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db1 = s.ReputationDB(tmp / "a.sqlite3")
            try:
                ev = [
                    s.Evidence(
                        "612345678", "src", "domain-a",
                        "https://example.invalid/a", 0.9, "explicit_spam", "spam"
                    )
                ]
                db1.ingest_run(ev, "2026-08-25T00:00:00+00:00")
                state = tmp / "spam_state.json"
                db1.export_state(state)
            finally:
                db1.close()

            db2 = s.ReputationDB(tmp / "b.sqlite3")
            try:
                phones, evidence = db2.import_state(state)
                self.assertEqual(phones, 1)
                self.assertEqual(evidence, 1)
                self.assertFalse(db2.is_empty())
                rows = db2.evidence_for_phone("612345678")
                self.assertEqual(rows[0]["source_group"], "domain-a")
            finally:
                db2.close()


if __name__ == "__main__":
    unittest.main()
