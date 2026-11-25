import uuid
from contextlib import contextmanager

from netbox.context_managers import event_tracking
from netbox.jobs import JobRunner, system_job
from netbox.plugins import get_plugin_config
from utilities.request import NetBoxFakeRequest

from .client import ProxmoxClient, ProxmoxError
from .models import ProxmoxConnection, ProxmoxObject
from .sync import Sync, discover, write_objects

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
    """mode='fetch': only fetch from Proxmox (sync overview shows new objects and pending changes).
    mode='apply': write the fetched data of object_ids (as the requesting user, permissions checked).
    No mode (daily system job): fetch all enabled connections, update linked objects with pending changes,
    set objects gone from Proxmox offline. New objects are never created automatically."""
    class Meta:
        name = 'Proxmox sync'

    def run(self, *args, mode=None, connection_id=None, object_ids=None, **kwargs):
        if mode == 'apply':
            links = list(ProxmoxObject.objects.filter(pk__in=object_ids or []).select_related('connection'))
            with changelog_context(self.job.user):
                counts = write_objects(links, self.logger, user=self.job.user)
            self.logger.info(', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'nothing to do')
            return
        connections = ProxmoxConnection.objects.filter(pk=connection_id) if connection_id else \
            ProxmoxConnection.objects.filter(enabled=True)
        failed = []
        for conn in connections:
            try:
                counts = discover(conn, ProxmoxClient.from_connection(conn), self.logger)
            except ProxmoxError as exc:
                failed.append(conn.name)
                self.logger.error(f'{conn}: {exc}')
                continue
            self.logger.info(f'{conn} fetched: ' + ', '.join(f'{k} {v}' for k, v in sorted(counts.items())))
            if mode == 'fetch':
                continue
            linked = conn.proxmox_objects.filter(state='pending', ignored=False)
            with changelog_context(None):  # system run: service user, no permission checks
                sync = Sync(conn, self.logger)
                counts = sync.write(list(linked))
                sync.offline_stale()
            self.logger.info(f'{conn} updated: ' + ', '.join(f'{k} {v}' for k, v in sorted(sync.counts.items())))
        if failed:  # mark the job as failed so it shows up, after the other connections ran
            raise ProxmoxError('Sync failed for: ' + ', '.join(failed))
