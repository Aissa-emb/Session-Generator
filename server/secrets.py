"""Write-only secret storage: Keychain locally; mounted files or environment in containers."""
import os
import re
import sys
from xcorgen.security import SecurityError, read_token_file, validate_headers
from xcorgen.secrets import resolve

NAMES = {'chronosphere', 'embrace_api', 'symbol_upload', 'anthropic'}


def write(name, value):
    if not re.fullmatch(r'[a-z0-9_-]{1,80}', name) or not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise ValueError('Unknown secret name or invalid secret length.')
    validate_headers({'Authorization': value})
    if sys.platform == 'darwin' and not os.environ.get('SG_SECRET_DIR'):
        import keyring
        try:
            keyring.set_password('Session-Generator', name, value)
        except keyring.errors.KeyringError:
            raise SecurityError('The credential could not be saved to Keychain.') from None
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
    return ref


def suffix(value):
    # Never disclose an entire short credential.
    return value[-4:] if len(value) > 4 else None


def save(store, name, value):
    if name not in NAMES:
        raise ValueError('Unknown secret name.')
    ref = write(name, value)
    store.put('settings', {'id': 'secret:' + name, 'ref': ref, 'last_four': suffix(value)})
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
    return read(ref)


def read(ref):
    if sys.platform == 'darwin' and ref.startswith('keychain:'):
        # Read with the same native API used by save(). The security CLI has a
        # different Keychain access identity and can block on a desktop prompt.
        import keyring
        service, _, account = ref.removeprefix('keychain:').partition('/')
        try:
            value = keyring.get_password(service, account)
        except keyring.errors.KeyringError:
            raise SecurityError('The saved Keychain credential could not be read.') from None
        if not value:
            raise SecurityError('The saved Keychain credential was not found.')
        return value
    return resolve(ref)


def remove(ref):
    if ref.startswith('keychain:'):
        import keyring
        service, _, account = ref.removeprefix('keychain:').partition('/')
        try:
            if keyring.get_password(service, account) is not None:
                keyring.delete_password(service, account)
        except keyring.errors.KeyringError:
            raise SecurityError('The credential could not be removed from Keychain.') from None
    elif ref.startswith('file:'):
        try:
            os.unlink(ref.removeprefix('file:'))
        except FileNotFoundError:
            pass


def for_spec(store, spec, name):
    if 'saved_credentials' in spec:
        id = spec['saved_credentials'].get(name)
        if not id:
            return None  # Never borrow another destination's global credential.
        try:
            row = store.get('settings', 'credential:' + id)
        except KeyError:
            raise SecurityError('The saved destination credential was removed. Select and save a destination again.') from None
        if row['kind'] != name:
            raise SecurityError('The saved credential has the wrong type.')
        return read(row['ref'])
    value = get(store, name)
    if not value and name == 'chronosphere':
        value = resolve((spec.get('chronosphere') or {}).get('token_ref'))
    return value


def masks(store):
    rows = {r['id']: r for r in store.list('settings')}
    return {name: None if os.environ.get('SG_' + name.upper() + '_TOKEN') or os.environ.get('SG_' + name.upper() + '_TOKEN_FILE')
            else rows.get('secret:' + name, {}).get('last_four') for name in sorted(NAMES)}


def status(store):
    return {name: bool(os.environ.get('SG_' + name.upper() + '_TOKEN') or
                      os.environ.get('SG_' + name.upper() + '_TOKEN_FILE') or
                      any(r['id'] == 'secret:' + name for r in store.list('settings'))) for name in sorted(NAMES)}
