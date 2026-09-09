import unittest

from core.security import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    has_extension,
    is_safe_output_name,
    is_valid_job_id,
    safe_filename,
)


class SecurityHelpersTest(unittest.TestCase):
    def test_filename_is_reduced_to_basename(self):
        self.assertEqual(safe_filename(r"..\..\video.mp4", "input.mp4"), "video.mp4")
        self.assertEqual(safe_filename("", "input.mp4"), "input.mp4")

    def test_extensions_are_case_insensitive(self):
        self.assertTrue(has_extension("clip.MP4", VIDEO_EXTENSIONS))
        self.assertTrue(has_extension("logo.PNG", IMAGE_EXTENSIONS))
        self.assertFalse(has_extension("clip.exe", VIDEO_EXTENSIONS))

    def test_job_id_and_output_name_validation(self):
        self.assertTrue(is_valid_job_id("abcdef12"))
        self.assertFalse(is_valid_job_id("../../etc"))
        self.assertTrue(is_safe_output_name("final.mp4"))
        self.assertFalse(is_safe_output_name("../final.mp4"))
        self.assertFalse(is_safe_output_name(".env"))


if __name__ == "__main__":
    unittest.main()