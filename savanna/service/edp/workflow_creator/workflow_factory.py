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

import six

from savanna import conductor as c
from savanna import context
from savanna.plugins import base as plugin_base
from savanna.plugins.general import utils as u
from savanna.service.edp import hdfs_helper as h
from savanna.service.edp.workflow_creator import hive_workflow
from savanna.service.edp.workflow_creator import java_workflow
from savanna.service.edp.workflow_creator import mapreduce_workflow
from savanna.service.edp.workflow_creator import pig_workflow
from savanna.utils import remote
from savanna.utils import xmlutils


conductor = c.API

swift_username = 'fs.swift.service.savanna.username'
swift_password = 'fs.swift.service.savanna.password'


class BaseFactory(object):
    def configure_workflow_if_needed(self, *args, **kwargs):
        pass

    def get_configs(self, input_data, output_data):
        configs = {}
        for src in (input_data, output_data):
            if src.type == "swift" and hasattr(src, "credentials"):
                if "user" in src.credentials:
                    configs[swift_username] = src.credentials['user']
                if "password" in src.credentials:
                    configs[swift_password] = src.credentials['password']
                break
        return configs

    def get_params(self, input_data, output_data):
        return {'INPUT': input_data.url,
                'OUTPUT': output_data.url}

    def update_configs(self, configs, execution_configs):
        if execution_configs is not None:
            for key, value in six.iteritems(configs):
                if hasattr(value, "update"):
                    new_vals = execution_configs.get(key, {})
                    value.update(new_vals)


class PigFactory(BaseFactory):
    def __init__(self, job):
        super(PigFactory, self).__init__()

        self.name = self.get_script_name(job)

    def get_script_name(self, job):
        return conductor.job_main_name(context.ctx(), job)

    def get_workflow_xml(self, execution, input_data, output_data):
        configs = {'configs': self.get_configs(input_data, output_data),
                   'params': self.get_params(input_data, output_data),
                   'args': []}
        self.update_configs(configs, execution.job_configs)

        # Update is not supported for list types, and besides
        # since args are listed (not named) update doesn't make
        # sense, just replacement of any default args
        if execution.job_configs:
            configs['args'] = execution.job_configs.get('args', [])

        creator = pig_workflow.PigWorkflowCreator()
        creator.build_workflow_xml(self.name,
                                   configuration=configs['configs'],
                                   params=configs['params'],
                                   arguments=configs['args'])
        return creator.get_built_workflow_xml()


class HiveFactory(BaseFactory):
    def __init__(self, job):
        super(HiveFactory, self).__init__()

        self.name = self.get_script_name(job)
        self.job_xml = "hive-site.xml"

    def get_script_name(self, job):
        return conductor.job_main_name(context.ctx(), job)

    def get_workflow_xml(self, execution, input_data, output_data):
        configs = {'configs': self.get_configs(input_data, output_data),
                   'params': self.get_params(input_data, output_data)}
        self.update_configs(configs, execution.job_configs)
        creator = hive_workflow.HiveWorkflowCreator()
        creator.build_workflow_xml(self.name,
                                   self.job_xml,
                                   configuration=configs['configs'],
                                   params=configs['params'])
        return creator.get_built_workflow_xml()

    def configure_workflow_if_needed(self, cluster, wf_dir):
        h_s = u.get_hiveserver(cluster)
        plugin = plugin_base.PLUGINS.get_plugin(cluster.plugin_name)
        hdfs_user = plugin.get_hdfs_user()
        h.copy_from_local(remote.get_remote(h_s),
                          plugin.get_hive_config_path(), wf_dir, hdfs_user)


class MapReduceFactory(BaseFactory):

    def get_configs(self, input_data, output_data):
        configs = super(MapReduceFactory, self).get_configs(input_data,
                                                            output_data)
        configs['mapred.input.dir'] = input_data.url
        configs['mapred.output.dir'] = output_data.url
        return configs

    def get_workflow_xml(self, execution, input_data, output_data):
        configs = {'configs': self.get_configs(input_data, output_data)}
        self.update_configs(configs, execution.job_configs)
        creator = mapreduce_workflow.MapReduceWorkFlowCreator()
        creator.build_workflow_xml(configuration=configs['configs'])
        return creator.get_built_workflow_xml()


class JavaFactory(BaseFactory):

    def get_workflow_xml(self, execution, *args, **kwargs):
        # input and output will be handled as args, so we don't really
        # know whether or not to include the swift configs.  Hmmm.
        configs = {'configs': {},
                   'args': []}
        self.update_configs(configs, execution.job_configs)

        # Update is not supported for list types, and besides
        # since args are listed (not named) update doesn't make
        # sense, just replacement of any default args
        if execution.job_configs:
            configs['args'] = execution.job_configs.get('args', [])

        if hasattr(execution, 'java_opts'):
            java_opts = execution.java_opts
        else:
            java_opts = ""

        creator = java_workflow.JavaWorkflowCreator()
        creator.build_workflow_xml(execution.main_class,
                                   configuration=configs['configs'],
                                   java_opts=java_opts,
                                   arguments=configs['args'])
        return creator.get_built_workflow_xml()


def get_creator(job):

    def make_PigFactory():
        return PigFactory(job)

    def make_HiveFactory():
        return HiveFactory(job)

    factories = [
        MapReduceFactory,
        make_HiveFactory,
        make_PigFactory,
        JavaFactory,
        # Keep 'Jar' as a synonym for 'MapReduce'
        MapReduceFactory,
    ]
    type_map = dict(zip(get_possible_job_types(), factories))

    return type_map[job.type]()


def get_possible_job_config(job_type):
    if job_type not in get_possible_job_types():
        return None

    if job_type == "Java":
        return {'job_config': {'configs': [], 'args': []}}

    if job_type in ['MapReduce', 'Pig', 'Jar']:
        #TODO(nmakhotkin) Savanna should return config based on specific plugin
        cfg = xmlutils.load_hadoop_xml_defaults(
            'plugins/vanilla/resources/mapred-default.xml')
        if job_type in ['MapReduce', 'Jar']:
            cfg += xmlutils.load_hadoop_xml_defaults(
                'service/edp/resources/mapred-job-config.xml')
    elif job_type == 'Hive':
        #TODO(nmakhotkin) Savanna should return config based on specific plugin
        cfg = xmlutils.load_hadoop_xml_defaults(
            'plugins/vanilla/resources/hive-default.xml')

    # TODO(tmckay): args should be a list when bug #269968
    # is fixed on the UI side
    config = {'configs': cfg, "args": {}}
    if job_type not in ['MapReduce', 'Jar', 'Java']:
        config.update({'params': {}})
    return {'job_config': config}


def get_possible_job_types():
    return [
        'MapReduce',
        'Hive',
        'Pig',
        'Java',
        'Jar',
    ]
