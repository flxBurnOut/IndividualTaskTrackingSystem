"""Preserve public policy resources required by the selected Python runtime."""
from pathlib import Path
import ssl


SSL_POLICY_NAME = 'codex-rejected-cas.pem'
SSL_POLICY_MEMBER = '_internal/' + SSL_POLICY_NAME


def ssl_policy_file():
    """Unmodified Python needs no policy file; patched Python must keep it."""
    source = Path(ssl.__file__)
    if SSL_POLICY_NAME.encode('ascii') not in source.read_bytes():
        return None
    policy = source.with_name(SSL_POLICY_NAME)
    if policy.is_symlink() or not policy.is_file():
        raise ValueError('The selected Python runtime is missing its public SSL policy resource')
    content = policy.read_bytes()
    if (len(content) > 64 * 1024 or b'PRIVATE KEY' in content
            or b'-----BEGIN TRUSTED CERTIFICATE-----' not in content):
        raise ValueError('The Python SSL policy must contain public trusted-certificate data only')
    return policy


def runtime_data_files():
    policy = ssl_policy_file()
    return [(str(policy), '.')] if policy is not None else []
