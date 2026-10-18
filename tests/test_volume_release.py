"""Public release/reload behavior with owned data references and no GPU."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import weakref

import numpy as np

from openmind.viewer import VolumeViewer


class FakeState(SimpleNamespace):
    def update(self, **values):
        vars(self).update(values)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeScalars:
    def __init__(self, array):
        self.array = array


class FakeImage:
    def __init__(self, scalars, clear_calls):
        self.scalars = scalars
        self.clear_calls = clear_calls

    def GetPointData(self):
        return self

    def SetScalars(self, scalars):
        self.scalars = scalars
        self.clear_calls.append(scalars)


class FakeMapper:
    def __init__(self, image):
        self.input = image
        self.graphics_release_calls = []
        self.input_removals = 0

    def ReleaseGraphicsResources(self, window):
        self.graphics_release_calls.append(window)

    def RemoveAllInputs(self):
        self.input = None
        self.input_removals += 1


class VolumeReleaseTests(unittest.TestCase):
    def make_viewer(self):
        viewer = VolumeViewer.__new__(VolumeViewer)
        viewer._building = False
        viewer.current_dataset = "/tmp/primary/catalog.json"
        viewer.current_level = "5um"
        viewer._array = np.zeros((64, 64, 128), dtype=np.uint8)
        viewer._vtk_scalars = FakeScalars(viewer._array)
        clear_calls = []
        viewer._image = FakeImage(viewer._vtk_scalars, clear_calls)
        viewer.mapper = FakeMapper(viewer._image)
        viewer.volume = Mock()
        viewer.render_window = object()
        viewer.renderer = Mock()
        viewer.state = FakeState(
            level="5um", loaded_mib=viewer._array.nbytes / 1024**2,
            volume_loaded=True, busy=False, error_message="", release_message=""
        )
        viewer.refresh_memory = Mock()
        viewer.update = Mock()
        return viewer, clear_calls

    def test_release_detaches_voxels_and_graphics_but_preserves_selected_level(self):
        viewer, clear_calls = self.make_viewer()
        references = [weakref.ref(value) for value in
                      (viewer._array, viewer._image, viewer._vtk_scalars)]

        viewer.release_volume()

        self.assertIsNone(viewer._array)
        self.assertIsNone(viewer._image)
        self.assertIsNone(viewer._vtk_scalars)
        self.assertTrue(all(reference() is None for reference in references))
        self.assertIsNone(viewer.mapper.input)
        self.assertEqual(viewer.mapper.graphics_release_calls, [viewer.render_window])
        self.assertEqual(viewer.mapper.input_removals, 1)
        self.assertEqual(clear_calls, [None])
        viewer.volume.VisibilityOff.assert_called_once_with()
        self.assertIsNone(viewer.current_level)
        self.assertEqual(viewer.state.level, "5um")
        self.assertEqual(viewer.state.loaded_mib, 0)
        self.assertFalse(viewer.state.volume_loaded)
        self.assertIn("0.50 MiB", viewer.state.release_message)
        viewer.refresh_memory.assert_called_once_with()

    def test_reload_loads_the_chosen_level_when_unloaded(self):
        viewer, _ = self.make_viewer()
        viewer.release_volume()
        viewer.state.level = "1um"

        def load(name):
            viewer.current_level = name
            viewer.state.volume_loaded = True

        viewer._load_level = Mock(side_effect=load)

        viewer.reload_volume()

        viewer._load_level.assert_called_once_with("1um")
        self.assertEqual(viewer.current_level, "1um")
        self.assertTrue(viewer.state.volume_loaded)
        self.assertFalse(viewer.state.busy)

    def test_reload_does_not_load_again_while_volume_is_loaded(self):
        viewer, _ = self.make_viewer()
        viewer._load_level = Mock()

        viewer.reload_volume()

        viewer._load_level.assert_not_called()
        self.assertEqual(viewer.current_level, "5um")
        self.assertTrue(viewer.state.volume_loaded)

    def test_failed_dataset_switch_while_released_keeps_a_reloadable_selection(self):
        viewer, _ = self.make_viewer()
        viewer.np = np
        viewer.max_bytes = 1024**2
        other = "/tmp/other/catalog.json"
        viewer.catalogs = {
            viewer.current_dataset: {"name": "Primary", "levels": [
                {"name": "1um", "shape_zyx": [64, 64, 64], "spacing_um_xyz": [1, 1, 1], "dtype": "uint8"},
                {"name": "5um", "shape_zyx": [16, 16, 16], "spacing_um_xyz": [5, 5, 5], "dtype": "uint8"},
            ]},
            other: {"name": "Other", "levels": [
                {"name": "other-native", "shape_zyx": [64, 64, 64], "spacing_um_xyz": [20, 20, 20], "dtype": "uint8"},
            ]},
        }
        viewer._set_catalog(viewer.current_dataset, preferred_level="5um")
        viewer.release_volume()
        viewer.state.dataset = other
        viewer._load_level = Mock(side_effect=OSError("Other region is unavailable"))

        viewer._dataset_changed(dataset=other)

        viewer._load_level.assert_called_once_with("other-native", reset_camera=True)
        self.assertEqual(viewer.catalog_path, Path("/tmp/primary/catalog.json"))
        self.assertEqual(viewer.state.dataset, viewer.current_dataset)
        self.assertEqual(viewer.state.level, "5um")
        self.assertIsNone(viewer.current_level)
        self.assertFalse(viewer.state.volume_loaded)
        self.assertFalse(viewer.state.busy)
        self.assertIn("Other region is unavailable", viewer.state.error_message)

        def reload(name):
            viewer.current_level = name
            viewer.state.volume_loaded = True

        viewer._load_level = Mock(side_effect=reload)
        viewer.reload_volume()
        viewer._load_level.assert_called_once_with("5um")
        self.assertEqual(viewer.current_level, "5um")


if __name__ == "__main__":
    unittest.main()
