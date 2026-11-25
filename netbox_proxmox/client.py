"""
Minimal read-only client for the Proxmox VE API (/api2/json), API token auth.
The plugin never writes to Proxmox.
"""
import os
from urllib.parse import quote

import requests
from django.utils.translation import gettext as _


class ProxmoxError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ProxmoxClient:
    def __init__(self, url, token_id, secret, verify=True, timeout=30):
        self.base = url.rstrip('/') + '/api2/json'
        self.headers = {'Authorization': f'PVEAPIToken={token_id}={secret}', 'Accept': 'application/json'}
        self.verify = verify
        self.timeout = timeout

    @classmethod
    def from_connection(cls, conn):
        from django.core.exceptions import ValidationError
        from .models import env_name, https_only, token_id
        try:  # re-check: the values could have been changed directly in the database
            https_only(conn.url)
            env_name(conn.token_secret_env)
            token_id(conn.token_id)
        except ValidationError as exc:
            raise ProxmoxError(' '.join(exc.messages))
        secret = os.environ.get(conn.token_secret_env)
        if not secret:
            raise ProxmoxError(_('Environment variable %(name)s is not set.') % {'name': conn.token_secret_env})
        return cls(conn.url, conn.token_id, secret, verify=conn.ca_bundle or conn.verify_tls, timeout=conn.timeout)

    def get(self, *parts):
        """GET /<part>/<part>/... (each part is URL-quoted). Returns the 'data' member. Retried once."""
        url = self.base + ''.join('/' + quote(str(p), safe='') for p in parts)
        for attempt in range(2):
            try:
                # no redirects: the Proxmox API never redirects, and a redirect could point the client at internal hosts
                resp = requests.get(url, headers=self.headers, verify=self.verify, timeout=self.timeout,
                                    allow_redirects=False)
                break
            except requests.RequestException as exc:
                if attempt:
                    raise ProxmoxError(str(exc))
        if resp.is_redirect:
            raise ProxmoxError(_('Redirect to %(location)s refused.') % {'location': resp.headers.get('Location', '?')},
                               status=resp.status_code)
        if resp.status_code >= 400:
            # Proxmox puts the reason into the status line, e.g. "500 QEMU guest agent is not running"
            raise ProxmoxError(f'HTTP {resp.status_code}: {resp.reason or resp.text[:200]}', status=resp.status_code)
        try:
            return resp.json().get('data')
        except ValueError:
            raise ProxmoxError(_('Response is not JSON (HTTP %(code)s)') % {'code': resp.status_code})

    def version(self):
        return self.get('version') or {}
