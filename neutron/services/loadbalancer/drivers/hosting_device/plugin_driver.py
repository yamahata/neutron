# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, 2014 Intel Corporation.
# Copyright 2013, 2014 Isaku Yamahata <isaku.yamahata at intel com>
#                                     <isaku.yamahata at gmail com>
# All Rights Reserved.
#
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Isaku Yamahata, Intel Corporation.

from oslo.config import cfg

from neutron.common import exceptions
from neutron.db.loadbalancer import loadbalancer_db
from neutron.db.vm import vm_db
from neutron import manager
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services.loadbalancer.drivers import abstract_driver
from neutron.vm import constants as vm_constants
from neutron.vm.mgmt_drivers import constants as mgmt_constants


LOG = logging.getLogger(__name__)

HOSTING_DEVICE_SCHEDULER_OPTS = [
    cfg.StrOpt('loadbalancer_hosting_device_scheduler_driver',
               default='neutron.vm.hosting_device_scheduler.ChanceScheduler',
               help=_('Driver to use for scheduling '
                      'pool to a default hosting device')),
]

cfg.CONF.register_opts(HOSTING_DEVICE_SCHEDULER_OPTS)

_ACTIVE_PENDING = (
    constants.ACTIVE,
    constants.PENDING_CREATE,
    constants.PENDING_UPDATE
)


