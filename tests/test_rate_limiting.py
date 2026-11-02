from rate_limiting.adaptive_controller import retry_delay
def test_retry_delay_bounded():
    assert 0 <= retry_delay(10) <= 20.25
def test_retry_after():
    assert retry_delay(0,3)==3
