from dataclasses import dataclass
from typing import Literal

Reliability = Literal["good", "fair", "experimental"]
Severity = Literal["urgent", "normal"]


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
    # Minimum detector confidence to report/alert. 0.30 is the detector's global default;
    # measured sounds use the "better threshold" from docs/accuracy.md.
    default_min_confidence: float = 0.30
    # "urgent" sounds push to ntfy with its highest priority (phone vibrates / breaks through
    # quiet mode per the user's ntfy app settings); the rest use "high".
    severity: Severity = "normal"


SOUNDS: dict[str, Sound] = {
    s.key: s
    for s in [
        Sound("scream", "Scream", "Distress", True, "good", severity="urgent"),
        Sound("shouting", "Shouting / yelling", "Distress", False, "fair"),
        Sound(
            "crying",
            "Crying (baby or adult)",
            "Distress",
            False,
            "good",
            default_min_confidence=0.50,
        ),
        Sound("moaning", "Moaning", "Distress", False, "experimental"),
        Sound("gasping", "Gasping / choking", "Distress", False, "experimental", severity="urgent"),
        Sound(
            "coughing", "Long coughing fit", "Distress", False, "fair", default_min_confidence=0.50
        ),
        Sound("glass_breaking", "Glass breaking", "Intrusion", True, "good", severity="urgent"),
        Sound("door_slam", "Door slam / forced knock", "Intrusion", False, "fair"),
        Sound("knocking", "Repeated knocking / banging", "Intrusion", False, "fair"),
        Sound("car_alarm", "Car alarm", "Intrusion", False, "good"),
        Sound(
            "dog_barking",
            "Dog barking",
            "Intrusion",
            False,
            "good",
            default_repeat=5,
            default_min_confidence=0.50,
        ),
        Sound("smoke_alarm", "Smoke / fire alarm", "Hazard", True, "good", severity="urgent"),
        Sound(
            "siren",
            "Siren",
            "Hazard",
            False,
            "good",
            default_min_confidence=0.50,
            severity="urgent",
        ),
        Sound("explosion", "Loud bang / explosion", "Hazard", False, "fair", severity="urgent"),
        Sound("running_water", "Running water / tap left on", "Hazard", False, "fair"),
        Sound(
            "thud_fall", "Loud thud / fall", "Accident", False, "experimental", severity="urgent"
        ),
        Sound("doorbell", "Doorbell", "Household", False, "good"),
    ]
}


def get_sound(key: str) -> Sound:
    return SOUNDS[key]
