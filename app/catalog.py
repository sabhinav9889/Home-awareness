from dataclasses import dataclass
from typing import Literal

Reliability = Literal["good", "fair", "experimental"]


@dataclass(frozen=True)
class Sound:
    key: str
    label: str
    group: str
    default_enabled: bool
    reliability: Reliability
    default_repeat: int = 1
    default_window_s: int = 60
    default_cooldown_s: int = 120


SOUNDS: dict[str, Sound] = {
    s.key: s
    for s in [
        Sound("scream", "Scream", "Distress", True, "good"),
        Sound("shouting", "Shouting / yelling", "Distress", False, "fair"),
        Sound("crying", "Crying (baby or adult)", "Distress", False, "good"),
        Sound("moaning", "Moaning", "Distress", False, "experimental"),
        Sound("gasping", "Gasping / choking", "Distress", False, "experimental"),
        Sound("coughing", "Long coughing fit", "Distress", False, "fair"),
        Sound("glass_breaking", "Glass breaking", "Intrusion", True, "good"),
        Sound("door_slam", "Door slam / forced knock", "Intrusion", False, "fair"),
        Sound("knocking", "Repeated knocking / banging", "Intrusion", False, "fair"),
        Sound("car_alarm", "Car alarm", "Intrusion", False, "good"),
        Sound("dog_barking", "Dog barking", "Intrusion", False, "good", default_repeat=5),
        Sound("smoke_alarm", "Smoke / fire alarm", "Hazard", True, "good"),
        Sound("siren", "Siren", "Hazard", False, "good"),
        Sound("explosion", "Loud bang / explosion", "Hazard", False, "fair"),
        Sound("running_water", "Running water / tap left on", "Hazard", False, "fair"),
        Sound("thud_fall", "Loud thud / fall", "Accident", False, "experimental"),
        Sound("doorbell", "Doorbell", "Household", False, "good"),
    ]
}


def get_sound(key: str) -> Sound:
    return SOUNDS[key]