class HostingDevicePluginDriver(abstract_driver.LoadBalancerAbstractDriver):
    @property
    def _device_plugin(self):
        return manager.NeutronManager.get_service_plugins()[
            constants.SERVICEVM]

    def __init__(self, plugin):
        super(HostingDevicePluginDriver, self).__init__()
        self._plugin = plugin   # lbaas plugin
        self._device_scheduler = importutils.import_object(
            cfg.CONF.loadbalancer_hosting_device_scheduler_driver)

    def _mgmt_pool_call(self, context, pool, kwargs, callback, errorback):
        vip_id = pool['vip_id']
        if vip_id is None:
            # pool may have None vip.
            if callback is not None:
                callback()
            return
        self._device_plugin._update_service_table_instance(
            context, vip_id, kwargs, callback, errorback)

    def _mgmt_pool_id_call(self, context, pool_id, kwargs,
                           callback, errorback):
        pool = self._plugin.get_pool(context, pool_id)
        self._mgmt_pool_call(context, pool, kwargs, callback, errorback)

    ###########################################################################
    # vip
    # TODO(yamahata): race
    # TODO(yamahata): currently VIP is associated with autogenerated port-id
    #                 VIP API doesn't allows to choose from subnet and so on.
    #                 enhance VIP API and catch up LBAAS API change.
    # This ignores the VIP-assigned port
    def _schedule_vip(self, context, vip):
        network_id = self._device_plugin.subnet_id_to_network_id(
            context, vip['subnet_id'])
        pool = self._plugin.get_pool(context, vip['pool_id'])
        pool_network_id = self._device_plugin.subnet_id_to_network_id(
            context, pool['subnet_id'])
        service_context = (
            vm_db.ServiceContextEntry.create(
                network_id, None, None, None,
                vm_constants.ROLE_TWOLEG_INGRESS, None),
            vm_db.ServiceContextEntry.create(
                pool_network_id, None, None, None,
                vm_constants.ROLE_TWOLEG_EGRESS, None),)
        return self._device_scheduler.schedule(
            self._device_plugin, context, constants.LOADBALANCER,
            vip['id'], vip['name'], service_context)

    # Currently Nova API accepts network_id, not subnet_id.
    # TODO(yamahata): race
    # TODO(yamahata): add following extensions and utilize them to
    #                 nova v2/v3 API?
    #                 - extension to nova compute api to get subnet_id/port_id
    #                 - extension to boot nova instance with specified
    #                   subnet_id/port_id
    # TODO(yamahata): add extension to specify template_id and/or kwargs
    def _boot_device(self, context, vip):
        template_dict = self._device_plugin.choose_device_template(
            context, constants.LOADBALANCER)
        if not template_dict:
            raise exceptions.NotFound()

        template_id = template_dict['id']
        kwargs = {}
        subnet_id = vip['subnet_id']
        network_id = self._device_plugin.subnet_id_to_network_id(
            context, subnet_id)
        port_id = vip['port_id']
        pool = self._plugin.get_pool(context, vip['pool_id'])
        pool_subnet_id = pool['subnet_id']
        pool_network_id = self._device_plugin.subnet_id_to_network_id(
            context, pool_subnet_id)
        service_context = (
            vm_db.ServiceContextEntry.create(
                network_id, subnet_id, port_id, None,
                vm_constants.ROLE_TWOLEG_INGRESS, 0),
            vm_db.ServiceContextEntry.create(
                pool_network_id, pool_subnet_id, None, None,
                vm_constants.ROLE_TWOLEG_EGRESS, 0),)
        device_dict = (
            self._device_plugin.create_device_sync(
                context, template_id, kwargs, service_context))
        service_instance_dict = (
            self._device_plugin.
            create_service_instance_by_type(
                context, device_dict, vip['name'],
                constants.LOADBALANCER, vip['id']))
        return (device_dict, service_instance_dict)

    def _update_vip(self, context, vip, kwargs):
        vip_id = vip['id']
        callback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Vip, vip_id, constants.ACTIVE)
        errorback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Vip, vip_id, constants.ERROR)
        self._device_plugin._update_service_table_instance(
            context, vip_id, kwargs, callback, errorback)

    def create_vip(self, context, vip):
        # try to schedule hosting device
        sched_ret = self._schedule_vip(context, vip)
        if sched_ret is None:
            # no device found, so try to create it.
            sched_ret = self._boot_device(context, vip)

        kwargs = {
            mgmt_constants.KEY_ACTION: 'create_vip',
            mgmt_constants.KEY_KWARGS: {
                'vip': vip,
            }
        }
        self._update_vip(context, vip, kwargs)

    def update_vip(self, context, old_vip, vip):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'update_vip',
            mgmt_constants.KEY_KWARGS: {
                'old_vip': old_vip,
                'vip': vip,
            }
        }
        self._update_vip(context, vip, kwargs)

    def delete_vip(self, context, vip):
        vip_id = vip['id']
        kwargs = {
            mgmt_constants.KEY_ACTION: 'delete_vip',
            mgmt_constants.KEY_KWARGS: {
                'vip': vip,
            }
        }
        callback = lambda: self._plugin._delete_db_vip(context, vip_id)
        errorback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Vip, vip_id, constants.ERROR)
        self._device_plugin._delete_service_table_instance(
            context, vip_id, kwargs, callback, errorback)

    ###########################################################################
    # pool
    def _update_pool(self, context, pool, kwargs):
        pool_id = pool['id']
        callback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Pool, pool_id, constants.ACTIVE)
        errorback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Pool, pool_id, constants.ERROR)
        self._mgmt_pool_call(context, pool, kwargs, callback, errorback)

    def create_pool(self, context, pool):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'create_pool',
            mgmt_constants.KEY_KWARGS: {
                'pool': pool,
            }
        }
        self._update_pool(context, pool, kwargs)

    def update_pool(self, context, old_pool, pool):
        if pool['status'] in _ACTIVE_PENDING:
            action = 'update_pool'
        else:
            action = 'delete_pool'
        kwargs = {
            mgmt_constants.KEY_ACTION: action,
            mgmt_constants.KEY_KWARGS: {
                'old_pool': old_pool,
                'pool': pool,
            }
        }
        self._update_pool(context, pool, kwargs)

    def delete_pool(self, context, pool):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'delete_pool',
            mgmt_constants.KEY_KWARGS: {
                'pool': pool,
            }
        }
        pool_id = pool['id']
        callback = lambda: self._plugin._delete_db_pool(context, pool_id)
        errorback = lambda: self._update_pool(context, pool,
                                              constants.ERROR)
        self._mgmt_pool_call(context, pool, kwargs, callback, errorback)

    ###########################################################################
    # member
    def _update_member(self, context, action, member):
        kwargs = {
            mgmt_constants.KEY_ACTION: action,
            mgmt_constants.KEY_KWARGS: {
                'member': member,
            }
        }
        member_id = member['id']
        callback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Member, member_id, constants.ACTIVE)
        errorback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Member, member_id, constants.ERROR)
        self._mgmt_pool_id_call(context, member['pool_id'], kwargs,
                                callback, errorback)

    def create_member(self, context, member):
        self._update_member(context, 'create_member', member)

    def update_member(self, context, old_member, member):
        # member may change pool id
        if member['pool_id'] != old_member['pool_id']:
            kwargs = {
                mgmt_constants.KEY_ACTION: 'delete_member',
                mgmt_constants.KEY_KWARGS: {
                    'member': old_member,
                }
            }
            self._mgmt_pool_id_call(context, old_member['pool_id'], kwargs,
                                    None, None)
            self._update_member(context, 'create_member', member)
        else:
            self._update_member(context, 'update_member', member)

    def delete_member(self, context, member):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'delete_member',
            mgmt_constants.KEY_KWARGS: {
                'member': member,
            }
        }
        member_id = member['id']
        callback = lambda: self._plugin._delete_db_member(context,
                                                          member['id'])
        errorback = lambda: self._plugin.update_status(
            context, loadbalancer_db.Member, member_id, constants.ERROR)
        self._mgmt_pool_id_call(context, member['pool_id'], kwargs,
                                callback, errorback)

    ###########################################################################
    # health monitor
    def _update_pool_health_monitor(
            self, context, health_monitor, pool_id, kwargs):
        monitor_id = health_monitor['id']
        callback = lambda: self._plugin.update_pool_health_monitor(
            context, monitor_id, pool_id, constants.ACTIVE)
        errorback = lambda: self._plugin.update_pool_health_monitor(
            context, monitor_id, pool_id, constants.ERROR)
        self._mgmt_pool_id_call(context, pool_id, kwargs, callback, errorback)

    def create_pool_health_monitor(self, context, health_monitor, pool_id):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'create_pool_health_monitor',
            mgmt_constants.KEY_KWARGS: {
                'health_monitor': health_monitor,
                'pool_id': pool_id,
            }
        }
        self._update_pool_health_monitor(context, health_monitor, pool_id,
                                         kwargs)

    def update_pool_health_monitor(self, context, old_health_monitor,
                                   health_monitor, pool_id):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'update_pool_health_monitor',
            mgmt_constants.KEY_KWARGS: {
                'old_health_monitor': old_health_monitor,
                'health_monitor': health_monitor,
                'pool_id': pool_id,
            }
        }
        self._update_pool_health_monitor(context, health_monitor, pool_id,
                                         kwargs)

    def delete_pool_health_monitor(self, context, health_monitor, pool_id):
        kwargs = {
            mgmt_constants.KEY_ACTION: 'delete_pool_health_monitor',
            mgmt_constants.KEY_KWARGS: {
                'health_monitor': health_monitor,
                'pool_id': pool_id,
            }
        }
        monitor_id = health_monitor['id']
        callback = lambda: self._plugin._delete_db_pool_health_monitor(
            context, monitor_id, pool_id)
        errorback = lambda: self._plugin.update_pool_health_monitor(
            context, monitor_id, pool_id, constants.ERROR)
        self._mgmt_pool_id_call(context, pool_id, kwargs, callback, errorback)

    def stats(self, context, pool_id):
        pass
