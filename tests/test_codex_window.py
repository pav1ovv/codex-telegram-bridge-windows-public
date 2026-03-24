import unittest


class CodexWindowTests(unittest.TestCase):
    def test_choose_backend_prefers_uia_when_editable_controls_exist(self) -> None:
        from tgcod.codex_window import DetectionProbe, choose_backend

        backend = choose_backend(
            DetectionProbe(
                has_window=True,
                has_editable_control=True,
                readable_text_controls=3,
                has_prosemirror_editor=False,
                forced_backend=None,
            )
        )
        self.assertEqual(backend, "uia")

    def test_choose_backend_falls_back_to_ocr(self) -> None:
        from tgcod.codex_window import DetectionProbe, choose_backend

        backend = choose_backend(
            DetectionProbe(
                has_window=True,
                has_editable_control=False,
                readable_text_controls=0,
                has_prosemirror_editor=False,
                forced_backend=None,
            )
        )
        self.assertEqual(backend, "ocr")

    def test_choose_backend_prefers_uia_when_prosemirror_exists_even_without_text(self) -> None:
        from tgcod.codex_window import DetectionProbe, choose_backend

        backend = choose_backend(
            DetectionProbe(
                has_window=True,
                has_editable_control=False,
                readable_text_controls=0,
                has_prosemirror_editor=True,
                forced_backend=None,
            )
        )
        self.assertEqual(backend, "uia")

    def test_stop_strategy_prioritizes_visible_stop_button(self) -> None:
        from tgcod.codex_window import build_stop_strategy

        strategy = build_stop_strategy(has_stop_button=True)
        self.assertEqual(strategy[0], "button")
        self.assertIn("esc", strategy)
        self.assertIn("ctrl+c", strategy)

    def test_health_tracker_marks_window_unavailable_after_repeated_failures(self) -> None:
        from tgcod.codex_window import WindowHealthTracker

        tracker = WindowHealthTracker(max_failures=2)
        tracker.record_failure()
        self.assertTrue(tracker.is_healthy)
        tracker.record_failure()
        self.assertFalse(tracker.is_healthy)
        tracker.record_success()
        self.assertTrue(tracker.is_healthy)

    def test_chat_content_filter_excludes_sidebar_and_offscreen_controls(self) -> None:
        from tgcod.codex_window import is_probably_chat_content_rect

        window_rect = (0, 0, 1920, 1032)

        self.assertFalse(is_probably_chat_content_rect((8, 180, 277, 252), window_rect))
        self.assertFalse(is_probably_chat_content_rect((770, -29544, 1467, -29522), window_rect))
        self.assertFalse(is_probably_chat_content_rect((794, 950, 858, 978), window_rect))
        self.assertTrue(is_probably_chat_content_rect((747, 427, 1434, 676), window_rect))

    def test_ocr_crop_box_targets_center_chat_area(self) -> None:
        from tgcod.codex_window import get_ocr_content_crop_box

        crop = get_ocr_content_crop_box((1920, 1032))
        self.assertEqual(crop, (345, 41, 1920, 909))

    def test_ocr_input_click_point_targets_bottom_center_pane(self) -> None:
        from tgcod.codex_window import get_ocr_input_click_point

        point = get_ocr_input_click_point((0, 0, 1920, 1032))
        self.assertEqual(point, (944, 919))

    def test_visible_input_filter_rejects_terminal_input_below_window(self) -> None:
        from tgcod.codex_window import is_probably_visible_input_rect

        window_rect = (0, 0, 1920, 1032)

        self.assertFalse(is_probably_visible_input_rect((537, 1068, 545, 1084), window_rect))
        self.assertTrue(is_probably_visible_input_rect((900, 900, 1500, 980), window_rect))

    def test_prosemirror_editor_filter_matches_bottom_chat_editor_group(self) -> None:
        from tgcod.codex_window import is_probably_prosemirror_editor_rect

        window_rect = (0, 0, 1920, 1032)
        self.assertTrue(
            is_probably_prosemirror_editor_rect(
                control_type="Group",
                class_name="ProseMirror ProseMirror-focused",
                rect=(751, 900, 1469, 941),
                window_rect=window_rect,
            )
        )

    def test_prosemirror_editor_filter_rejects_toolbar_group(self) -> None:
        from tgcod.codex_window import is_probably_prosemirror_editor_rect

        window_rect = (0, 0, 1920, 1032)
        self.assertFalse(
            is_probably_prosemirror_editor_rect(
                control_type="Group",
                class_name="ToolbarButton",
                rect=(1220, 45, 1400, 80),
                window_rect=window_rect,
            )
        )

    def test_layout_visible_chat_elements_merges_same_row_fragments_and_filters_copy_buttons(self) -> None:
        from tgcod.codex_window import layout_visible_chat_elements, sanitize_visible_chat_text

        elements = [
            ((738, 821, 871, 838), sanitize_visible_chat_text("Выполняется команда", "Text")),
            ((870, 821, 908, 838), sanitize_visible_chat_text("для 1s", "Text")),
            ((747, 852, 773, 869), sanitize_visible_chat_text("Shell", "Text")),
            ((747, 882, 1434, 919), sanitize_visible_chat_text("$ python -m tgcod.main", "Button")),
            ((1433, 882, 1458, 907), sanitize_visible_chat_text("Копировать", "Button")),
        ]
        filtered = [(rect, text) for rect, text in elements if text]

        snapshot = layout_visible_chat_elements(filtered)
        self.assertIn("Выполняется команда для 1s", snapshot)
        self.assertIn("Shell", snapshot)
        self.assertIn("$ python -m tgcod.main", snapshot)
        self.assertNotIn("Копировать", snapshot)


if __name__ == "__main__":
    unittest.main()
