"""현재 프레임의 최대 박스 선택. ID/외형/과거 위치에 의존하지 않는다."""

import unittest

from robot_vision.tracking_controller import TargetSelector


def box(b, label="person", confidence=0.9, **extra):
    return {"bbox": b, "label": label, "confidence": confidence, **extra}


class LargestBoxTests(unittest.TestCase):
    def test_area_wins_over_center_confidence_and_old_target(self):
        s = TargetSelector()
        small = box([140, 90, 160, 110], confidence=0.99)
        large = box([0, 0, 100, 180], confidence=0.5)
        s.select([small], "person", 300, 200, 0)
        self.assertEqual(s.select([small, large], "person", 300, 200, 0.1)["bbox"], large["bbox"])

    def test_ids_and_appearance_do_not_affect_selection(self):
        s = TargetSelector()
        item = box([0, 0, 100, 180], track_id=None, track_confirmed=False, appearance="ignored")
        self.assertEqual(s.select([item], "person", 300, 200, 0)["bbox"], item["bbox"])
        item["track_id"] = "entirely-new-id"
        self.assertIsNotNone(s.select([item], "person", 300, 200, 0.1))

    def test_only_requested_class_and_confident_detections_are_considered(self):
        s = TargetSelector()
        valid = box([10, 10, 30, 30])
        other = box([0, 0, 300, 200], label="cup")
        weak = box([0, 0, 300, 200], confidence=0.2)
        self.assertEqual(
            s.select([other, weak, valid], "person", 300, 200, 0)["bbox"], valid["bbox"]
        )

    def test_lost_target_can_reappear_anywhere_without_waiting_for_id(self):
        s = TargetSelector()
        s.select([box([0, 0, 30, 30])], "person", 300, 200, 0)
        self.assertIsNone(s.select([], "person", 300, 200, 100))
        new = box([250, 150, 300, 200])
        self.assertEqual(s.select([new], "person", 300, 200, 101)["bbox"], new["bbox"])


if __name__ == "__main__":
    unittest.main()
