import pytest

from ha_edge import cli

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


@pytest.mark.parametrize("entry", ["sensor_main", "agent_main"])
def test_help_contains_disclaimer(entry, capsys):
    with pytest.raises(SystemExit) as e:
        getattr(cli, entry)(["--help"])
    assert e.value.code == 0
    assert DISCLAIMER in capsys.readouterr().out
