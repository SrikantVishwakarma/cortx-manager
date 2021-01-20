# CORTX-CSM: CORTX Management web and CLI interface.
# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.

from cortx.utils.conf_store.conf_store import Conf
from csm.conf.salt import SaltWrappers
from csm.core.blogic import const

from ipaddress import ip_address
from pathlib import Path
from pwd import getpwnam
from shutil import copyfile, rmtree
from tempfile import NamedTemporaryFile

import json
import os


class UDSConfigGenerator:
    HAPROXY_BEGIN_UDS = '\n# BEGIN UDS\n'
    HAPROXY_END_UDS = '\n# END UDS\n'
    HAPROXY_UDS_WARNING = """\
# The following HAproxy config entries, as well as the ``# BEGIN UDS`` and
# ``# END UDS`` comment lines surrounding it, were automatically generated by
# ``csm_setup``. Please *do not edit these manually*.
# Only a single occurrence of an UDS-related config block is supported by
# ``csm_setup``.
"""
    HAPROXY_CONFIG_PATH = '/etc/haproxy/haproxy.cfg'
    HAPROXY_TEMP_CONFIG_PREFIX = 'haproxy.cfg.'
    UDS_HOME_DIR = '/var/lib/uds'
    UDS_CONFIG_DIR = f'{UDS_HOME_DIR}/.uds'
    UDS_CONFIG_PATH = f'{UDS_CONFIG_DIR}/uds-config.json'
    UDS_USERNAME = 'uds'

    @classmethod
    def generate_csm_config(cls, uds_public_ip=None):
        return {
            'UDS.public_ip': f'{uds_public_ip}',
        }

    @staticmethod
    def write_csm_config(entries):
        for k, v in entries.items():
            Conf.set(const.CSM_GLOBAL_INDEX, k, v)
        Conf.save(const.CSM_GLOBAL_INDEX)

    @classmethod
    def update_csm_config(cls, uds_public_ip):
        cls.remove_csm_config()
        if uds_public_ip is not None:
            entries = cls.generate_csm_config(uds_public_ip)
            cls.write_csm_config(entries)

    @classmethod
    def remove_csm_config(cls):
        keys = cls.generate_csm_config().keys()
        for key in keys:
            Conf.delete(const.CSM_GLOBAL_INDEX, key)
        Conf.save(const.CSM_GLOBAL_INDEX)

    @staticmethod
    def generate_haproxy_frontend_config():
        cluster_ip = SaltWrappers.get_salt_call('pillar.get', 'cluster:cluster_ip')
        ip_address(cluster_ip)
        return f"""\
frontend uds-frontend
    mode tcp
    option tcplog
    bind 127.0.0.1:5000
    bind ::1:5000
    bind {cluster_ip}:5000
    acl udsbackendacl dst_port 5000
    use_backend uds-backend if udsbackendacl\
"""

    @staticmethod
    def generate_haproxy_backend_config():
        minions = list(SaltWrappers.get_salt('grains.get', 'id').values())
        if len(minions) < 1:
            raise RuntimeError('No minions were found')
        minions.sort()
        backend_server_scheme = '    server uds-{} {}:5000 check'
        backend_servers = '\n'.join(
            backend_server_scheme.format(i, minion) for (i, minion) in enumerate(minions, start=1))
        return f"""\
backend uds-backend
    mode tcp
    balance static-rr
{backend_servers}\
"""

    @classmethod
    def generate_haproxy_config(cls):
        frontend_rules = cls.generate_haproxy_frontend_config()
        backend_rules = cls.generate_haproxy_backend_config()
        return f"""\
{frontend_rules}

{backend_rules}\
"""

    @staticmethod
    def generate_uds_config():
        minion_id = SaltWrappers.get_salt_call('grains.get', 'id')
        d = {
            "version": "2.0",
            "service_config": {
                "RESTServer": {
                    "host": f'{minion_id}',
                },
            },
        }
        body = json.dumps(d, indent=4)
        return body

    # TODO Refactor once we come up with a better solution to manage HAproxy config updates.
    @classmethod
    def write_haproxy_config(cls, config):
        old_umask = os.umask(0o077)
        try:
            with NamedTemporaryFile(prefix=cls.HAPROXY_TEMP_CONFIG_PREFIX, mode='w') as outfile:
                with open(cls.HAPROXY_CONFIG_PATH, 'r') as infile:
                    contents = infile.read()
                    begin_uds_pos = contents.find(cls.HAPROXY_BEGIN_UDS)
                    end_uds_pos = contents.find(cls.HAPROXY_END_UDS)
                    has_no_block = begin_uds_pos < 0 and end_uds_pos < 0
                    has_valid_block = (begin_uds_pos >= 0 and end_uds_pos >= 0
                        and begin_uds_pos + len(cls.HAPROXY_BEGIN_UDS) <= end_uds_pos)
                    if not (has_no_block or has_valid_block):
                        raise RuntimeError("Malformed UDS-related block in HAproxy config file")
                    infile.seek(0)
                    contents_before = infile.read(begin_uds_pos)
                    if has_valid_block:
                        after_end_uds_pos = end_uds_pos + len(cls.HAPROXY_END_UDS)
                        infile.seek(after_end_uds_pos)
                        contents_after = infile.read()
                        has_repeated_block_delimiters = (cls.HAPROXY_BEGIN_UDS in contents_after
                            or cls.HAPROXY_END_UDS in contents_after)
                        if has_repeated_block_delimiters:
                            raise RuntimeError(
                                "Repeated UDS-related block delimiters in HAproxy config file. "
                                "Please remove all repeated UDS-related entries (including dangling"
                                " delimiters if there are any) from the file before proceeding to"
                                " the config update.")
                    else:
                        contents_after = ''
                    outfile.write(contents_before)
                    if config is not None:
                        outfile.write(
                            f'{cls.HAPROXY_BEGIN_UDS}'
                            f'{cls.HAPROXY_UDS_WARNING}'
                            f'{config}'
                            f'{cls.HAPROXY_END_UDS}'
                        )
                    outfile.write(contents_after)
                    outfile.flush()
                copyfile(outfile.name, infile.name)
        finally:
            os.umask(old_umask)

    @classmethod
    def update_haproxy_config(cls):
        config = cls.generate_haproxy_config()
        cls.write_haproxy_config(config)

    @classmethod
    def remove_haproxy_config(cls):
        cls.write_haproxy_config(None)

    @classmethod
    def write_uds_config(cls, config):
        uds_pwnam = getpwnam(cls.UDS_USERNAME)
        uds_uid, uds_gid = uds_pwnam.pw_uid, uds_pwnam.pw_gid
        old_umask = os.umask(0o077)
        try:
            Path(cls.UDS_CONFIG_DIR).mkdir(exist_ok=True)
            os.chown(cls.UDS_CONFIG_DIR, uds_uid, uds_gid)
            with open(cls.UDS_CONFIG_PATH, 'w') as f:
                f.write(config)
            os.chown(cls.UDS_CONFIG_PATH, uds_uid, uds_gid)
        finally:
            os.umask(old_umask)

    @classmethod
    def update_uds_config(cls):
        config = cls.generate_uds_config()
        cls.write_uds_config(f'{config}\n')

    @classmethod
    def remove_uds_config(cls):
        rmtree(cls.UDS_CONFIG_DIR)

    @classmethod
    def apply(cls, uds_public_ip):
        cls.update_csm_config(uds_public_ip)
        if uds_public_ip is None:
            cls.update_haproxy_config()
            cls.update_uds_config()
        else:
            cls.remove_haproxy_config()
            cls.remove_uds_config()

    @classmethod
    def delete(cls):
        cls.remove_csm_config()
        cls.remove_haproxy_config()
        cls.remove_uds_config()
