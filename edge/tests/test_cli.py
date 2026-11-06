import pytest

from ha_edge import cli

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


@pytest.mark.parametrize("entry", ["sensor_main", "agent_main"])
def test_help_contains_disclaimer(entry, capsys):
    with pytest.raises(SystemExit) as e:
        getattr(cli, entry)(["--help"])
    assert e.value.code == 0
    assert DISCLAIMER in capsys.readouterr().out


def test_fetch_model_prints_path(monkeypatch, capsys, tmp_path):
    from ha_edge import model

    target = tmp_path / "yamnet.tflite"
    monkeypatch.setattr(model, "fetch_model", lambda d, **kw: target)
    assert cli.sensor_main(["fetch-model"]) == 0
    assert capsys.readouterr().out.strip() == str(target)
