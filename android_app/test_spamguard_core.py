import tempfile
import unittest
from pathlib import Path
from spamguard_core import SpamGuardStore, normalize_phone, parse_number_file

class TestCore(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_phone('+34 612 345 678'), '612345678')
        self.assertEqual(normalize_phone('0034 912-345-678'), '912345678')
        self.assertIsNone(normalize_phone('512345678'))
    def test_parse(self):
        self.assertEqual(parse_number_file(b'612345678\n912345678\n612345678\n'), {'612345678','912345678'})
        with self.assertRaises(ValueError):
            parse_number_file(b'<!doctype html><html>x</html>')
    def test_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=SpamGuardStore(Path(tmp))
            s.block_path.write_text('612345678\n', encoding='utf-8')
            s.review_path.write_text('912345678\n', encoding='utf-8')
            self.assertEqual(s.lookup('612345678')[0], 'BLOCK')
            self.assertEqual(s.lookup('+34 912345678')[0], 'REVIEW')
            self.assertEqual(s.lookup('622345678')[0], 'CLEAR')

if __name__ == '__main__':
    unittest.main()
