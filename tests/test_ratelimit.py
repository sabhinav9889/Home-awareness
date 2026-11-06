import time

from app import ratelimit


def test_frozen_ratelimit_clock_freezes_only_the_limiter(frozen_ratelimit_clock):
    assert ratelimit.now() == frozen_ratelimit_clock
    assert time.time() != frozen_ratelimit_clock  # global time is untouched
