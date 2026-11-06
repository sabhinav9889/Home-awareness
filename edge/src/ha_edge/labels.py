"""Mapping between server catalog keys and YAMNet (AudioSet) class display names."""

from collections.abc import Sequence

# Generic classes (Door, Dog, Breathing, Breaking, Bang, Grunt) are deliberately omitted:
# they fire constantly in ordinary homes and would flood users with false alarms.
YAMNET_LABELS: dict[str, tuple[str, ...]] = {
    "scream": ("Screaming",),
    "shouting": ("Shout", "Yell", "Bellow"),
    "crying": ("Crying, sobbing", "Baby cry, infant cry", "Whimper"),
    # Grunt omitted: too common in everyday exertion.
    "moaning": ("Groan",),
    # Breathing omitted: fires on normal breathing.
    "gasping": ("Gasp", "Wheeze"),
    "coughing": ("Cough",),
    # Breaking omitted: matches any breaking object.
    "glass_breaking": ("Glass", "Shatter"),
    # Door omitted: matches every door sound.
    "door_slam": ("Slam",),
    "knocking": ("Knock",),
    "car_alarm": ("Car alarm",),
    # Dog omitted: matches panting and whining, not just barking.
    "dog_barking": ("Bark", "Bow-wow"),
    "smoke_alarm": ("Smoke detector, smoke alarm", "Fire alarm"),
    "siren": (
        "Siren",
        "Emergency vehicle",
        "Police car (siren)",
        "Ambulance (siren)",
        "Fire engine, fire truck (siren)",
        "Civil defense siren",
    ),
    # Bang omitted: matches routine knocks and pops.
    "explosion": ("Explosion",),
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
