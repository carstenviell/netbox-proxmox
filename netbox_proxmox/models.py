import re

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from netbox.models import NetBoxModel
from utilities.querysets import RestrictedQuerySet

ENV_PREFIX = 'PROXMOX_'


def https_only(value):
    """The API token must never travel unencrypted."""
    if value and not value.lower().startswith('https://'):
        raise ValidationError(_('Only https:// URLs are allowed.'))


def env_name(value):
    """Only PROXMOX_* variables: otherwise the secret field could point to any process secret
    (SECRET_KEY, DB password) and send it to the configured server."""
    if not re.fullmatch(rf'{ENV_PREFIX}[A-Z0-9_]+', value or ''):
        raise ValidationError(_('The variable name must start with %(p)s (A-Z, 0-9, _).') % {'p': ENV_PREFIX})


def token_id(value):
    if not re.fullmatch(r'[^\s@!=]+@[^\s@!=]+![A-Za-z0-9_.-]+', value or ''):
        raise ValidationError(_('Format: user@realm!tokenname'))


class ProxmoxConnection(NetBoxModel):
    """One Proxmox VE cluster (or standalone node). The token secret is never stored - only the name
    of the environment variable holding it."""
    name = models.CharField(verbose_name=_('name'), max_length=100, unique=True)
    url = models.URLField(verbose_name=_('API URL'), validators=[https_only],
                          help_text=_('e.g. https://pve01.example.local:8006'))
    token_id = models.CharField(verbose_name=_('API token ID'), max_length=150, validators=[token_id],
                                help_text=_('user@realm!tokenname'))
    token_secret_env = models.CharField(verbose_name=_('token secret environment variable'), max_length=100,
                                        default='PROXMOX_TOKEN_SECRET', validators=[env_name],
                                        help_text=_('Must start with PROXMOX_'))
    verify_tls = models.BooleanField(verbose_name=_('verify TLS'), default=True)
    ca_bundle = models.CharField(verbose_name=_('CA bundle path'), max_length=300, blank=True)
    timeout = models.PositiveIntegerField(verbose_name=_('timeout (s)'), default=30)
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True,
                                  help_text=_('Included in the scheduled sync'))

    # Defaults for objects the sync has to create (Proxmox knows nothing about sites, roles, hardware types)
    site = models.ForeignKey(to='dcim.Site', on_delete=models.PROTECT, related_name='+', verbose_name=_('site'),
                             help_text=_('Site of the cluster and of new nodes'))
    node_role = models.ForeignKey(to='dcim.DeviceRole', on_delete=models.PROTECT, related_name='+',
                                  verbose_name=_('node role'))
    node_device_type = models.ForeignKey(to='dcim.DeviceType', on_delete=models.PROTECT, related_name='+',
                                         verbose_name=_('node device type'))
    vm_role = models.ForeignKey(to='dcim.DeviceRole', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+', verbose_name=_('VM role'), help_text=_('Only set on creation'))
    container_role = models.ForeignKey(to='dcim.DeviceRole', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='+', verbose_name=_('container role'),
                                       help_text=_('Only set on creation'))
    tenant = models.ForeignKey(to='tenancy.Tenant', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='+', verbose_name=_('tenant'), help_text=_('Only set on creation'))
    cluster = models.OneToOneField(to='virtualization.Cluster', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='proxmox_connection', verbose_name=_('cluster'),
                                   help_text=_('Set by the first sync (found by name or created)'))
    description = models.CharField(verbose_name=_('description'), max_length=200, blank=True)
    comments = models.TextField(verbose_name=_('comments'), blank=True)
    last_sync = models.DateTimeField(verbose_name=_('last sync'), null=True, blank=True, editable=False)

    class Meta:
        ordering = ('name',)
        verbose_name = _('Proxmox connection')
        verbose_name_plural = _('Proxmox connections')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_proxmox:proxmoxconnection', args=[self.pk])


class ProxmoxObject(models.Model):
    """Link between a Proxmox node/VM/container and its NetBox object, plus the last measured usage.
    Key is (connection, kind, proxmox_id): VMIDs are only unique within one Proxmox cluster."""
    KIND_CHOICES = [('cluster', _('Cluster')), ('node', _('Node')), ('qemu', _('VM (QEMU)')), ('lxc', _('Container (LXC)'))]
    STATE_CHOICES = [
        ('ok', _('In sync')),
        ('created', _('Created')),
        ('ambiguous', _('Ambiguous')),
        ('error', _('Error')),
        ('stale', _('Gone from Proxmox')),
    ]

    connection = models.ForeignKey(to=ProxmoxConnection, on_delete=models.CASCADE, related_name='proxmox_objects',
                                   verbose_name=_('connection'))
    kind = models.CharField(verbose_name=_('type'), max_length=7, choices=KIND_CHOICES)
    proxmox_id = models.CharField(verbose_name=_('Proxmox ID'), max_length=100,
                                  help_text=_('VMID, node name or cluster name'))
    name = models.CharField(verbose_name=_('name in Proxmox'), max_length=255, blank=True)
    node = models.CharField(verbose_name=_('node'), max_length=100, blank=True)
    device = models.OneToOneField(to='dcim.Device', null=True, blank=True, on_delete=models.CASCADE,
                                  related_name='proxmox_object')
    virtual_machine = models.OneToOneField(to='virtualization.VirtualMachine', null=True, blank=True,
                                           on_delete=models.CASCADE, related_name='proxmox_object')
    state = models.CharField(verbose_name=_('state'), max_length=10, choices=STATE_CHOICES)
    message = models.CharField(verbose_name=_('message'), max_length=500, blank=True)
    last_seen = models.DateTimeField(verbose_name=_('last seen'), null=True, blank=True)

    # Usage snapshot from the last sync (bytes / percent / seconds)
    cpu_usage = models.FloatField(verbose_name=_('CPU usage (%)'), null=True, blank=True)
    cpu_count = models.PositiveIntegerField(verbose_name=_('CPUs'), null=True, blank=True)
    mem_used = models.PositiveBigIntegerField(verbose_name=_('memory used'), null=True, blank=True)
    mem_total = models.PositiveBigIntegerField(verbose_name=_('memory total'), null=True, blank=True)
    disk_used = models.PositiveBigIntegerField(verbose_name=_('disk used'), null=True, blank=True)
    disk_total = models.PositiveBigIntegerField(verbose_name=_('disk total'), null=True, blank=True)
    uptime = models.PositiveBigIntegerField(verbose_name=_('uptime (s)'), null=True, blank=True)

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('connection', 'kind', 'name')
        verbose_name = _('Proxmox object')
        verbose_name_plural = _('Proxmox objects')
        constraints = [
            models.UniqueConstraint(fields=('connection', 'kind', 'proxmox_id'),
                                    name='netbox_proxmox_proxmoxobject_unique_key'),
        ]

    def __str__(self):
        return f'{self.connection} {self.kind}/{self.proxmox_id}'

    @property
    def target(self):
        return self.device or self.virtual_machine
