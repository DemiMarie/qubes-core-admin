#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2016  Marek Marczykowski-Górecki
#                                   <marmarek@invisiblethingslab.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.
#

""" Qubes PCI Extensions """

import functools
import os
import re
import string
import subprocess
from typing import Optional, List, Dict

import libvirt
import lxml
import lxml.etree

import qubes.device_protocol
import qubes.devices
import qubes.ext

#: cache of PCI device classes
pci_classes = None


#: emit warning on unsupported device only once
unsupported_devices_warned = set()


class UnsupportedDevice(Exception):
    pass


def load_pci_classes():
    """ List of known device classes, subclasses and programming interfaces. """
    # Syntax:
    # C class       class_name
    #       subclass        subclass_name           <-- single tab
    #               prog-if  prog-if_name   <-- two tabs
    result = {}
    with open('/usr/share/hwdata/pci.ids',
              encoding='utf-8', errors='ignore') as pciids:
        class_id = None
        subclass_id = None
        for line in pciids.readlines():
            line = line.rstrip()
            if line.startswith('\t\t') and class_id and subclass_id:
                (progif_id, _, class_name) = line[2:].split(' ', 2)
                result[class_id + subclass_id + progif_id] = \
                    class_name
            elif line.startswith('\t') and class_id:
                (subclass_id, _, class_name) = line[1:].split(' ', 2)
                # store both prog-if specific entry and generic one
                result[class_id + subclass_id + '00'] = \
                    class_name
                result[class_id + subclass_id] = \
                    class_name
            elif line.startswith('C '):
                (_, class_id, _, class_name) = line.split(' ', 3)
                result[class_id + '0000'] = class_name
                result[class_id + '00'] = class_name
                subclass_id = None

    return result


def pcidev_class(dev_xmldesc):
    sysfs_path = dev_xmldesc.findtext('path')
    assert sysfs_path
    try:
        with open(sysfs_path + '/class', encoding='ascii') as f_class:
            class_id = f_class.read().strip()
    except OSError:
        return "unknown"

    if not qubes.ext.pci.pci_classes:
        qubes.ext.pci.pci_classes = load_pci_classes()
    if class_id.startswith('0x'):
        class_id = class_id[2:]
    try:
        # ignore prog-if
        return qubes.ext.pci.pci_classes[class_id[0:4]]
    except KeyError:
        return "unknown"


def pcidev_interface(dev_xmldesc):
    sysfs_path = dev_xmldesc.findtext('path')
    assert sysfs_path
    try:
        with open(sysfs_path + '/class', encoding='ascii') as f_class:
            class_id = f_class.read().strip()
    except OSError:
        return "000000"

    if class_id.startswith('0x'):
        class_id = class_id[2:]
    return class_id


def attached_devices(app):
    """Return map device->domain-name for all currently attached devices"""

    # Libvirt do not expose nice API to query where the device is
    # attached. The only way would be to query _all_ the domains (
    # each with separate libvirt call) and look if the device is
    # there. Horrible waste of resources.
    # Instead, do this on much lower level - xenstore info for
    # xen-pciback driver, where we get all the info at once

    xs = app.vmm.xs
    devices = {}
    for domid in xs.ls('', 'backend/pci') or []:
        for devid in xs.ls('', 'backend/pci/' + domid) or []:
            devpath = 'backend/pci/' + domid + '/' + devid
            domain_name = xs.read('', devpath + '/domain')
            try:
                domain = app.domains[domain_name]
            except KeyError:
                # unknown domain - maybe from another qubes.xml?
                continue
            devnum = xs.read('', devpath + '/num_devs')
            for dev in range(int(devnum)):
                dbdf = xs.read('', devpath + '/dev-' + str(dev))
                bdf = dbdf[len('0000:'):]
                devices[bdf.replace(':', '_')] = domain

    return devices


def _device_desc(hostdev_xml):
    return '{devclass}: {vendor} {product}'.format(
        devclass=pcidev_class(hostdev_xml),
        vendor=hostdev_xml.findtext('capability/vendor'),
        product=hostdev_xml.findtext('capability/product'),
    )


