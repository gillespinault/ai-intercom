import pytest
from src.hub.telegram_bot import format_agent_message, parse_start_command


def test_format_agent_message():
    text = format_agent_message(
        from_agent="serverlab/infra",
        message="Add reverse proxy for newapp",
    )
    assert "serverlab/infra" in text
    assert "Add reverse proxy" in text


def test_format_agent_message_human():
    text = format_agent_message(
        from_agent="human",
        message="Use port 3457",
    )
    assert "human" not in text or "Gilles" in text  # human messages formatted differently


def test_parse_start_command_full():
    machine, project, mission = parse_start_command('vps/nginx "Check SSL certs"')
    assert machine == "vps"
    assert project == "nginx"
    assert mission == "Check SSL certs"


def test_parse_start_command_just_target():
    machine, project, mission = parse_start_command("vps/nginx")
    assert machine == "vps"
    assert project == "nginx"
    assert mission == ""


def test_parse_start_command_invalid():
    with pytest.raises(ValueError):
        parse_start_command("")
