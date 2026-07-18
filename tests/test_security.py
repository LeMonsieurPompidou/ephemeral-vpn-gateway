from security import generate_wireguard_keypair, redact


def test_wireguard_keypair_is_base64_and_distinct() -> None:
    private, public = generate_wireguard_keypair()
    assert len(private) == 44 and len(public) == 44 and private != public


def test_redaction_removes_tokens_and_private_keys() -> None:
    value = redact("token=abc123\nPrivateKey = secret-value\nAWS AKIA1234567890123456")
    assert "abc123" not in value
    assert "secret-value" not in value
    assert "AKIA1234567890123456" not in value