class PCIDevice(qubes.device_protocol.DeviceInfo):
    # pylint: disable=too-few-public-methods
    regex = re.compile(
        r'\A(?P<bus>[0-9a-f]+)_(?P<device>[0-9a-f]+)\.'
        r'(?P<function>[0-9a-f]+)\Z')
    _libvirt_regex = re.compile(
        r'\Apci_0000_(?P<bus>[0-9a-f]+)_(?P<device>[0-9a-f]+)_'
        r'(?P<function>[0-9a-f]+)\Z')

    def __init__(self, backend_domain, ident, libvirt_name=None):
        if libvirt_name:
            dev_match = self._libvirt_regex.match(libvirt_name)
            if not dev_match:
                raise UnsupportedDevice(libvirt_name)
            ident = '{bus}_{device}.{function}'.format(**dev_match.groupdict())

        super().__init__(
            backend_domain=backend_domain, ident=ident, devclass="pci")

        if hasattr(self, 'regex'):
            # pylint: disable=no-member
            dev_match = self.regex.match(ident)
            if not dev_match:
                raise ValueError('Invalid device identifier: {!r}'.format(
                    ident))

            for group in self.regex.groupindex:
                setattr(self, group, dev_match.group(group))

        # lazy loading
        self._description = None
        self._vendor_id = None
        self._product_id = None

    @property
    def vendor(self) -> str:
        """
        Device vendor from local database `/usr/share/hwdata/pci.ids`

        Could be empty string or "unknown".

        Lazy loaded.
        """
        if self._vendor is None:
            result = self._load_desc()["vendor"]
        else:
            result = self._vendor
        return result

    @property
    def product(self) -> str:
        """
        Device name from local database `/usr/share/hwdata/usb.ids`

        Could be empty string or "unknown".

        Lazy loaded.
        """
        if self._product is None:
            result = self._load_desc()["product"]
        else:
            result = self._product
        return result

    @property
    def interfaces(self) -> List[qubes.device_protocol.DeviceInterface]:
        """
        List of device interfaces.

        Every device should have at least one interface.
        """
        if self._interfaces is None:
            hostdev_details = \
                self.backend_domain.app.vmm.libvirt_conn.nodeDeviceLookupByName(
                    self.libvirt_name
                )
            interface_encoding = pcidev_interface(lxml.etree.fromstring(
                hostdev_details.XMLDesc()))
            self._interfaces = [qubes.device_protocol.DeviceInterface(
                interface_encoding, devclass='pci')]
        return self._interfaces

    @property
    def parent_device(self) -> Optional[qubes.device_protocol.DeviceInfo]:
        """
        The parent device if any.

        PCI device has no parents.
        """
        return None

    @property
    def libvirt_name(self):
        # pylint: disable=no-member
        # noinspection PyUnresolvedReferences
        return f'pci_0000_{self.bus}_{self.device}_{self.function}'

    @property
    def description(self):
        if self._description is None:
            hostdev_details = \
                self.backend_domain.app.vmm.libvirt_conn.nodeDeviceLookupByName(
                    self.libvirt_name
                )
            self._description = _device_desc(lxml.etree.fromstring(
                hostdev_details.XMLDesc()))
        return self._description

    @property
    def self_identity(self) -> str:
        """
        Get identification of device not related to port.
        """
        allowed_chars = string.digits + string.ascii_letters + '-_.'
        if self._vendor_id is None:
            vendor_id = self._load_desc()["vendor ID"]
            self._vendor_id = ''.join(
                c if c in set(allowed_chars) else '_' for c in vendor_id)
        if self._product_id is None:
            product_id = self._load_desc()["product ID"]
            self._product_id = ''.join(
                c if c in set(allowed_chars) else '_' for c in product_id)
        interfaces = ''.join(repr(ifc) for ifc in self.interfaces)
        serial = self._serial if self._serial else ""
        return \
            f'{self._vendor_id}:{self._product_id}:{serial}:{interfaces}'

    def _load_desc(self) -> Dict[str, str]:
        unknown = "unknown"
        result = {"vendor": unknown,
                  "vendor ID": "0000",
                  "product": unknown,
                  "product ID": "0000",
                  "manufacturer": unknown,
                  "name": unknown,
                  "serial": unknown}
        if not self.backend_domain.is_running():
            # don't cache these values
            return result
        hostdev_details = \
            self.backend_domain.app.vmm.libvirt_conn.nodeDeviceLookupByName(
                self.libvirt_name
            )

        # Data successfully loaded, cache these values
        hostdev_xml = lxml.etree.fromstring(hostdev_details.XMLDesc())
        self._vendor = result["vendor"] = hostdev_xml.findtext(
            'capability/vendor')
        self._vendor_id = result["vendor ID"] = hostdev_xml.xpath(
            "//vendor/@id")
        self._product = result["product"] = hostdev_xml.findtext(
            'capability/product')
        self._product_id = result["product ID"] = hostdev_xml.xpath(
            "//product/@id")
        return result

    @staticmethod
    def _sanitize(
            untrusted_device_desc: bytes,
            safe_chars: str =
            string.ascii_letters + string.digits + string.punctuation + ' '
    ) -> str:
        # b'USB\\x202.0\\x20Camera' -> 'USB 2.0 Camera'
        untrusted_device_desc = untrusted_device_desc.decode(
            'unicode_escape', errors='ignore')
        return ''.join(
            c if c in set(safe_chars) else '_' for c in untrusted_device_desc
        )

    @property
    def frontend_domain(self):
        # TODO: cache this
        all_attached = attached_devices(self.backend_domain.app)
        return all_attached.get(self.ident, None)


