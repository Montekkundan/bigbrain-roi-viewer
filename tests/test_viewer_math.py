"""Crop geometry tests independent of a GPU, a browser, or dataset access."""

import unittest

from openmind.viewer import camera_corner_planes, physical_pivot


class CropMathTests(unittest.TestCase):
    def test_every_camera_octant_selects_exactly_its_own_box(self):
        bounds = (-10, 10, -20, 20, -30, 30)
        for x in (-1, 1):
            for y in (-1, 1):
                for z in (-1, 1):
                    planes = camera_corner_planes(bounds, (x, y, z), (0, 0, 0))
                    for axis, side in enumerate((x, y, z)):
                        self.assertEqual(planes[2 * axis:2 * axis + 2], (0, bounds[2 * axis + 1]) if side > 0 else (bounds[2 * axis], 0))

    def test_camera_comparison_uses_world_pivot(self):
        self.assertEqual(camera_corner_planes((0, 20, 10, 30, 20, 40), (11, 19, 31), (10, 20, 30)), (10, 20, 10, 20, 30, 40))

    def test_anisotropic_bounds_and_clamped_percentages(self):
        self.assertEqual(physical_pivot((10, 30, -20, 80, 3, 7), (25, -5, 120)), (15, -20, 7))


if __name__ == "__main__":
    unittest.main()
