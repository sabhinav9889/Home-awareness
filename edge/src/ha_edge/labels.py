"""Mapping between server catalog keys and YAMNet (AudioSet) class display names."""

from collections.abc import Sequence

YAMNET_LABELS: dict[str, tuple[str, ...]] = {
    "scream": ("Screaming",),
    "shouting": ("Shout", "Yell", "Bellow"),
    "crying": ("Crying, sobbing", "Baby cry, infant cry", "Whimper"),
    "moaning": ("Groan", "Grunt"),
    "gasping": ("Gasp", "Wheeze", "Breathing"),
    "coughing": ("Cough",),
    "glass_breaking": ("Glass", "Shatter", "Breaking"),
    "door_slam": ("Slam", "Door"),
    "knocking": ("Knock",),
    "car_alarm": ("Car alarm",),
    "dog_barking": ("Bark", "Dog", "Bow-wow"),
    "smoke_alarm": ("Smoke detector, smoke alarm", "Fire alarm"),
    "siren": (
        "Siren",
        "Emergency vehicle",
        "Police car (siren)",
        "Ambulance (siren)",
        "Fire engine, fire truck (siren)",
        "Civil defense siren",
    ),
    "explosion": ("Explosion", "Bang"),
    "running_water": (
        "Water tap, faucet",
        "Sink (filling or washing)",
        "Bathtub (filling or washing)",
    ),
    "thud_fall": ("Thump, thud",),
    "doorbell": ("Doorbell", "Ding-dong"),
}

CATALOG_KEYS: frozenset[str] = frozenset(YAMNET_LABELS)


def build_index(class_names: Sequence[str]) -> dict[str, list[int]]:
    """Map each catalog key to the model class indices of its labels."""
    position = {name: i for i, name in enumerate(class_names)}
    missing = [
        label for labels in YAMNET_LABELS.values() for label in labels if label not in position
    ]
    if missing:
        raise ValueError(f"labels missing from model class map: {missing}")
    return {key: [position[label] for label in labels] for key, labels in YAMNET_LABELS.items()}
