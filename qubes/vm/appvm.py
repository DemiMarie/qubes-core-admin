#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2014-2016  Wojtek Porczyk <woju@invisiblethingslab.com>
# Copyright (C) 2016       Marek Marczykowski <marmarek@invisiblethingslab.com>)
# Copyright (C) 2016       Bahtiar `kalkin-` Gadimov <bahtiar@gadimov.de>
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
""" This module contains the AppVM implementation """

import copy

import qubes.events
import qubes.vm.qubesvm
import qubes.vm.mix.dvmtemplate
from qubes.config import defaults


def template_changed_update_storage(self):
    """Update storage configuration for TemplateVM changes"""
    for volume_name, conf in self.default_volume_config.items():
        if (
            conf.get("snap_on_start", False)
            and conf.get("source", None) is None
        ):
            config = conf.copy()
            self.volume_config[volume_name] = config
            self.storage.init_volume(volume_name, config)


class AppVM(
    qubes.vm.mix.dvmtemplate.DVMTemplateMixin, qubes.vm.qubesvm.QubesVM
):
    """Application VM"""

    template = qubes.VMProperty(
        "template",
        load_stage=4,
        vmclass=qubes.vm.templatevm.TemplateVM,
        doc="Template, on which this AppVM is based.",
    )

    default_volume_config = {
        "root": {
            "name": "root",
            "snap_on_start": True,
            "save_on_stop": False,
            "rw": True,
            "source": None,
        },
        "private": {
            "name": "private",
            "snap_on_start": False,
            "save_on_stop": True,
            "rw": True,
            "size": defaults["private_img_size"],
        },
        "volatile": {
            "name": "volatile",
            "snap_on_start": False,
            "save_on_stop": False,
            "size": defaults["root_img_size"],
            "rw": True,
        },
        "kernel": {
            "name": "kernel",
            "snap_on_start": False,
            "save_on_stop": False,
            "rw": False,
        },
    }

    def __init__(self, app, xml, **kwargs):
        # migrate renamed properties
        if xml is not None:
            node_dispvm_allowed = xml.find(
                "./properties/property[@name='dispvm_allowed']"
            )
            if node_dispvm_allowed is not None:
                kwargs["template_for_dispvms"] = qubes.property.bool(
                    None, None, node_dispvm_allowed.text
                )
                node_dispvm_allowed.getparent().remove(node_dispvm_allowed)

        self.volume_config = copy.deepcopy(self.default_volume_config)
        template = kwargs.get("template", None)

        if template is not None:
            # template is only passed if the AppVM is created, in other cases we
            # don't need to patch the volume_config because the config is
            # coming from XML, already as we need it
            for name, config in template.volume_config.items():
                # in case the template vm has more volumes add them to own
                # config
                if name not in self.volume_config:
                    self.volume_config[name] = config.copy()
                    if "vid" in self.volume_config[name]:
                        del self.volume_config[name]["vid"]

        super().__init__(app, xml, **kwargs)

    @qubes.stateless_property
    def icon(self):
        if self.template_for_dispvms:
            return "templatevm-" + self.label.name
        # multi-inheritance and properties confuses pylint here
        # pylint: disable=no-member
        return super().icon

    @qubes.events.handler("domain-load")
    def on_domain_loaded(self, event):
        """When domain is loaded assert that this vm has a template."""  # pylint: disable=unused-argument
        assert self.template

    @qubes.events.handler("property-pre-reset:template")
    def on_property_pre_reset_template(self, event, name, oldvalue=None):
        """Forbid deleting template of running VM"""  # pylint: disable=unused-argument
        default_template = getattr(self.app, "default_template", None)
        if default_template is not None:
            raise qubes.exc.QubesValueError(
                "Cannot unset template. "
                "You can set it to the current default "
                f"({default_template.name}) instead"
            )
        raise qubes.exc.QubesValueError("Cannot unset template")

    @qubes.events.handler("property-pre-set:template")
    def on_property_pre_set_template(
        self, event, name, newvalue, oldvalue=None
    ):
        """Forbid changing template of running VM"""  # pylint: disable=unused-argument
        if not self.is_halted():
            raise qubes.exc.QubesVMNotHaltedError(
                self, "Cannot change template while qube is running"
            )

    @qubes.events.handler("property-set:template")
    def on_property_set_template(self, event, name, newvalue, oldvalue=None):
        """Adjust root (and possibly other snap_on_start=True) volume
        on template change.
        """  # pylint: disable=unused-argument
        template_changed_update_storage(self)
        for vm in self.dispvms:
            vm.on_property_set_template(event, name, newvalue, oldvalue)
