"""NetGravity — CoG Screener Tests"""

import math
import pytest
from netgravity.cog.screener import weiszfeld_cog, multi_cog


class TestWeiszfeldCoG:

    def test_single_point(self):
        """CoG of a single point is that point."""
        result = weiszfeld_cog([(5.0, 10.0)], [100.0])
        assert abs(result.x - 5.0) < 1e-6
        assert abs(result.y - 10.0) < 1e-6

    def test_two_equal_weight_points(self):
        """CoG of two equal-weight points is their midpoint."""
        result = weiszfeld_cog([(0.0, 0.0), (10.0, 0.0)], [50.0, 50.0])
        assert abs(result.x - 5.0) < 0.01
        assert abs(result.y - 0.0) < 0.01

    def test_weighted_center_pulls_toward_heavier_point(self):
        """Heavy weight at (0,0) vs light at (10,0) → CoG is left of 5."""
        result = weiszfeld_cog([(0.0, 0.0), (10.0, 0.0)], [90.0, 10.0])
        assert result.x < 5.0, f"Expected CoG left of 5, got {result.x}"

    def test_convergence_flag(self):
        pts = [(i * 10.0, i * 5.0) for i in range(6)]
        wts = [100.0] * 6
        result = weiszfeld_cog(pts, wts)
        assert result.converged, "Weiszfeld should converge on simple inputs"

    def test_disclaimer_present(self):
        result = weiszfeld_cog([(1.0, 2.0)], [10.0])
        assert "SCREENING OUTPUT" in result.disclaimer

    def test_total_weighted_distance_non_negative(self):
        pts = [(0.0, 0.0), (10.0, 10.0), (5.0, 5.0)]
        wts = [100.0, 80.0, 120.0]
        result = weiszfeld_cog(pts, wts)
        assert result.total_weighted_dist >= 0.0

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            weiszfeld_cog([(0.0, 0.0), (1.0, 1.0)], [100.0])

    def test_zero_weight_raises(self):
        with pytest.raises(ValueError):
            weiszfeld_cog([(0.0, 0.0)], [0.0])


class TestMultiCoG:

    def test_two_clusters(self):
        """Multi-CoG with 2 centers on clearly separated demand."""
        pts = [(0.0, 0.0)] * 10 + [(100.0, 100.0)] * 10
        wts = [50.0] * 20
        result = multi_cog(pts, wts, n_facilities=2)
        assert result.n_facilities == 2
        assert len(result.cog_locations) == 2
        assert "SCREENING OUTPUT" in result.disclaimer

    def test_single_center_equals_weiszfeld(self):
        """Multi-CoG with 1 center should equal single Weiszfeld."""
        pts = [(i * 10.0, 0.0) for i in range(5)]
        wts = [10.0, 20.0, 30.0, 40.0, 50.0]
        single = weiszfeld_cog(pts, wts)
        multi  = multi_cog(pts, wts, n_facilities=1)
        assert len(multi.cog_locations) == 1
        assert abs(multi.cog_locations[0].x - single.x) < 0.1
