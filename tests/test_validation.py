import pytest
from models import DeploymentOptions
from validation import validate_options


@pytest.mark.parametrize("cidr", ["bad", "10.0.0.999/24", "10.0.0.0/99"])
def test_invalid_cidr(cidr: str) -> None:
    with pytest.raises(ValueError):
        validate_options(DeploymentOptions(allowed_ips=(cidr,)))


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        validate_options(DeploymentOptions(wireguard_port=port))


def test_ipv6_requires_opt_in() -> None:
    with pytest.raises(ValueError, match="IPv6"):
        validate_options(DeploymentOptions(allowed_ips=("::/0",)))
