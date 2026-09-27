import pytest

from gpt_tool.parser import Credentials, ParseLineError, parse_line


def test_parse_line_strips_whitespace_around_pipe_delimited_fields() -> None:
    creds = parse_line("  USER@example.com | pa$$ word | JBSWY3DPEHPK3PXP  ")

    assert creds == Credentials(
        email="user@example.com",
        password="pa$$ word",
        totp_secret="JBSWY3DPEHPK3PXP",
    )


def test_parse_line_rejects_password_that_is_only_separator_whitespace() -> None:
    with pytest.raises(ParseLineError, match="empty password"):
        parse_line("user@example.com |   | JBSWY3DPEHPK3PXP")
