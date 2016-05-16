# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
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

# pylint: disable=W0212
# pylint: disable=R0913
# pylint: disable=W0233
# pylint: disable=W0231

"""
Tools for ceph config.
"""

import os
import uuid
from vsm import utils
from vsm import context as vsm_context
from vsm import manager
from vsm import flags
from vsm import db
from vsm.agent import rpcapi as agent_rpc
from vsm.openstack.common import log as logging
from vsm.openstack.common.gettextutils import _

LOG = logging.getLogger(__name__)
FLAGS = flags.FLAGS

class Section(object):
    def __init__(self, name=None, options=None):
        self.sec_name = name
        self.options = options or {}

    def name(self):
        return self.sec_name

    def values(self):
        return self.options

    def set(self, key, value):
        self.options[key] = value

    def get(self, key):
        return self.options[key]

    def as_dict(self):
        return {self.sec_name: self.options}

    def as_str(self):
        label = "[%s]\n" % self.sec_name
        content = ""
        for (key, val) in self.options.items():
            content = content + ("%s = %s\n" % (key, val))

        return label + content

class Parser(object):
    def __init__(self):
        self._sections = {}

    def init(self, contents):
        self._sections = {}

        sec_name = None
        options = {}
        for line in contents:
            line = line.strip()
            if line.startswith('#') or len(line) <= 1:
                continue

            if line.find('#') != -1:
                vs = line.split('#')
                line = vs[0]

            if line.find('[') != -1 and line.find(']') != -1:
                line = line.replace('[', ' ')
                line = line.replace(']', ' ').strip()

                if sec_name:
                    self._sections[sec_name] = options

                sec_name = line
                options = {}
            elif line.find('=') != -1:
                key = line[0:line.find('=')].strip()
                val = line[line.find('=')+1:].strip()
                options[key] = val

        self._sections[sec_name] = options

    def read(self, file_path):
        with open(file_path, 'r') as f:
            content = f.readlines()
            self.init(content)

    def write(self, file_path, rgw=False):
        content = self.as_str(rgw)
        with open(FLAGS.ceph_conf, 'w') as f:
            f.write(content)
            f.write('\n')

    def as_dict(self):
        return self._sections

    def as_str(self, rgw=False):
        lines = ""
        d = self._sections.copy()
        def _sec_as_str(sec_name):
            return Section(sec_name, d[sec_name]).as_str() + '\n'

        def _secs_as_str(sec_type):
            strs = ''
            if sec_type != "client":
                strs = _sec_as_str(sec_type)
                d.pop(sec_type)
            drop_list = []
            for sec in d:
                if sec.find(sec_type+'.') != -1:
                    drop_list.append(sec)

            for sec in sorted(drop_list):
                strs = strs + _sec_as_str(sec)

            for sec in drop_list:
                if sec.find(sec_type+'.') != -1:
                    d.pop(sec)

            return strs

        if self.has_section('global'):
            lines = lines + _secs_as_str('global')

        if self.has_section('mon'):
            lines = lines + _secs_as_str('mon')

        if self.has_section('mds'):
            lines = lines + _secs_as_str('mds')

        if self.has_section('osd'):
            lines = lines + _secs_as_str('osd')

        if rgw:
            lines = lines + _secs_as_str("client")

        lines = lines.strip()

        return lines

    def has_section(self, sec_name):
        sec_name = sec_name.strip()
        if self._sections.get(sec_name):
            return True
        return False

    def add_section(self, sec_name):
        sec_name = sec_name.strip()
        if self.has_section(sec_name):
            return

        self._sections[sec_name] = {}

    def remove_section(self, sec_name):
        sec_name = sec_name.strip()
        if not self.has_section(sec_name):
            return

        self._sections.pop(sec_name)

    def get(self, sec_name, key, default_val=None):
        if not self.has_section(sec_name):
            return None

        sec = self._sections[sec_name]
        if sec.get(key, None):
            return sec.get(key, default_val)

        return sec.get(key.replace('_', ' '), default_val)

    def set(self, sec_name, key, val):
        sec_name = sec_name.strip()
        key = key.strip()
        val = val.strip()
        self._sections[sec_name][key] = val

    def sections(self):
        return self._sections

