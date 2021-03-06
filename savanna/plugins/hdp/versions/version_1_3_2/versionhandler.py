# Copyright (c) 2013 Hortonworks, Inc.
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
import logging
import pkg_resources as pkg
import requests

from oslo.config import cfg

from savanna import context
from savanna.plugins.general import exceptions as ex
from savanna.plugins.hdp import clusterspec as cs
from savanna.plugins.hdp import configprovider as cfgprov
from savanna.plugins.hdp.versions import abstractversionhandler as avm
from savanna.plugins.hdp.versions.version_1_3_2 import services
from savanna import version

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class VersionHandler(avm.AbstractVersionHandler):
    config_provider = None
    version = None
    client = None

    def _set_version(self, version):
        self.version = version

    def _get_config_provider(self):
        if self.config_provider is None:
            self.config_provider = cfgprov.ConfigurationProvider(
                json.load(pkg.resource_stream(version.version_info.package,
                          'plugins/hdp/versions/version_1_3_2/resources/'
                          'ambari-config-resource.json')))

        return self.config_provider

    def get_version(self):
        return self.version

    def get_ambari_client(self):
        if not self.client:
            self.client = AmbariClient(self)

        return self.client

    def get_config_items(self):
        return self._get_config_provider().get_config_items()

    def get_applicable_target(self, name):
        return self._get_config_provider().get_applicable_target(name)

    def get_cluster_spec(self, cluster, user_inputs,
                         scaled_groups=None, cluster_template=None):
        if cluster_template:
            cluster_spec = cs.ClusterSpec(cluster_template)
        else:
            cluster_spec = self.get_default_cluster_configuration()
            cluster_spec.create_operational_config(
                cluster, user_inputs, scaled_groups)

        return cluster_spec

    def get_default_cluster_configuration(self):
        return cs.ClusterSpec(self._get_default_cluster_template())

    def _get_default_cluster_template(self):
        return pkg.resource_string(
            version.version_info.package,
            'plugins/hdp/versions/version_1_3_2/resources/'
            'default-cluster.template')

    def get_node_processes(self):
        node_processes = {}
        for service in self.get_default_cluster_configuration().services:
            components = []
            for component in service.components:
                components.append(component.name)
            node_processes[service.name] = components

        return node_processes

    def install_swift_integration(self, servers):
        for server in servers:
            server.install_swift_integration()

    def get_services_processor(self):
        return services