class PCIDeviceExtension(qubes.ext.Extension):
    def __init__(self):
        super().__init__()
        # lazy load this
        self.pci_classes = {}

    @qubes.ext.handler('device-list:pci')
    def on_device_list_pci(self, vm, event):
        # pylint: disable=unused-argument
        # only dom0 expose PCI devices
        if vm.qid != 0:
            return

        for dev in vm.app.vmm.libvirt_conn.listAllDevices():
            if 'pci' not in dev.listCaps():
                continue

            xml_desc = lxml.etree.fromstring(dev.XMLDesc())
            libvirt_name = xml_desc.findtext('name')
            try:
                yield PCIDevice(vm, None, libvirt_name=libvirt_name)
            except UnsupportedDevice:
                if libvirt_name not in unsupported_devices_warned:
                    vm.log.warning("Unsupported device: %s", libvirt_name)
                    unsupported_devices_warned.add(libvirt_name)

    @qubes.ext.handler('device-get:pci')
    def on_device_get_pci(self, vm, event, ident):
        # pylint: disable=unused-argument
        if not vm.app.vmm.offline_mode:
            yield _cache_get(vm, ident)

    @qubes.ext.handler('device-list-attached:pci')
    def on_device_list_attached(self, vm, event, **kwargs):
        # pylint: disable=unused-argument
        if not vm.is_running() or isinstance(vm, qubes.vm.adminvm.AdminVM):
            return
        xml_desc = lxml.etree.fromstring(vm.libvirt_domain.XMLDesc())

        for hostdev in xml_desc.findall('devices/hostdev'):
            if hostdev.get('type') != 'pci':
                continue
            address = hostdev.find('source/address')
            bus = address.get('bus')[2:]
            device = address.get('slot')[2:]
            function = address.get('function')[2:]

            ident = '{bus}_{device}.{function}'.format(
                bus=bus,
                device=device,
                function=function,
            )
            yield (PCIDevice(vm.app.domains[0], ident), {})

    @qubes.ext.handler('device-pre-attach:pci')
    def on_device_pre_attached_pci(self, vm, event, device, options):
        # pylint: disable=unused-argument
        if not os.path.exists('/sys/bus/pci/devices/0000:{}'.format(
                device.ident.replace('_', ':'))):
            raise qubes.exc.QubesException(
                'Invalid PCI device: {}'.format(device.ident))

        if isinstance(vm, qubes.vm.adminvm.AdminVM):
            raise qubes.exc.QubesException("Can't attach PCI device to dom0")

        if vm.virt_mode == 'pvh':
            raise qubes.exc.QubesException(
                "Can't attach PCI device to VM in pvh mode")

        if not vm.is_running():
            return

        try:
            device = _cache_get(device.backend_domain, device.ident)
            self.bind_pci_to_pciback(vm.app, device)
            vm.libvirt_domain.attachDevice(
                vm.app.env.get_template('libvirt/devices/pci.xml').render(
                    device=device, vm=vm, options=options,
                    power_mgmt=vm.app.domains[0].features.get(
                        'suspend-s0ix', False)))
        except subprocess.CalledProcessError as e:
            vm.log.exception('Failed to attach PCI device {!r} on the fly,'
                ' changes will be seen after VM restart.'.format(
                device.ident), e)

    @qubes.ext.handler('device-pre-detach:pci')
    def on_device_pre_detached_pci(self, vm, event, device):
        # pylint: disable=unused-argument
        if not vm.is_running():
            return

        # this cannot be converted to general API, because there is no
        # provision in libvirt for extracting device-side BDF; we need it for
        # qubes.DetachPciDevice, which unbinds driver, not to oops the kernel

        device = _cache_get(device.backend_domain, device.ident)
        with subprocess.Popen(['xl', 'pci-list', str(vm.xid)],
                stdout=subprocess.PIPE) as p:
            result = p.communicate()[0].decode()
        m = re.search(r'^(\d+.\d+)\s+0000:{}$'.format(device.ident.replace(
            '_', ':')),
            result,
            flags=re.MULTILINE)
        if not m:
            vm.log.error('Device %s already detached', device.ident)
            return
        vmdev = m.group(1)
        try:
            vm.run_service('qubes.DetachPciDevice',
                user='root', input='00:{}'.format(vmdev))
            vm.libvirt_domain.detachDevice(
                vm.app.env.get_template('libvirt/devices/pci.xml').render(
                    device=device, vm=vm,
                    power_mgmt=vm.app.domains[0].features.get(
                        'suspend-s0ix', False)))
        except (subprocess.CalledProcessError, libvirt.libvirtError) as e:
            vm.log.exception('Failed to detach PCI device {!r} on the fly,'
                ' changes will be seen after VM restart.'.format(
                device.ident), e)
            raise

    @qubes.ext.handler('domain-pre-start')
    def on_domain_pre_start(self, vm, _event, **_kwargs):
        # Bind pci devices to pciback driver
        for assignment in vm.devices['pci'].get_assigned_devices():
            device = _cache_get(assignment.backend_domain, assignment.ident)
            self.bind_pci_to_pciback(vm.app, device)

    @staticmethod
    def bind_pci_to_pciback(app, device):
        '''Bind PCI device to pciback driver.

        :param qubes.devices.PCIDevice device: device to attach

        Devices should be unbound from their normal kernel drivers and bound to
        the dummy driver, which allows for attaching them to a domain.
        '''
        try:
            node = app.vmm.libvirt_conn.nodeDeviceLookupByName(
                device.libvirt_name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_NODE_DEVICE:
                raise qubes.exc.QubesException(
                    'PCI device {!s} does not exist'.format(
                        device))
            raise

        try:
            node.dettach()
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR:
                # allreaddy dettached
                pass
            else:
                raise

    @qubes.ext.handler('qubes-close', system=True)
    def on_app_close(self, app, event):
        # pylint: disable=unused-argument
        _cache_get.cache_clear()


@functools.lru_cache(maxsize=None)
def _cache_get(vm, ident):
    ''' Caching wrapper around `PCIDevice(vm, ident)`. '''
    return PCIDevice(vm, ident)
