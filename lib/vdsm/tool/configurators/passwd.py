# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import absolute_import
from __future__ import division

import io

from vdsm.common import cmdutils
from vdsm.common import commands
from vdsm.common.libvirtconnection import libvirt_password, SASL_USERNAME

from . import YES, NO, MAYBE


_SASLDBLISTUSERS2 = cmdutils.CommandPath("sasldblistusers2",
                                         "/usr/sbin/sasldblistusers2",
                                         )
_LIBVIRT_SASLDB = "/etc/libvirt/passwd.db"
_SASL2_CONF = "/etc/sasl2/libvirt.conf"
_SASLPASSWD2 = cmdutils.CommandPath("saslpasswd2",
                                    "/usr/sbin/saslpasswd2",
                                    )


def isconfigured():
    ret = passwd_isconfigured()
    if ret == NO:
        return ret
    return libvirt_sasl_isconfigured()


def libvirt_sasl_isconfigured():
    with io.open(_SASL2_CONF, 'r', encoding='utf8') as f:
        lines = f.readlines()
        # check for new default configuration - since libvirt 3.2
        if 'mech_list: gssapi\n' in lines:
            return NO
        if 'mech_list: scram-sha-256\n' not in lines:
            return NO
    return MAYBE


def passwd_isconfigured():
    try:
        out = commands.run([
            _SASLDBLISTUSERS2.cmd,
            '-f',
            _LIBVIRT_SASLDB
        ])
        username = SASL_USERNAME.encode("utf-8")
        for user in out.splitlines():
            if username in user:
                return YES
    # TODO: why errors here were always ignored?
    except cmdutils.Error:
        pass
    return NO


def configure():
    configure_libvirt_sasl()
    configure_passwd()


def removeConf():
    if passwd_isconfigured() == YES:
        try:
            commands.run([
                _SASLPASSWD2.cmd,
                '-p',
                '-a',
                'libvirt',
                '-d',
                SASL_USERNAME
            ])
        except cmdutils.Error as e:
            raise RuntimeError("Remove password failed: {}".format(e))


def configure_libvirt_sasl():
    with io.open(_SASL2_CONF, 'w', encoding='utf8') as f:
        f.writelines([u'## start vdsm-4.50.0 configuration\n',
                      u'mech_list: scram-sha-256\n',
                      u'sasldb_path: %s\n' % (_LIBVIRT_SASLDB),
                      u'## end vdsm configuration']
                     )


def configure_passwd():
    args = [
        _SASLPASSWD2.cmd,
        '-p',
        '-a',
        'libvirt',
        SASL_USERNAME
    ]

    password = libvirt_password().encode("utf-8")
    try:
        commands.run(args, input=password)
    except cmdutils.Error as e:
        raise RuntimeError("Set password failed: {}".format(e))
