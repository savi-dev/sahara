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

from keystoneclient import access
from keystoneclient.openstack.common import jsonutils
from keystoneclient.v2_0 import client as keystone_client

from sahara.openstack.common import log as logging
from sahara.utils.openstack import base as utils


LOG = logging.getLogger(__name__)


class CatalogPresent:
    """Handles the missing of service catalog at headers."""

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf

    def __call__(self, env, start_response):
        """Ensures that the service catalog is present.

        Sahara requires the presence of service catalog at request's header.
        This function check the presence of service catalog, and get it from
        Keystone if it is missing.
        """
        if 'HTTP_X_SERVICE_CATALOG' not in env:
            LOG.debug("Service catalog is not prsent. Get it from Keystone")

            token_info = env['keystone.token_info']
            env['HTTP_X_SERVICE_CATALOG'] = self._request_catalog(token_info)

        return self.app(env, start_response)

    def _request_catalog(self, token_info):
        """Retrieve catalog from keystone."""
        auth_url = utils.retrieve_auth_url()
        auth_ref = access.AccessInfo.factory(body=token_info)

        keystone = keystone_client.Client(username=auth_ref.username,
                                          token=auth_ref.auth_token,
                                          tenant_id=auth_ref.project_id,
                                          auth_url=auth_url)
        catalog = keystone.service_catalog.get_data()
        return jsonutils.dumps(catalog)


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def filter(app):
        return CatalogPresent(app, conf)

    return filter