class AmbariClient():

    def __init__(self, handler):
        #  add an argument for neutron discovery
        self.handler = handler

    def _get_http_session(self, host, port):
        return host.remote().get_http_client(port)

    def _get_standard_headers(self):
        return {"X-Requested-By": "savanna"}

    def _post(self, url, ambari_info, data=None):
        session = self._get_http_session(ambari_info.host, ambari_info.port)
        return session.post(url, data=data,
                            auth=(ambari_info.user, ambari_info.password),
                            headers=self._get_standard_headers())

    def _delete(self, url, ambari_info):
        session = self._get_http_session(ambari_info.host, ambari_info.port)
        return session.delete(url,
                              auth=(ambari_info.user, ambari_info.password),
                              headers=self._get_standard_headers())

    def _put(self, url, ambari_info, data=None):
        session = self._get_http_session(ambari_info.host, ambari_info.port)
        auth = (ambari_info.user, ambari_info.password)
        return session.put(url, data=data, auth=auth,
                           headers=self._get_standard_headers())

    def _get(self, url, ambari_info):
        session = self._get_http_session(ambari_info.host, ambari_info.port)
        return session.get(url, auth=(ambari_info.user, ambari_info.password),
                           headers=self._get_standard_headers())

    def _add_cluster(self, ambari_info, name):
        add_cluster_url = 'http://{0}/api/v1/clusters/{1}'.format(
            ambari_info.get_address(), name)
        result = self._post(add_cluster_url, ambari_info,
                            data='{"Clusters": {"version" : "HDP-' +
                            self.handler.get_version() + '"}}')

        if result.status_code != 201:
            LOG.error('Create cluster command failed. %s' % result.text)
            raise ex.HadoopProvisionError(
                'Failed to add cluster: %s' % result.text)

    def _add_configurations_to_cluster(
            self, cluster_spec, ambari_info, name):

        existing_config_url = 'http://{0}/api/v1/clusters/{1}?fields=' \
                              'Clusters/desired_configs'.format(
                                  ambari_info.get_address(), name)

        result = self._get(existing_config_url, ambari_info)

        json_result = json.loads(result.text)
        existing_configs = json_result['Clusters']['desired_configs']

        configs = cluster_spec.get_deployed_configurations()
        if len(configs) == len(existing_configs):
            # nothing to do
            return

        config_url = 'http://{0}/api/v1/clusters/{1}'.format(
            ambari_info.get_address(), name)

        body = {}
        clusters = {}
        version = 1
        body['Clusters'] = clusters
        for config_name in configs:
            if config_name == 'ambari':
                # ambari configs are currently internal to the plugin
                continue
            if config_name in existing_configs:
                if config_name == 'core-site' or config_name == 'global':
                    existing_version = existing_configs[config_name]['tag']\
                        .lstrip('v')
                    version = int(existing_version) + 1
                else:
                    continue

            config_body = {}
            clusters['desired_config'] = config_body
            config_body['type'] = config_name
            config_body['tag'] = 'v%s' % version
            config_body['properties'] = \
                cluster_spec.configurations[config_name]
            result = self._put(config_url, ambari_info, data=json.dumps(body))
            if result.status_code != 200:
                LOG.error(
                    'Set configuration command failed. {0}'.format(
                        result.text))
                raise ex.HadoopProvisionError(
                    'Failed to set configurations on cluster: %s'
                    % result.text)

    def _add_services_to_cluster(self, cluster_spec, ambari_info, name):
        services = cluster_spec.services
        add_service_url = 'http://{0}/api/v1/clusters/{1}/services/{2}'
        for service in services:
            if service.deployed and service.name != 'AMBARI':
                result = self._post(add_service_url.format(
                    ambari_info.get_address(), name, service.name),
                    ambari_info)
                if result.status_code not in [201, 409]:
                    LOG.error(
                        'Create service command failed. {0}'.format(
                            result.text))
                    raise ex.HadoopProvisionError(
                        'Failed to add services to cluster: %s' % result.text)

    def _add_components_to_services(self, cluster_spec, ambari_info, name):
        add_component_url = 'http://{0}/api/v1/clusters/{1}/services/{'\
                            '2}/components/{3}'
        for service in cluster_spec.services:
            if service.deployed and service.name != 'AMBARI':
                for component in service.components:
                    result = self._post(add_component_url.format(
                        ambari_info.get_address(), name, service.name,
                        component.name),
                        ambari_info)
                    if result.status_code not in [201, 409]:
                        LOG.error(
                            'Create component command failed. {0}'.format(
                                result.text))
                        raise ex.HadoopProvisionError(
                            'Failed to add components to services: %s'
                            % result.text)

    def _add_hosts_and_components(
            self, cluster_spec, servers, ambari_info, name):

        add_host_url = 'http://{0}/api/v1/clusters/{1}/hosts/{2}'
        add_host_component_url = 'http://{0}/api/v1/clusters/{1}' \
                                 '/hosts/{2}/host_components/{3}'
        for host in servers:
            hostname = host.instance.fqdn().lower()
            result = self._post(
                add_host_url.format(ambari_info.get_address(), name, hostname),
                ambari_info)
            if result.status_code != 201:
                LOG.error(
                    'Create host command failed. {0}'.format(result.text))
                raise ex.HadoopProvisionError(
                    'Failed to add host: %s' % result.text)

            node_group_name = host.node_group.name
            #TODO(jspeidel): ensure that node group exists
            node_group = cluster_spec.node_groups[node_group_name]
            for component in node_group.components:
                # don't add any AMBARI components
                if component.find('AMBARI') != 0:
                    result = self._post(add_host_component_url.format(
                        ambari_info.get_address(), name, hostname, component),
                        ambari_info)
                    if result.status_code != 201:
                        LOG.error(
                            'Create host_component command failed. %s' %
                            result.text)
                        raise ex.HadoopProvisionError(
                            'Failed to add host component: %s' % result.text)

    def _install_services(self, cluster_name, ambari_info):
        LOG.info('Installing required Hadoop services ...')

        ambari_address = ambari_info.get_address()
        install_url = 'http://{0}/api/v1/clusters/{' \
                      '1}/services?ServiceInfo/state=INIT'.format(
                      ambari_address, cluster_name)
        body = '{"RequestInfo" : { "context" : "Install all services" },'\
               '"Body" : {"ServiceInfo": {"state" : "INSTALLED"}}}'

        result = self._put(install_url, ambari_info, data=body)

        if result.status_code == 202:
            json_result = json.loads(result.text)
            request_id = json_result['Requests']['id']
            success = self._wait_for_async_request(self._get_async_request_uri(
                ambari_info, cluster_name, request_id),
                ambari_info)
            if success:
                LOG.info("Install of Hadoop stack successful.")
                self._finalize_ambari_state(ambari_info)
            else:
                LOG.critical('Install command failed.')
                raise ex.HadoopProvisionError(
                    'Installation of Hadoop stack failed.')
        elif result.status_code != 200:
            LOG.error(
                'Install command failed. {0}'.format(result.text))
            raise ex.HadoopProvisionError(
                'Installation of Hadoop stack failed.')

    def _get_async_request_uri(self, ambari_info, cluster_name, request_id):
        return 'http://{0}/api/v1/clusters/{1}/requests/{' \
               '2}/tasks?fields=Tasks/status'.format(
               ambari_info.get_address(), cluster_name,
               request_id)

    def _wait_for_async_request(self, request_url, ambari_info):
        started = False
        while not started:
            result = self._get(request_url, ambari_info)
            LOG.debug(
                'async request ' + request_url + ' response:\n' + result.text)
            json_result = json.loads(result.text)
            started = True
            for items in json_result['items']:
                status = items['Tasks']['status']
                if status == 'FAILED' or status == 'ABORTED':
                    return False
                else:
                    if status != 'COMPLETED':
                        started = False

            context.sleep(5)
        return started

    def _finalize_ambari_state(self, ambari_info):
        LOG.info('Finalizing Ambari cluster state.')

        persist_state_uri = 'http://{0}/api/v1/persist'.format(
            ambari_info.get_address())
        # this post data has non-standard format because persist
        # resource doesn't comply with Ambari API standards
        persist_data = '{ "CLUSTER_CURRENT_STATUS":' \
                       '"{\\"clusterState\\":\\"CLUSTER_STARTED_5\\"}" }'
        result = self._post(persist_state_uri, ambari_info, data=persist_data)

        if result.status_code != 201 and result.status_code != 202:
            LOG.warning('Finalizing of Ambari cluster state failed. {0}'.
                        format(result.text))
            raise ex.HadoopProvisionError('Unable to finalize Ambari state.')

    def start_services(self, cluster_name, cluster_spec, ambari_info):
        LOG.info('Starting Hadoop services ...')
        LOG.info('Cluster name: {0}, Ambari server address: {1}'
                 .format(cluster_name, ambari_info.get_address()))
        start_url = 'http://{0}/api/v1/clusters/{1}/services?ServiceInfo/' \
                    'state=INSTALLED'.format(
                        ambari_info.get_address(), cluster_name)
        body = '{"RequestInfo" : { "context" : "Start all services" },'\
               '"Body" : {"ServiceInfo": {"state" : "STARTED"}}}'

        self._fire_service_start_notifications(
            cluster_name, cluster_spec, ambari_info)
        result = self._put(start_url, ambari_info, data=body)
        if result.status_code == 202:
            json_result = json.loads(result.text)
            request_id = json_result['Requests']['id']
            success = self._wait_for_async_request(
                self._get_async_request_uri(ambari_info, cluster_name,
                                            request_id), ambari_info)
            if success:
                LOG.info(
                    "Successfully started Hadoop cluster '{0}'.".format(
                        cluster_name))
            else:
                LOG.critical('Failed to start Hadoop cluster.')
                raise ex.HadoopProvisionError(
                    'Start of Hadoop services failed.')

        elif result.status_code != 200:
            LOG.error(
                'Start command failed. Status: {0}, response: {1}'.
                format(result.status_code, result.text))
            raise ex.HadoopProvisionError(
                'Start of Hadoop services failed.')

    def _exec_ambari_command(self, ambari_info, body, cmd_uri):

        LOG.debug('PUT URI: {0}'.format(cmd_uri))
        result = self._put(cmd_uri, ambari_info, data=body)
        if result.status_code == 202:
            LOG.debug(
                'PUT response: {0}'.format(result.text))
            json_result = json.loads(result.text)
            href = json_result['href'] + '/tasks?fields=Tasks/status'
            success = self._wait_for_async_request(href, ambari_info)
            if success:
                LOG.info(
                    "Successfully changed state of Hadoop components ")
            else:
                LOG.critical('Failed to change state of Hadoop '
                             'components')
                raise RuntimeError('Failed to change state of Hadoop '
                                   'components')

        else:
            LOG.error(
                'Command failed. Status: {0}, response: {1}'.
                format(result.status_code, result.text))
            raise RuntimeError('Hadoop/Ambari command failed.')

    def _get_host_list(self, servers):
        host_list = [server.instance.fqdn().lower() for server in servers]
        return ",".join(host_list)

    def _install_and_start_components(self, cluster_name, servers,
                                      ambari_info, cluster_spec):

        auth = (ambari_info.user, ambari_info.password)
        self._install_components(ambari_info, auth, cluster_name, servers)
        self.handler.install_swift_integration(servers)
        self._start_components(ambari_info, auth, cluster_name,
                               servers, cluster_spec)

    def _install_components(self, ambari_info, auth, cluster_name, servers):
        LOG.info('Starting Hadoop components while scaling up')
        LOG.info('Cluster name {0}, Ambari server ip {1}'
                 .format(cluster_name, ambari_info.get_address()))
        # query for the host components on the given hosts that are in the
        # INIT state
        #TODO(jspeidel): provide request context
        body = '{"HostRoles": {"state" : "INSTALLED"}}'
        install_uri = 'http://{0}/api/v1/clusters/{' \
                      '1}/host_components?HostRoles/state=INIT&' \
                      'HostRoles/host_name.in({2})'.format(
                      ambari_info.get_address(), cluster_name,
                      self._get_host_list(servers))
        self._exec_ambari_command(ambari_info, body, install_uri)

    def _start_components(self, ambari_info, auth, cluster_name, servers,
                          cluster_spec):
        # query for all the host components in the INSTALLED state,
        # then get a list of the client services in the list
        installed_uri = 'http://{0}/api/v1/clusters/{'\
                        '1}/host_components?HostRoles/state=INSTALLED&'\
                        'HostRoles/host_name.in({2})'.format(
                            ambari_info.get_address(), cluster_name,
                            self._get_host_list(servers))
        result = self._get(installed_uri, ambari_info)
        if result.status_code == 200:
            LOG.debug(
                'GET response: {0}'.format(result.text))
            json_result = json.loads(result.text)
            items = json_result['items']

            client_set = cluster_spec.get_components_for_type('CLIENT')
            inclusion_list = list(set([x['HostRoles']['component_name']
                                       for x in items
                                       if x['HostRoles']['component_name']
                                       not in client_set]))

            # query and start all non-client components on the given set of
            # hosts
            #TODO(jspeidel): Provide request context
            body = '{"HostRoles": {"state" : "STARTED"}}'
            start_uri = 'http://{0}/api/v1/clusters/{'\
                        '1}/host_components?HostRoles/state=INSTALLED&'\
                        'HostRoles/host_name.in({2})'\
                        '&HostRoles/component_name.in({3})'.format(
                        ambari_info.get_address(), cluster_name,
                        self._get_host_list(servers),
                        ",".join(inclusion_list))
            self._exec_ambari_command(ambari_info, body, start_uri)
        else:
            raise ex.HadoopProvisionError(
                'Unable to determine installed service '
                'components in scaled instances.  status'
                ' code returned = {0}'.format(result.status))

    def wait_for_host_registrations(self, num_hosts, ambari_info):
        LOG.info(
            'Waiting for all Ambari agents to register with server ...')

        url = 'http://{0}/api/v1/hosts'.format(ambari_info.get_address())
        result = None
        json_result = None

        #TODO(jspeidel): timeout
        while result is None or len(json_result['items']) < num_hosts:
            context.sleep(5)
            try:
                result = self._get(url, ambari_info)
                json_result = json.loads(result.text)

                LOG.info('Registered Hosts: {0} of {1}'.format(
                    len(json_result['items']), num_hosts))
                for hosts in json_result['items']:
                    LOG.debug('Registered Host: {0}'.format(
                        hosts['Hosts']['host_name']))
            except requests.ConnectionError:
                #TODO(jspeidel): max wait time
                LOG.info('Waiting to connect to ambari server ...')

    def update_ambari_admin_user(self, password, ambari_info):
        old_pwd = ambari_info.password
        user_url = 'http://{0}/api/v1/users/admin'.format(
            ambari_info.get_address())
        update_body = '{{"Users":{{"roles":"admin","password":"{0}",' \
                      '"old_password":"{1}"}} }}'.format(password, old_pwd)

        result = self._put(user_url, ambari_info, data=update_body)

        if result.status_code != 200:
            raise ex.HadoopProvisionError('Unable to update Ambari admin user'
                                          ' credentials: {0}'.format(
                                          result.text))

    def add_ambari_user(self, user, ambari_info):
        user_url = 'http://{0}/api/v1/users/{1}'.format(
            ambari_info.get_address(), user.name)

        create_body = '{{"Users":{{"password":"{0}","roles":"{1}"}} }}'. \
            format(user.password, '%s' % ','.join(map(str, user.groups)))

        result = self._post(user_url, ambari_info, data=create_body)

        if result.status_code != 201:
            raise ex.HadoopProvisionError(
                'Unable to create Ambari user: {0}'.format(result.text))

    def delete_ambari_user(self, user_name, ambari_info):
        user_url = 'http://{0}/api/v1/users/{1}'.format(
            ambari_info.get_address(), user_name)

        result = self._delete(user_url, ambari_info)

        if result.status_code != 200:
            raise ex.HadoopProvisionError(
                'Unable to delete Ambari user: {0}'
                ' : {1}'.format(user_name, result.text))

    def configure_scaled_cluster_instances(self, name, cluster_spec,
                                           num_hosts, ambari_info):
        self.wait_for_host_registrations(num_hosts, ambari_info)
        self._add_configurations_to_cluster(
            cluster_spec, ambari_info, name)
        self._add_services_to_cluster(
            cluster_spec, ambari_info, name)
        self._add_components_to_services(
            cluster_spec, ambari_info, name)
        self._install_services(name, ambari_info)

    def start_scaled_cluster_instances(self, name, cluster_spec, servers,
                                       ambari_info):
        self.start_services(name, cluster_spec, ambari_info)
        self._add_hosts_and_components(
            cluster_spec, servers, ambari_info, name)
        self._install_and_start_components(
            name, servers, ambari_info, cluster_spec)

    def provision_cluster(self, cluster_spec, servers, ambari_info, name):
        self._add_cluster(ambari_info, name)
        self._add_configurations_to_cluster(cluster_spec, ambari_info, name)
        self._add_services_to_cluster(cluster_spec, ambari_info, name)
        self._add_components_to_services(cluster_spec, ambari_info, name)
        self._add_hosts_and_components(
            cluster_spec, servers, ambari_info, name)

        self._install_services(name, ambari_info)
        self.handler.install_swift_integration(servers)

    def cleanup(self, ambari_info):
        ambari_info.host.remote().close_http_sessions()

    def _get_services_in_state(self, cluster_name, ambari_info, state):
        services_url = 'http://{0}/api/v1/clusters/{1}/services?' \
                       'ServiceInfo/state.in({2})'.format(
                           ambari_info.get_address(), cluster_name, state)

        result = self._get(services_url, ambari_info)

        json_result = json.loads(result.text)
        services = []
        for service in json_result['items']:
            services.append(service['ServiceInfo']['service_name'])

        return services

    def _fire_service_start_notifications(self, cluster_name,
                                          cluster_spec, ambari_info):
        started_services = self._get_services_in_state(
            cluster_name, ambari_info, 'STARTED')
        for service in cluster_spec.services:
            if service.deployed and not service.name in started_services:
                service.pre_service_start(cluster_spec, ambari_info,
                                          started_services)