class CephConfigParser(manager.Manager):
    cluster_id = None
    context = vsm_context.get_admin_context()
    _agent_rpcapi = agent_rpc.AgentAPI()

    def _get_cluster_id(self):
        cluster_id_file = os.path.join(FLAGS.state_path, 'cluster_id')
        if not os.path.exists(cluster_id_file):
            return None

        cid = utils.read_file_as_root(cluster_id_file)
        cid = cid.strip()
        self.cluster_id = cid
        return self.cluster_id

    def _load_ceph_conf_from_db(self):
        if not self.cluster_id:
            if not self._get_cluster_id():
                LOG.debug('Can not get cluster_id')
                return

        ceph_conf = db.cluster_get_ceph_conf(self.context,
                                             self.cluster_id)

        if not ceph_conf:
            return

        utils.write_file_as_root(FLAGS.ceph_conf, ceph_conf, 'w')

        # We try to update fstab here.
        utils.execute('sed',
                      '-i',
                      '/forvsmosd/d',
                      '/etc/fstab',
                      run_as_root=True)

        parser = Parser()
        parser.read(FLAGS.ceph_conf)
        fs_type = parser.get('osd', 'osd mkfs type', 'xfs')
        cluster = db.cluster_get_all(self.context)[0]
        mount_option = cluster['mount_option']
        if not mount_option:
            mount_option = utils.get_fs_options(fs_type)[1]
        mount_attr = parser.get('osd', 'osd mount options %s' % fs_type, mount_option)

        for sec in parser.sections():
            if sec.find('osd.') != -1:
                osd_id = sec.split('.')[1]
                mount_path = os.path.join(FLAGS.osd_data_path, "osd%s" % osd_id)
                mount_disk = parser.get(sec, 'devs')
                mount_host = parser.get(sec, 'host')
                if FLAGS.host == mount_host:
                    line = mount_disk + ' ' + mount_path
                    line = line + ' ' + fs_type
                    line = line + ' ' + mount_attr + ' 0 0'
                    line = line + ' ' + '## forvsmosd'
                    utils.write_file_as_root('/etc/fstab', line)

    def __init__(self, fp=None, *args, **kwargs):
        super(CephConfigParser, self).__init__(*args, **kwargs)
        self._parser = Parser()
        self._load_ceph_conf_from_db()

        try:
            if not fp is None:
                if isinstance(fp, str) or isinstance(fp, unicode):
                    if os.path.exists(fp) and os.path.isfile(fp):
                        self._parser.read(fp)
                elif isinstance(fp, dict):
                    for k, v  in fp.iteritems():
                        self._parser.add_section(k)
                        for key, val in v.iteritems():
                            self._parser.set(k, key, val)
        except:
            LOG.error(_('Failed to load ceph configuration'))
            raise

    def __get_type_number(self, sec_type):
        cnt = 0
        for sec in self._parser._sections:
            if sec.lower().find(sec_type.lower()) != -1:
                cnt = cnt + 1
        return cnt

    def get_mon_num(self):
        return self.__get_type_number('mon.')

    def get_mds_num(self):
        return self.__get_type_number('mds.')

    def get_osd_num(self):
        return self.__get_type_number('osd.')

    def as_sections(self):
        return self._parser._sections

    def as_dict(self):
        return self._parser.as_dict()

    # def add_global(self,
    #                pg_num=None,
    #                heartbeat_interval=None,
    #                osd_heartbeat_interval=None,
    #                osd_heartbeat_grace=None,
    #                is_cephx=True,
    #                max_file=131072,
    #                down_out_interval=90,
    #                pool_default_size=3):

    def add_global(self,dict_kvs={}):
        dict_kvs['max_file'] = dict_kvs.get('max_file',131072)
        dict_kvs['is_cephx'] = dict_kvs.get('is_cephx',True)
        dict_kvs['down_out_interval'] = dict_kvs.get('down_out_interval',True)
        dict_kvs['pool_default_size'] = dict_kvs.get('pool_default_size',3)

        self._parser.add_section('global')
        if not dict_kvs['is_cephx']:
            self._parser.set('global', 'auth supported', 'none')
        else:
            self._parser.set('global', 'auth supported', 'cephx')
        self._parser.set('global', 'max open files', str(dict_kvs['max_file']))
        self._parser.set('global',
                         'mon osd down out interval',
                         str(dict_kvs['down_out_interval']))

        for key,value in dict_kvs.items():
            if  key not in ['max_file','is_cephx','down_out_interval','pool_default_size']:
                self._parser.set('global',
                         key.replace('_',' '),
                         str(dict_kvs[key]))
        # Must add fsid for create cluster in newer version of ceph.
        # In order to support lower version of vsm.
        # We set keyring path here.
        # keyring = /etc/ceph/keyring.admin
        self._parser.set('global', 'keyring', '/etc/ceph/keyring.admin')
        # Have to setup fsid.
        self._parser.set('global', 'fsid', str(uuid.uuid1()))

    def add_mds_header(self, dict_kvs={}):
        if self._parser.has_section('mds'):
            return
        dict_kvs['keyring'] = dict_kvs.get('keyring','false')

        self._parser.add_section('mds')
        # NOTE : settings for mds.
        self._parser.set('mds', 'mds data', '/var/lib/ceph/mds/ceph-$id')
        self._parser.set('mds', 'mds standby replay', dict_kvs['keyring'])
        self._parser.set('mds', 'keyring', '/etc/ceph/keyring.$name')
        for key,value in dict_kvs.items():
            if  key not in ['keyring']:
                self._parser.set('mds',
                         key.replace('_',' '),
                         str(dict_kvs[key]))

    def add_mon_header(self, dict_kvs={}):
        if self._parser.has_section('mon'):
            return
        dict_kvs['clock_drift'] = dict_kvs.get('clock_drift',200)
        dict_kvs['cnfth'] = dict_kvs.get('cnfth',None)
        dict_kvs['cfth'] = dict_kvs.get('cfth',None)

        self._parser.add_section('mon')
        # NOTE
        # the default mon data dir set in ceph-deploy.
        # is in:
        # /var/lib/ceph/mon/ceph-$id/
        # In order to support created by mkcephfs and live update.
        # We have to set it to: mon_data="/var/lib/ceph/mon/mon$id"
        mon_data = "/var/lib/ceph/mon/mon$id"
        self._parser.set('mon', 'mon data', mon_data)
        self._parser.set('mon',
                         'mon clock drift allowed',
                         '.' + str(dict_kvs['clock_drift']))
        if dict_kvs['cfth']:
            self._parser.set('mon', 'mon osd full ratio', '.' + str(dict_kvs['cfth']))
        if dict_kvs['cnfth']:
            self._parser.set('mon', 'mon osd nearfull ratio', '.' + str(dict_kvs['cnfth']))
        for key,value in dict_kvs.items():
            if  key not in ['clock_drift','cnfth','cfth']:
                self._parser.set('mon',
                         key.replace('_',' '),
                         str(dict_kvs[key]))

    def _update_ceph_conf_into_db(self, content):
        if not self.cluster_id:
            if not self._get_cluster_id():
                return

        db.cluster_update_ceph_conf(self.context, self.cluster_id, content)
        server_list = db.init_node_get_all(self.context)
        for ser in server_list:
            self._agent_rpcapi.update_ceph_conf(self.context, ser['host'])

    def save_conf(self, file_path=FLAGS.ceph_conf, rgw=False):
        utils.execute('chown',
                      '-R',
                      'vsm:vsm',
                      '/etc/ceph/',
                      run_as_root=True)

        self._parser.write(file_path, rgw)
        self._update_ceph_conf_into_db(self._parser.as_str(rgw))

    def add_mon(self, hostname, ip, mon_id):
        sec = 'mon.%s' % mon_id
        if self._parser.has_section(sec):
            return

        self._parser.add_section(sec)
        self._parser.set(sec, 'host', hostname)
        ips = ip.split(',')
        ip_strs = ['%s:%s' % (i, str(6789)) for i in ips]
        ip_str = ','.join(ip_strs)
        self._parser.set(sec, 'mon addr', ip_str)

    def add_mds(self, hostname, ip, mds_id):
        sec = 'mds.%s' % mds_id
        if self._parser.has_section(sec):
            return

        self._parser.add_section(sec)
        self._parser.set(sec, 'host', hostname)
        self._parser.set(sec, 'public addr', '%s' % ip)


    def add_osd_header(self, dict_kvs={}):
        if self._parser.has_section('osd'):
            return
        dict_kvs['journal_size'] = dict_kvs.get('journal_size',0)
        dict_kvs['osd_type'] = dict_kvs.get('osd_type','xfs')
        dict_kvs['osd_heartbeat_interval'] = dict_kvs.get('osd_heartbeat_interval',10)
        dict_kvs['osd_heartbeat_grace'] = dict_kvs.get('osd_heartbeat_grace',10)
        self._parser.add_section('osd')
        # NOTE Do not add osd data here.
        self._parser.set('osd', 'osd journal size', str(dict_kvs['journal_size']))
        self._parser.set('osd', 'filestore xattr use omap', 'true')
        self._parser.set('osd', 'osd crush update on start', 'false' )
        osd_data = "/var/lib/ceph/osd/osd$id"
        self._parser.set('osd', 'osd data', osd_data)
        # NOTE add keyring to support lower version of OSD.
        # keyring = /etc/ceph/keyring.$name
        self._parser.set('osd', 'keyring', '/etc/ceph/keyring.$name')
        self._parser.set('osd', 'osd heartbeat interval', str(dict_kvs['osd_heartbeat_interval']))
        self._parser.set('osd', 'osd heartbeat grace', str(dict_kvs['osd_heartbeat_grace']))
        self._parser.set('osd', 'osd mkfs type', dict_kvs['osd_type'])
        cluster = db.cluster_get_all(self.context)[0]
        mount_option = cluster['mount_option']
        if not mount_option:
            mount_option = utils.get_fs_options(dict_kvs['osd_type'])[1]
        self._parser.set('osd', 'osd mount options %s' % dict_kvs['osd_type'], mount_option)

        # Below is very important for set file system.
        # Do not change any of them.
        format_type = '-f'
        if dict_kvs['osd_type'].lower() == 'ext4':
            format_type = '-F'
        self._parser.set('osd', 'osd mkfs options %s' % dict_kvs['osd_type'], format_type)
        for key,value in dict_kvs.items():
            if key not in ['journal_size','osd_type','osd_heartbeat_interval','osd_heartbeat_grace']:
                self._parser.set('osd', key.replace("_",' '), str(value))

    def add_osd(self,
                hostname,
                pub_addr,
                cluster_addr,
                osd_dev,
                journal_dev,
                osd_id):
        sec = 'osd.%s' % osd_id
        if self._parser.has_section(sec):
            return

        self._parser.add_section(sec)
        if hostname is None \
           or pub_addr is None\
           or cluster_addr is None \
           or journal_dev is None \
           or osd_dev is None:
            LOG.error('cephconfigparser error, all parameters are empty')
            raise

        self._parser.set(sec, 'host', hostname)

        # a list or tuple of public ip
        if hasattr(pub_addr, '__iter__'):
            ip_str = ','.join([ip for ip in pub_addr])
            self._parser.set(sec,
                             'public addr',
                             ip_str)
        else:
            self._parser.set(sec, 'public addr', pub_addr)

        self._parser.set(sec, 'cluster addr', cluster_addr)
        self._parser.set(sec, 'osd journal', journal_dev)
        self._parser.set(sec, 'devs', osd_dev)

    def _remove_section(self, typ, num):
        sec = '%s.%s' % (typ, num)
        if not self._parser.has_section(sec):
            return True
        return self._parser.remove_section(sec)

    def remove_mds_header(self):
        if not self._parser.has_section('mds'):
            return True
        return self._parser.remove_section('mds')

    def remove_osd(self, osd_id):
        return self._remove_section('osd', osd_id)

    def remove_mon(self, mon_id):
        return self._remove_section('mon', mon_id)

    def remove_mds(self, mds_id):
        return self._remove_section('mds', mds_id)

    def add_rgw(self, rgw_sec, host, keyring, rsp, log_file, rgw_frontends):
        if self._parser.has_section(rgw_sec):
            self._parser.remove_section(rgw_sec)
        self._parser.add_section(rgw_sec)
        self._parser.set(rgw_sec, "host", host)
        self._parser.set(rgw_sec, "keyring", keyring)
        self._parser.set(rgw_sec, "rgw socket path", rsp)
        self._parser.set(rgw_sec, "log file", log_file)
        self._parser.set(rgw_sec, "rgw frontends", rgw_frontends)
