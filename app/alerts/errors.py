class DeliveryError(Exception):
    """A channel send failed; the message is safe to store (no secrets, no URLs)."""
