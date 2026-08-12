from unittest.mock import Mock

import pytest

from swg_te_monitor import ui


def test_required_text_reprompts_for_empty_answer(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.side_effect = ["", "   ", "value"]
    monkeypatch.setattr(ui.questionary, "text", Mock(return_value=prompt))
    monkeypatch.setattr(ui.questionary, "print", Mock())

    assert ui._required_text("Required:") == "value"
    assert prompt.ask.call_count == 3


def test_required_hidden_reprompts_for_empty_answer(monkeypatch) -> None:
    getpass = Mock(side_effect=["", "   ", "secret-value"])
    monkeypatch.setattr(ui, "getpass", getpass)
    monkeypatch.setattr(ui.questionary, "print", Mock())

    assert ui._required_hidden("Secret").get_secret_value() == "secret-value"
    assert getpass.call_count == 3


def test_optional_ssh_ip_blank_disables_ssh(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.return_value = ""
    monkeypatch.setattr(ui.questionary, "text", Mock(return_value=prompt))

    assert ui._optional_ssh_ip() is None


def test_optional_ssh_ip_creates_single_host_cidr(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.return_value = "192.0.2.10"
    text = Mock(return_value=prompt)
    monkeypatch.setattr(ui.questionary, "text", text)

    assert str(ui._optional_ssh_ip()) == "192.0.2.10/32"
    assert text.call_args.args[0] == "Authorized SSH IP (blank for SSH disabled):"


def test_optional_ssh_ip_reprompts_for_invalid_input(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.side_effect = ["192.0.2.10/24", "not-an-ip", "192.0.2.10"]
    monkeypatch.setattr(ui.questionary, "text", Mock(return_value=prompt))
    monkeypatch.setattr(ui.questionary, "print", Mock())

    assert str(ui._optional_ssh_ip()) == "192.0.2.10/32"
    assert prompt.ask.call_count == 3


def test_pac_url_is_visible_and_reprompts_until_https(monkeypatch) -> None:
    required_text = Mock(
        side_effect=[
            "http://proxy.example/proxy.pac",
            "not a url",
            "https://proxy.example/proxy.pac",
        ]
    )
    monkeypatch.setattr(ui, "_required_text", required_text)
    monkeypatch.setattr(ui.questionary, "print", Mock())

    result = ui._required_https_url("Cisco Secure Access PAC URL:")

    assert result.get_secret_value() == "https://proxy.example/proxy.pac"
    assert required_text.call_count == 3


def test_required_locations_reprompts_for_empty_selection(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.side_effect = [[], ["dallas"]]
    monkeypatch.setattr(ui.questionary, "checkbox", Mock(return_value=prompt))
    monkeypatch.setattr(ui.questionary, "print", Mock())

    assert ui._required_locations([]) == ["dallas"]
    assert prompt.ask.call_count == 2


def test_deployment_mode_returns_selected_value(monkeypatch) -> None:
    prompt = Mock()
    prompt.ask.return_value = ui.GENERATE_TEMPLATE
    monkeypatch.setattr(ui.questionary, "select", Mock(return_value=prompt))

    assert ui.deployment_mode() == ui.GENERATE_TEMPLATE


def test_aws_profile_offers_configured_sso_profiles(monkeypatch) -> None:
    session = Mock(available_profiles=["company-sso"])
    monkeypatch.setattr(ui.boto3, "Session", Mock(return_value=session))
    prompt = Mock()
    prompt.ask.return_value = "company-sso"
    select = Mock(return_value=prompt)
    monkeypatch.setattr(ui.questionary, "select", select)

    assert ui.aws_profile() == "company-sso"
    labels = [choice.title for choice in select.call_args.kwargs["choices"]]
    assert "AWS profile: company-sso" in labels


def test_ctrl_c_aborts_instead_of_reprompting() -> None:
    prompt = Mock()
    prompt.ask.return_value = None

    with pytest.raises(KeyboardInterrupt):
        ui._ask(prompt)

    assert prompt.ask.call_count == 1
