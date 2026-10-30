"""Write-only secret storage: Keychain locally; mounted files or environment in containers."""
import os
import re
import sys
from xcorgen.security import SecurityError, read_token_file, validate_headers
from xcorgen.secrets import resolve

NAMES = {'chronosphere', 'embrace_api', 'symbol_upload', 'anthropic'}


def save(store, name, value):
    if name not in NAMES or not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise ValueError('Unknown secret name or invalid secret length.')
    validate_headers({'Authorization': value})
    if sys.platform == 'darwin':
        import keyring
        keyring.set_password('Session-Generator', name, value)
        ref = 'keychain:Session-Generator/' + name
    else:
        directory = os.environ.get('SG_SECRET_DIR')
        if not directory:
            raise ValueError('Set SG_SECRET_DIR to a writable private mounted directory, or provide secrets through environment variables.')
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.abspath(os.path.join(directory, name + '.token'))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as f:
                fd = None
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        ref = 'file:' + path
    store.put('settings', {'id': 'secret:' + name, 'ref': ref})
    return {'name': name, 'configured': True}


def get(store, name):
    env = 'SG_' + name.upper() + '_TOKEN'
    if os.environ.get(env):
        return os.environ[env]
    if os.environ.get(env + '_FILE'):
        return read_token_file(os.environ[env + '_FILE'])
    try:
        ref = store.get('settings', 'secret:' + name)['ref']
    except KeyError:
        return None
    return resolve(ref)


def status(store):
    return {name: bool(os.environ.get('SG_' + name.upper() + '_TOKEN') or
                      os.environ.get('SG_' + name.upper() + '_TOKEN_FILE') or
                      any(r['id'] == 'secret:' + name for r in store.list('settings'))) for name in sorted(NAMES)}
