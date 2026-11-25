import uuid
from contextlib import contextmanager

from netbox.context_managers import event_tracking
from netbox.jobs import JobRunner, system_job
from netbox.plugins import get_plugin_config
from utilities.request import NetBoxFakeRequest

from .client import ProxmoxClient, ProxmoxError
from .models import ProxmoxConnection
from .sync import sync_connection

SERVICE_USER = 'proxmox-sync'


def service_user():
    """Changelog entries need a user; scheduled runs have none. Inactive, without usable password."""
    from users.models import User
    user, created = User.objects.get_or_create(username=SERVICE_USER, defaults={'is_active': False})
    if created:
        user.set_unusable_password()
        user.save()
    return user


@contextmanager
def changelog_context(user):
    """Like extras.jobs.ScriptJob: a fake request so changes made by the sync appear in the NetBox changelog."""
    request = NetBoxFakeRequest({'META': {}, 'POST': {}, 'GET': {}, 'FILES': {}, 'user': user or service_user(),
                                 'path': '', 'id': uuid.uuid4()})
    with event_tracking(request):
        yield


@system_job(interval=get_plugin_config('netbox_proxmox', 'sync_interval_min'))
class ProxmoxSyncJob(JobRunner):
    """Import + update + usage snapshot. Without connection_id: all enabled connections."""
    class Meta:
        name = 'Proxmox sync'

    def run(self, *args, connection_id=None, **kwargs):
        if connection_id:
            connections = ProxmoxConnection.objects.filter(pk=connection_id)
        else:
            connections = ProxmoxConnection.objects.filter(enabled=True)
        failed = []
        with changelog_context(self.job.user):
            for conn in connections:
                try:
                    counts = sync_connection(conn, ProxmoxClient.from_connection(conn), self.logger)
                except ProxmoxError as exc:
                    failed.append(conn.name)
                    self.logger.error(f'{conn}: {exc}')
                    continue
                self.logger.info(f'{conn}: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
        if failed:  # mark the job as failed so it shows up, after the other connections ran
            raise ProxmoxError('Sync failed for: ' + ', '.join(failed))
