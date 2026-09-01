import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from publish_aemet_warnings import current_warnings, point_in_ring, zone_for_point


class AemetWarningsPipelineTest(unittest.TestCase):
    def test_point_in_polygon_resolves_expected_zone(self):
        features = [{
            "type": "Feature",
            "properties": {"zoneCode": "690804"},
            "geometry": {"type": "Polygon", "coordinates": [[[2.0, 41.2], [2.4, 41.2], [2.4, 41.6], [2.0, 41.6], [2.0, 41.2]]]},
        }]
        self.assertTrue(point_in_ring(2.1734, 41.3851, features[0]["geometry"]["coordinates"][0]))
        self.assertEqual("690804", zone_for_point(features, 2.1734, 41.3851))
        self.assertIsNone(zone_for_point(features, -3.7, 40.4))

    def test_expired_warning_is_not_published(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        warnings = [
            {"endsAt": "2026-09-01T23:59:59Z"},
            {"endsAt": "2026-09-02T00:00:00Z"},
            {"endsAt": "2026-09-03T00:00:00Z"},
        ]
        self.assertEqual(warnings[1:], current_warnings(warnings, now))


if __name__ == "__main__":
    unittest.main()
