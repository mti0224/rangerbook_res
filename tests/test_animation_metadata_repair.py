import copy
import unittest

from scripts.repair_animation_metadata import pixel_numbers, repair_part


class AnimationMetadataRepairTests(unittest.TestCase):
    def test_decimal_coordinates_and_negative_values(self):
        self.assertEqual(pixel_numbers("{{98.0,322.0},{99.0,99.0}}", 4), [98, 322, 99, 99])
        self.assertEqual(pixel_numbers("{{-1.6,2.5},{18.0,41.0}}", 4), [-2, 3, 18, 41])
        self.assertEqual(pixel_numbers("{{98,322},{99,99}}", 4), [98, 322, 99, 99])
        self.assertIsNone(pixel_numbers("broken", 4))

    def test_rect_repair_alias_and_existing_canonical_priority(self):
        part = {
            "images": [{"name": "body_18x41.png"}, {"name": "body_18x41_2.png"}],
            "sprites": {
                "body_18x41_2.png": {"rect": [98, 0, 322, 0], "rotated": True, "offset": [1, 2]},
                "protected.png": {"rect": [1, 1, 5, 5]},
            },
        }
        frames = {
            "body_18x41_2.png": {
                "textureRect": "{{98.0,322.0},{18.0,41.0}}",
                "aliases": ["body_18x41.png", "protected.png"],
            },
        }
        self.assertEqual(repair_part(part, frames), (1, 1))
        self.assertEqual(part["sprites"]["body_18x41.png"]["rect"], [98, 322, 18, 41])
        self.assertEqual(part["sprites"]["body_18x41.png"]["offset"], [1, 2])
        self.assertTrue(part["sprites"]["body_18x41.png"]["rotated"])
        self.assertEqual(part["sprites"]["protected.png"]["rect"], [1, 1, 5, 5])
        self.assertEqual(repair_part(part, frames), (0, 0))
        self.assertNotEqual(id(part["sprites"]["body_18x41.png"]), id(part["sprites"]["body_18x41_2.png"]))

    def test_invalid_rect_is_not_added(self):
        part = {"sprites": {}, "images": [{"name": "bad.png"}]}
        self.assertEqual(repair_part(part, {"bad.png": {"textureRect": "{{1,1},{0,2}}", "aliases": ["x.png"]}}), (0, 0))
        self.assertEqual(part["sprites"], {})


if __name__ == "__main__":
    unittest.main()
