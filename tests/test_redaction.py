from deploy_cli.redaction import Redactor, secrets_from_env


def test_env_values_are_redacted_without_exposing_keys() -> None:
    values = secrets_from_env("USER=service\nPASSWORD='very-secret'\n# ignored\n")
    redacted = Redactor(values)("service authenticated with very-secret")

    assert redacted == "[REDACTED] authenticated with [REDACTED]"
    assert "very-secret" not in redacted


def test_blank_env_values_are_not_redaction_patterns() -> None:
    assert secrets_from_env("EMPTY=\n") == set()


def test_dotenv_quotes_escapes_and_inline_comments_are_parsed() -> None:
    values = secrets_from_env(
        'PLAIN=value # comment\nQUOTED="a # value\\nline" # comment\n'
        "SINGLE='literal # value'\nexport EXPORTED=token\n"
    )

    assert values == {"value", "a # value\nline", "literal # value", "token"}


def test_multiline_secret_is_redacted_from_line_stream() -> None:
    redactor = Redactor({"first-secret-line\nsecond-secret-line"})

    output = "".join(redactor(line) for line in ["first-secret-line\n", "second-secret-line\n"])

    assert output == "[REDACTED]\n[REDACTED]\n"
