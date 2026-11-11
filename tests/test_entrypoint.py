import logging

import main
from birthdaybot.config import Config


def test_runtime_value_error_records_fatal_traceback(monkeypatch, caplog):
    monkeypatch.setattr(main.Config, "load", lambda: Config("test"))
    monkeypatch.setattr(main, "configure", lambda *args: None)

    async def fail(config):
        raise ValueError("runtime failure")

    monkeypatch.setattr(main, "run", fail)
    with caplog.at_level(logging.ERROR):
        assert main.main() == 1
    record = next(record for record in caplog.records if record.message == "bot_fatal_exit")
    assert record.exc_info[0] is ValueError


def test_invalid_configuration_stops_before_connecting(monkeypatch, capsys):
    def invalid():
        raise ValueError("Missing bot token")

    monkeypatch.setattr(main.Config, "load", invalid)
    assert main.main() == 2
    assert "Configuration error: Missing bot token" in capsys.readouterr().err
