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

import json

from heatclient import client as heat_client
from oslo.config import cfg

from sahara import context
from sahara import exceptions as ex
from sahara.openstack.common import log as logging
from sahara.utils import files as f
from sahara.utils.openstack import base


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def client():
    ctx = context.current()
    heat_url = base.url_for(ctx.service_catalog, 'orchestration')
    return heat_client.Client('1', heat_url, token=ctx.token)


def _get_inst_name(cluster_name, ng_name, index):
    return '%s-%s-%03d' % (cluster_name, ng_name, (index + 1))


def _get_port_name(inst_name):
    return '%s-port' % inst_name


def _get_floating_name(inst_name):
    return '%s-floating' % inst_name


def _get_volume_name(inst_name, volume_idx):
    return '%s-volume-%i' % (inst_name, volume_idx)


def _get_volume_attach_name(inst_name, volume_idx):
    return '%s-volume-attachment-%i' % (inst_name, volume_idx)


def _get_anti_affinity_scheduler_hints(instances_names):
    if not instances_names:
        return ''

    aa_list = []
    for instances_name in set(instances_names):
        aa_list.append({"Ref": instances_name})
    return '"scheduler_hints" : %s,' % json.dumps({"different_host": aa_list})


def _load_template(template_name, fields):
    template_file = f.get_file_text('resources/%s' % template_name)
    return template_file.rstrip() % fields


def _prepare_userdata(userdata):
    """Converts userdata as a text into format consumable by heat template."""

    userdata = userdata.replace('"', '\\"')

    lines = userdata.splitlines()
    return '"' + '",\n"'.join(lines) + '"'


class ClusterTemplate(object):
    def __init__(self, cluster):
        self.cluster = cluster
        self.node_groups_extra = {}

    def add_node_group_extra(self, node_group_id, node_count,
                             gen_userdata_func):
        self.node_groups_extra[node_group_id] = {
            'node_count': node_count,
            'gen_userdata_func': gen_userdata_func
        }

    # Consider using a single Jinja template for all this
    def instantiate(self, update_existing):
        main_tmpl = _load_template('main.heat',
                                   {'resources': self._serialize_resources()})
        heat = client()

        kwargs = {
            'stack_name': self.cluster.name,
            'timeout_mins': 180,
            'disable_rollback': True,
            'parameters': {},
            'template': json.loads(main_tmpl)}

        if not update_existing:
            heat.stacks.create(**kwargs)
        else:
            for stack in heat.stacks.list():
                if stack.stack_name == self.cluster.name:
                    stack.update(**kwargs)
                    break

        for stack in heat.stacks.list():
            if stack.stack_name == self.cluster.name:
                return ClusterStack(self, stack)

        raise RuntimeError('Failed to find just created stack %s' %
                           self.cluster.name)

    def _serialize_resources(self):
        resources = []
        aa_groups = {}

        for ng in self.cluster.node_groups:
            for idx in range(0, self.node_groups_extra[ng.id]['node_count']):
                resources.extend(self._serialize_instance(ng, idx, aa_groups))

        return ',\n'.join(resources)

    def _serialize_instance(self, ng, idx, aa_groups):
        inst_name = _get_inst_name(self.cluster.name, ng.name, idx)

        # TODO(dmitryme): support floating IPs for nova-network without
        # auto-assignment

        nets = ''
        security_groups = ''
        if CONF.use_neutron:
            port_name = _get_port_name(inst_name)
            yield self._serialize_port(port_name,
                                       self.cluster.neutron_management_network,
                                       ng.security_groups)

            nets = '"networks" : [{ "port" : { "Ref" : "%s" }}],' % port_name

            if ng.floating_ip_pool:
                yield self._serialize_floating(inst_name, port_name,
                                               ng.floating_ip_pool)

            if ng.security_groups:
                security_groups = (
                    '"security_groups": %s,' % json.dumps(ng.security_groups))

        aa_names = []
        for node_process in ng.node_processes:
            aa_names += aa_groups.get(node_process) or []

        # Check if cluster contains user key-pair and include it to template.
        key_name = ''
        if self.cluster.user_keypair_id:
            key_name = '"key_name" : "%s",' % self.cluster.user_keypair_id

        gen_userdata_func = self.node_groups_extra[ng.id]['gen_userdata_func']
        userdata = gen_userdata_func(ng, inst_name)

        fields = {'instance_name': inst_name,
                  'flavor_id': ng.flavor_id,
                  'image_id': ng.get_image_id(),
                  'network_interfaces': nets,
                  'key_name': key_name,
                  'userdata': _prepare_userdata(userdata),
                  'scheduler_hints': _get_anti_affinity_scheduler_hints(
                      aa_names),
                  'security_groups': security_groups}

        for node_process in ng.node_processes:
            if node_process in self.cluster.anti_affinity:
                aa_group_names = aa_groups.get(node_process, [])
                aa_group_names.append(inst_name)
                aa_groups[node_process] = aa_group_names

        yield _load_template('instance.heat', fields)

        for idx in range(0, ng.volumes_per_node):
            yield self._serialize_volume(inst_name, idx, ng.volumes_size)

    def _serialize_port(self, port_name, fixed_net_id, security_groups):
        fields = {'port_name': port_name,
                  'fixed_net_id': fixed_net_id,
                  'security_groups': ('"security_groups": %s,' % json.dumps(
                      security_groups) if security_groups else '')}

        return _load_template('neutron-port.heat', fields)

    def _serialize_floating(self, inst_name, port_name, floating_net_id):
        fields = {'floating_ip_name': _get_floating_name(inst_name),
                  'floating_net_id': floating_net_id,
                  'port_name': port_name}

        return _load_template('neutron-floating.heat', fields)

    def _serialize_volume(self, inst_name, volume_idx, volumes_size):
        fields = {'volume_name': _get_volume_name(inst_name, volume_idx),
                  'volumes_size': volumes_size,
                  'volume_attach_name': _get_volume_attach_name(inst_name,
                                                                volume_idx),
                  'instance_name': inst_name}

        return _load_template('volume.heat', fields)


class ClusterStack(object):
    def __init__(self, tmpl, heat_stack):
        self.tmpl = tmpl
        self.heat_stack = heat_stack

    def wait_till_active(self):
        while self.heat_stack.stack_status in ('CREATE_IN_PROGRESS',
                                               'UPDATE_IN_PROGRESS'):
            context.sleep(1)
            self.heat_stack.get()

        if self.heat_stack.stack_status not in ('CREATE_COMPLETE',
                                                'UPDATE_COMPLETE'):
            raise ex.HeatStackException(self.heat_stack.stack_status)

    def get_node_group_instances(self, node_group):
        insts = []

        count = self.tmpl.node_groups_extra[node_group.id]['node_count']

        heat = client()
        for i in range(0, count):
            name = _get_inst_name(self.tmpl.cluster.name, node_group.name, i)
            res = heat.resources.get(self.heat_stack.id, name)
            insts.append((name, res.physical_resource_id))

        return insts
