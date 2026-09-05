from unittest import TestCase

from wayland.grid import align_offset, cell_rect, parse_track, size_tracks


class TestParseTrack(TestCase):
    def test_forms(self):
        self.assertEqual(parse_track("auto"), ("auto", 0.0))
        self.assertEqual(parse_track("2fr"), ("fr", 2.0))
        self.assertEqual(parse_track("fr"), ("fr", 1.0))
        self.assertEqual(parse_track("120px"), ("px", 120.0))
        self.assertEqual(parse_track(80), ("px", 80.0))
        self.assertEqual(parse_track(" 3FR "), ("fr", 3.0))


class TestSizeTracks(TestCase):
    def test_single_fr_takes_all(self):
        self.assertEqual(size_tracks(["1fr"], 1000, 40), [(0.0, 1000.0)])

    def test_px_then_fr_share_leftover(self):
        self.assertEqual(size_tracks(["200px", "1fr", "1fr"], 1000, 0),
                         [(0.0, 200.0), (200.0, 400.0), (600.0, 400.0)])

    def test_gap_reduces_inner(self):
        self.assertEqual(size_tracks(["1fr", "1fr"], 1000, 40),
                         [(0.0, 480.0), (520.0, 480.0)])

    def test_fr_weights(self):
        self.assertEqual(size_tracks(["3fr", "2fr"], 1000, 0),
                         [(0.0, 600.0), (600.0, 400.0)])

    def test_auto_track_uses_measured_size(self):
        self.assertEqual(size_tracks(["auto", "1fr"], 1000, 40, {0: 300}),
                         [(0.0, 300.0), (340.0, 660.0)])

    def test_auto_zero_collapses_to_gap_only(self):
        cols = size_tracks(["auto", "1fr"], 1000, 40, {0: 0})
        self.assertEqual(cols[0][1], 0.0)
        self.assertEqual(cols[1][1], 960.0)

    def test_overflow_zeroes_fr(self):
        self.assertEqual(size_tracks(["800px", "1fr"], 1000, 0),
                         [(0.0, 800.0), (800.0, 200.0)])
        self.assertEqual(size_tracks(["1200px", "1fr"], 1000, 0),
                         [(0.0, 1200.0), (1200.0, 0.0)])


class TestAlignOffset(TestCase):
    def test_modes(self):
        self.assertEqual(align_offset("start", 400, 1000), 0.0)
        self.assertEqual(align_offset("center", 400, 1000), 300.0)
        self.assertEqual(align_offset("end", 400, 1000), 600.0)
        self.assertEqual(align_offset("center", 1200, 1000), 0.0)


class TestCellRect(TestCase):
    def setUp(self):
        self.cols = size_tracks(["1fr", "auto"], 1000, 40, {1: 300})
        self.rows = size_tracks(["auto", "1fr", "auto"], 800, 40,
                                {0: 60, 2: 40})

    def test_plain_cell(self):
        self.assertEqual(cell_rect({"col": 0, "row": 1}, self.cols, self.rows,
                                   40),
                         (0.0, 100.0, 660.0, 620.0))

    def test_row_span_covers_all_rows(self):
        x, y, w, h = cell_rect({"col": 1, "row": 0, "rowspan": 3},
                               self.cols, self.rows, 40)
        self.assertEqual((x, y, w, h), (700.0, 0.0, 300.0, 800.0))

    def test_span_clamped_to_track_count(self):
        x, y, w, h = cell_rect({"col": 0, "row": 0, "colspan": 9},
                               self.cols, self.rows, 40)
        self.assertEqual(x, 0.0)
        self.assertAlmostEqual(w, self.cols[-1][0] + self.cols[-1][1])
