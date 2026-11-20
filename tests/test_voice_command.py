"""음성이나 모터를 구동하지 않고 오인식 처리를 검사한다."""

import unittest

from robot_vision.command_parser import normalize_transcript


class VoiceTextTests(unittest.TestCase):
    def test_known_caption_does_not_hide_valid_command(self):
        self.assertEqual(normalize_transcript("[구독] 사람을 따라가."), "사람을 따라가.")

    def test_caption_only_is_empty(self):
        self.assertEqual(normalize_transcript("[구독]"), "")

    def test_misheard_words_are_not_rewritten(self):
        self.assertEqual(normalize_transcript("사랑할까?"), "사랑할까?")

    def test_unknown_bracket_content_is_preserved(self):
        self.assertEqual(normalize_transcript("[빨간색] 사람을 따라가"), "[빨간색] 사람을 따라가")


if __name__ == "__main__":
    unittest.main()
