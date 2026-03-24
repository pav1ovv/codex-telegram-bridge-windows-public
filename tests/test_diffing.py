import unittest


class DiffingTests(unittest.TestCase):
    def test_normalize_window_text(self) -> None:
        from tgcod.diffing import normalize_window_text

        normalized = normalize_window_text("a\r\n\r\nb  \n")
        self.assertEqual(normalized, "a\n\nb")

    def test_extract_increment_returns_suffix_for_appended_output(self) -> None:
        from tgcod.diffing import extract_increment

        previous = "user prompt\nassistant line 1"
        current = "user prompt\nassistant line 1\nassistant line 2"
        increment = extract_increment(previous, current)

        self.assertEqual(increment, "assistant line 2")

    def test_extract_increment_handles_non_prefix_change(self) -> None:
        from tgcod.diffing import extract_increment

        previous = "line 1\nline 2"
        current = "line 1\nline 2 updated\nline 3"
        increment = extract_increment(previous, current)

        self.assertIn("line 2 updated", increment)
        self.assertIn("line 3", increment)


if __name__ == "__main__":
    unittest.main()
