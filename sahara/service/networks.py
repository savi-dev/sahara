# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo.config import cfg
import six

from sahara import conductor as c
from sahara import context
from sahara.openstack.common import log as logging
from sahara.utils.openstack import neutron
from sahara.utils.openstack import nova

LOG = logging.getLogger(__name__)

conductor = c.API
CONF = cfg.CONF


def init_instances_ips(instance):
    """Extracts internal and management ips.

    As internal ip will be used the first ip from the nova networks CIDRs.
    If use_floating_ip flag is set than management ip will be the first
    non-internal ip.
    """

    server = nova.get_instance_info(instance)

    management_ip = None
    internal_ip = None

    for network_label, addresses in six.iteritems(server.addresses):
        for address in addresses:
            if 'OS-EXT-IPS:type' in address:
                if address['OS-EXT-IPS:type'] == 'fixed':
                    internal_ip = internal_ip or address['addr']
                else:
                    management_ip = management_ip or address['addr']
            else:
                internal_ip = internal_ip or address['addr']
                management_ip = management_ip or address['addr']


    if not CONF.use_floating_ips:
        management_ip = internal_ip

    # NOTE(aignatov): Once bug #1262529 is fixed this 'if' block should be
    # reviewed and reformatted again, probably removed completely.
    if CONF.use_neutron and not (management_ip and internal_ip):
        LOG.debug("Instance %s doesn't contain yet Floating IP or Internal IP."
                  " Floating IP=%s, Internal IP=%s. Trying to get via Neutron."
                  % (server.name, management_ip, internal_ip))
        neutron_client = neutron.client()
        ports = neutron_client.list_ports(device_id=server.id)["ports"]
        if ports:
            target_port_id = ports[0]['id']
            fl_ips = neutron_client.list_floatingips(
                port_id=target_port_id)['floatingips']
            if fl_ips:
                fl_ip = fl_ips[0]
                if not internal_ip:
                    internal_ip = fl_ip['fixed_ip_address']
                    LOG.debug('Found fixed IP %s for %s' % (internal_ip,
                                                            server.name))
                # Zeroing management_ip if Sahara in private network
                if not CONF.use_floating_ips:
                    management_ip = internal_ip
                elif not management_ip:
                    management_ip = fl_ip['floating_ip_address']
                    LOG.debug('Found floating IP %s for %s' % (management_ip,
                                                               server.name))

    conductor.instance_update(context.ctx(), instance,
                              {"management_ip": management_ip,
                               "internal_ip": internal_ip})

    return internal_ip and management_ip


def assign_floating_ip(instance_id, pool):
    ip = nova.client().floating_ips.create(pool)
    nova.client().servers.get(instance_id).add_floating_ip(ip)


def delete_floating_ip(instance_id):
    fl_ips = nova.client().floating_ips.findall(instance_id=instance_id)
    for fl_ip in fl_ips:
        nova.client().floating_ips.delete(fl_ip.id)

def list_networks(**kwargs):
    return neutron.client().list_networks(**kwargs)['networks']
