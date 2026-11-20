"""内置 Skill 集合。"""

from skill.builtin.vision_recognize import VisionRecognizeSkill
from skill.builtin.vision_grasp import VisionGraspSkill
from skill.builtin.tool_skills import (
    weather_spec,
    time_spec,
    date_spec,
    find_object_spec,
    describe_scene_spec,
)

__all__ = [
    "VisionRecognizeSkill",
    "VisionGraspSkill",
    "weather_spec",
    "time_spec",
    "date_spec",
    "find_object_spec",
    "describe_scene_spec",
]
