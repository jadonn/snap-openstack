# Copyright (c) 2022 Canonical Ltd.
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

import ipaddress
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from snaphelpers import Snap

import sunbeam.jobs.questions
from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.commands.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.jobs.common import BaseStep, Result, ResultType, Status, run_plan
from sunbeam.jobs.juju import (
    CONTROLLER_MODEL,
    JujuHelper,
    ModelNotFoundException,
    run_sync,
)

CLOUD_CONFIG_SECTION = "CloudConfig"
LOG = logging.getLogger(__name__)
console = Console()


def user_questions():
    return {
        "run_demo_setup": sunbeam.jobs.questions.ConfirmQuestion(
            "Populate OpenStack cloud with demo user, default images, flavors etc",
            default_value=True,
        ),
        "username": sunbeam.jobs.questions.PromptQuestion(
            "Username to use for access to OpenStack", default_value="demo"
        ),
        "password": sunbeam.jobs.questions.PromptQuestion(
            "Password to use for access to OpenStack",
            default_function=utils.generate_password,
        ),
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            "Network range to use for project network", default_value="192.168.122.0/24"
        ),
        "security_group_rules": sunbeam.jobs.questions.ConfirmQuestion(
            "Setup security group rules for SSH and ICMP ingress", default_value=True
        ),
        "remote_access_location": sunbeam.jobs.questions.PromptQuestion(
            "Local or remote access to VMs",
            choices=[utils.LOCAL_ACCESS, utils.REMOTE_ACCESS],
            default_value=utils.LOCAL_ACCESS,
        ),
    }


def ext_net_questions():
    return {
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            "CIDR of network to use for external networking",
            default_value="10.20.20.0/24",
        ),
        "gateway": sunbeam.jobs.questions.PromptQuestion(
            "IP address of default gateway for external network", default_value=None
        ),
        "start": sunbeam.jobs.questions.PromptQuestion(
            "Start of IP allocation range for external network", default_value=None
        ),
        "end": sunbeam.jobs.questions.PromptQuestion(
            "End of IP allocation range for external network", default_value=None
        ),
        "network_type": sunbeam.jobs.questions.PromptQuestion(
            "Network type for access to external network",
            choices=["flat", "vlan"],
            default_value="flat",
        ),
        "segmentation_id": sunbeam.jobs.questions.PromptQuestion(
            "VLAN ID to use for external network", default_value=0
        ),
        "nic": sunbeam.jobs.questions.PromptQuestion(
            "Free network interface microstack can use for external traffic",
            choices=utils.get_free_nics(),
            default_value=utils.get_free_nic(),
        ),
    }


def ext_net_questions_local_only():
    return {
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            (
                "CIDR of OpenStack external network - arbitrary but must not "
                "be in use"
            ),
            default_value="10.20.20.0/24",
        ),
        "start": sunbeam.jobs.questions.PromptQuestion(
            "Start of IP allocation range for external network", default_value=None
        ),
        "end": sunbeam.jobs.questions.PromptQuestion(
            "End of IP allocation range for external network", default_value=None
        ),
        "network_type": sunbeam.jobs.questions.PromptQuestion(
            "Network type for access to external network",
            choices=["flat", "vlan"],
            default_value="flat",
        ),
        "segmentation_id": sunbeam.jobs.questions.PromptQuestion(
            "VLAN ID to use for external network", default_value=0
        ),
    }


VARIABLE_DEFAULTS = {
    "user": {
        "username": "demo",
        "cidr": "192.168.122.0/24",
        "security_group_rules": True,
    },
    "external_network": {
        "cidr": "10.20.20.0/24",
        "gateway": None,
        "start": None,
        "end": None,
        "physical_network": "physnet1",
        "network_type": "flat",
        "segmentation_id": 0,
    },
}


def _retrieve_admin_credentials(jhelper: JujuHelper, model: str) -> dict:
    """Retrieve cloud admin credentials.

    Retrieve cloud admin credentials from keystone and
    return as a dict suitable for use with subprocess
    commands.  Variables are prefixed with OS_.
    """
    app = "keystone"
    action_cmd = "get-admin-account"

    unit = run_sync(jhelper.get_leader_unit(app, model))
    if not unit:
        _message = f"Unable to get {app} leader"
        raise click.ClickException(_message)

    action_result = run_sync(jhelper.run_action(unit, model, action_cmd))
    if action_result.get("return-code", 0) > 1:
        _message = "Unable to retrieve openrc from Keystone service"
        raise click.ClickException(_message)

    return {
        "OS_USERNAME": action_result.get("username"),
        "OS_PASSWORD": action_result.get("password"),
        "OS_AUTH_URL": action_result.get("public-endpoint"),
        "OS_USER_DOMAIN_NAME": action_result.get("user-domain-name"),
        "OS_PROJECT_DOMAIN_NAME": action_result.get("project-domain-name"),
        "OS_PROJECT_NAME": action_result.get("project-name"),
        "OS_AUTH_VERSION": action_result.get("api-version"),
        "OS_IDENTITY_API_VERSION": action_result.get("api-version"),
    }


class SetHypervisorCharmConfigStep(BaseStep):
    """Update openstack-hypervisor charm config"""

    IPVANYNETWORK_UNSET = "0.0.0.0/0"

    def __init__(self, jhelper, ext_network: Path):
        super().__init__(
            "Update charm config",
            "Updating openstack-hypervisor charm configuration",
        )

        # File path with external_network details in json format
        self.ext_network_file = ext_network
        self.ext_network = {}
        self.client = Client()
        self.jhelper = jhelper
        self.charm_config = {}

    def has_prompts(self) -> bool:
        return False

    def run(self, status: Optional["Status"] = None) -> Result:
        """Run the step to completion.

        Invoked when the step is run and returns a ResultType to indicate

        :return:
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        self.ext_network = self.variables.get("external_network", {})
        self.charm_config["enable-gateway"] = str(
            self.variables["user"]["remote_access_location"] == utils.REMOTE_ACCESS
        )
        self.charm_config["external-bridge"] = "br-ex"
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            external_network = ipaddress.ip_network(
                self.variables["external_network"].get("cidr")
            )
            bridge_interface = (
                f"{self.ext_network.get('gateway')}/{external_network.prefixlen}"
            )
            self.charm_config["external-bridge-address"] = bridge_interface
        else:
            self.charm_config["external-bridge-address"] = self.IPVANYNETWORK_UNSET

        self.charm_config["physnet-name"] = self.variables["external_network"].get(
            "physical_network"
        )
        try:
            model = CONTROLLER_MODEL.split("/")[-1]
            LOG.debug(
                f"Config to apply on openstack-hypervisor snap: {self.charm_config}"
            )
            run_sync(
                self.jhelper.set_application_config(
                    model,
                    "openstack-hypervisor",
                    self.charm_config,
                )
            )
        except Exception as e:
            LOG.exception("Error setting config for openstack-hypervisor")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class UserOpenRCStep(BaseStep):
    """Generate openrc for created cloud user."""

    def __init__(self, auth_url: str, auth_version: str, openrc: str, clouds: str):
        super().__init__("Generate user openrc", "Generating openrc for cloud usage")
        self.auth_url = auth_url
        self.auth_version = auth_version
        self.openrc = openrc
        self.clouds = clouds
        self.client = Client()

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)

    def run(self, status: Optional["Status"] = None) -> Result:
        try:
            snap = Snap()
            terraform = str(snap.paths.snap / "bin" / "terraform")
            cmd = [terraform, "output", "-json"]
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=snap.paths.user_common / "etc" / "demo-setup",
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
            tf_output = json.loads(process.stdout)
            self._print_openrc(tf_output)
            self._print_clouds_yaml(tf_output)
            return Result(ResultType.COMPLETED)
        except subprocess.CalledProcessError as e:
            LOG.exception("Error initializing Terraform")
            return Result(ResultType.FAILED, str(e))

    def _print_openrc(self, tf_output: dict) -> None:
        """Print openrc to console and save to disk using provided information"""
        _openrc = f"""# openrc for {tf_output["OS_USERNAME"]["value"]}
export OS_AUTH_URL={self.auth_url}
export OS_USERNAME={tf_output["OS_USERNAME"]["value"]}
export OS_PASSWORD={tf_output["OS_PASSWORD"]["value"]}
export OS_USER_DOMAIN_NAME={tf_output["OS_USER_DOMAIN_NAME"]["value"]}
export OS_PROJECT_DOMAIN_NAME={tf_output["OS_PROJECT_DOMAIN_NAME"]["value"]}
export OS_PROJECT_NAME={tf_output["OS_PROJECT_NAME"]["value"]}
export OS_AUTH_VERSION={self.auth_version}
export OS_IDENTITY_API_VERSION={self.auth_version}"""
        if self.openrc:
            message = f"Writing openrc to {self.openrc} ... "
            console.status(message)
            with open(self.openrc, "w") as f_openrc:
                os.fchmod(f_openrc.fileno(), mode=0o640)
                f_openrc.write(_openrc)
            console.print(f"{message}[green]done[/green]")
        else:
            console.print(_openrc)

    def _print_clouds_yaml(self, tf_output: dict) -> None:
        """Print a clouds.yaml file and save to disk using provided information"""
        _cloudsyaml = f"""clouds:
  sunbeam:
    auth:
      auth_url: {self.auth_url}
      project_name: {tf_output["OS_PROJECT_NAME"]["value"]}
      username: {tf_output["OS_USERNAME"]["value"]}
      password: {tf_output["OS_PASSWORD"]["value"]}
      user_domain_name: {tf_output["OS_USER_DOMAIN_NAME"]["value"]}
      project_domain_name: {tf_output["OS_PROJECT_DOMAIN_NAME"]["value"]}
    identity_api_version: {self.auth_version}
"""
        if self.clouds:
            message = f"Writing clouds.yaml to {self.clouds} ... "
            console.status(message)
            with open(self.clouds, "w") as f_clouds:
                os.fchmod(f_clouds.fileno(), mode=0o640)
                f_clouds.write(_cloudsyaml)
        else:
            console.print(_cloudsyaml)

class UserQuestions(BaseStep):
    """Ask user configuration questions."""

    def __init__(
        self,
        answer_file: str,
        preseed_file: str = None,
        accept_defaults: bool = False,
    ):
        super().__init__("Ask configure questions", "Ask configure questions")
        self.accept_defaults = accept_defaults
        self.preseed_file = preseed_file
        self.client = Client()
        self.answer_file = answer_file

    def has_prompts(self) -> bool:
        return True

    def prompt(self, console: Optional[Console] = None) -> None:
        """Prompt the user for basic cloud configuration.

        Prompts the user for required information for cloud configuration.

        :param console: the console to prompt on
        :type console: rich.console.Console (Optional)
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        for section in ["user", "external_network"]:
            if not self.variables.get(section):
                self.variables[section] = {}
        if self.preseed_file:
            preseed = sunbeam.jobs.questions.read_preseed(self.preseed_file)
        else:
            preseed = {}
        user_bank = sunbeam.jobs.questions.QuestionBank(
            questions=user_questions(),
            console=console,
            preseed=preseed.get("user"),
            previous_answers=self.variables.get("user"),
            accept_defaults=self.accept_defaults,
        )
        self.variables["user"][
            "remote_access_location"
        ] = user_bank.remote_access_location.ask()
        # External Network Configuration
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions_local_only(),
                console=console,
                preseed=preseed.get("external_network"),
                previous_answers=self.variables.get("external_network"),
                accept_defaults=self.accept_defaults,
            )
        else:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions(),
                console=console,
                preseed=preseed.get("external_network"),
                previous_answers=self.variables.get("external_network"),
                accept_defaults=self.accept_defaults,
            )
        self.variables["external_network"]["cidr"] = ext_net_bank.cidr.ask()
        external_network = ipaddress.ip_network(
            self.variables["external_network"]["cidr"]
        )
        external_network_hosts = list(external_network.hosts())
        default_gateway = self.variables["external_network"].get("gateway") or str(
            external_network_hosts[0]
        )
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            self.variables["external_network"]["gateway"] = default_gateway
        else:
            self.variables["external_network"]["gateway"] = ext_net_bank.gateway.ask(
                new_default=default_gateway
            )
        self.variables["external_network"]["physical_network"] = VARIABLE_DEFAULTS[
            "external_network"
        ]["physical_network"]
        self.variables["user"]["run_demo_setup"] = user_bank.run_demo_setup.ask()
        if self.variables["user"]["run_demo_setup"]:
            # User configuration
            self.variables["user"]["username"] = user_bank.username.ask()
            self.variables["user"]["password"] = user_bank.password.ask()
            self.variables["user"]["cidr"] = user_bank.cidr.ask()
            self.variables["user"][
                "security_group_rules"
            ] = user_bank.security_group_rules.ask()
            default_allocation_range_start = self.variables["external_network"].get(
                "start"
            ) or str(external_network_hosts[1])
            self.variables["external_network"]["start"] = ext_net_bank.start.ask(
                new_default=default_allocation_range_start
            )
            default_allocation_range_end = self.variables["external_network"].get(
                "end"
            ) or str(external_network_hosts[-1])
            self.variables["external_network"]["end"] = ext_net_bank.end.ask(
                new_default=default_allocation_range_end
            )

            self.variables["external_network"][
                "network_type"
            ] = ext_net_bank.network_type.ask()
            if self.variables["external_network"]["network_type"] == "vlan":
                self.variables["external_network"][
                    "segmentation_id"
                ] = ext_net_bank.segmentation_id.ask()
            else:
                self.variables["external_network"]["segmentation_id"] = 0

        sunbeam.jobs.questions.write_answers(
            self.client, CLOUD_CONFIG_SECTION, self.variables
        )

    def run(self, status: Optional[Status] = None) -> Result:
        return Result(ResultType.COMPLETED)


class DemoSetup(BaseStep):
    """Default cloud configuration for all-in-one install."""

    def __init__(
        self,
        tfhelper: TerraformHelper,
        answer_file: str,
    ):
        super().__init__("Setup demo artifacts", "Setup demo artifacts")
        self.answer_file = answer_file
        self.tfhelper = tfhelper
        self.client = Client()

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        self.tfhelper.write_tfvars(self.variables, self.answer_file)
        try:
            self.tfhelper.apply()
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            LOG.exception("Error configuring cloud")
            return Result(ResultType.FAILED, str(e))


class TerraformDemoInitStep(TerraformInitStep):
    def __init__(
        self,
        tfhelper: TerraformHelper,
    ):
        super().__init__(tfhelper)
        self.tfhelper = tfhelper
        self.client = Client()

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)


class SetLocalHypervisorOptions(BaseStep):
    def __init__(
        self,
        name,
        jhelper,
        join_mode: bool = False,
    ):
        super().__init__(
            "Apply local hypervisor settings", "Apply local hypervisor settings"
        )
        self.name = name
        self.jhelper = jhelper
        self.join_mode = join_mode
        self.client = Client()

    def has_prompts(self) -> bool:
        return True

    def prompt(self, console: Optional[Console] = None) -> None:
        self.nic = None
        # If adding a node before configure step has run then answers will
        # not be populated yet.
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        remote_access_location = self.variables.get("user", {}).get(
            "remote_access_location"
        )
        # If adding new nodes to the cluster then local access makes no sense
        # so always prompt for the nic.
        if self.join_mode:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions(),
                console=console,
                preseed={},
                previous_answers={},
                accept_defaults=False,
            )
            self.nic = ext_net_bank.nic.ask()
        elif remote_access_location == utils.REMOTE_ACCESS:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions(),
                console=console,
                preseed={},
                previous_answers={},
                accept_defaults=False,
            )
            self.nic = ext_net_bank.nic.ask()

    def run(self, status: Optional[Status] = None) -> Result:
        if not self.nic:
            return Result(ResultType.COMPLETED)
        node = self.client.cluster.get_node_info(self.name)
        self.machine_id = str(node.get("machineid"))
        app = "openstack-hypervisor"
        action_cmd = "set-hypervisor-local-settings"
        model = CONTROLLER_MODEL.split("/")[-1]
        unit = run_sync(self.jhelper.get_unit_from_machine(app, self.machine_id, model))
        action_result = run_sync(
            self.jhelper.run_action(
                unit.entity_id,
                model,
                action_cmd,
                action_params={
                    "external-nic": self.nic,
                },
            )
        )

        if action_result.get("return-code", 0) > 1:
            _message = "Unable to set local hypervisor config"
            return Result(ResultType.FAILED, _message)
        return Result(ResultType.COMPLETED)


@click.command()
@click.option("-a", "--accept-defaults", help="Accept all defaults.", is_flag=True)
@click.option("-p", "--preseed", help="Preseed file.")
@click.option("-o", "--openrc", help="Output file for cloud access details.")
@click.option("-c", "--clouds", help="Output file for clouds.yaml cloud access details.")
def configure(
    openrc: str = None, preseed: str = None, accept_defaults: bool = False, clouds: str = None
) -> None:
    """Configure cloud with some sane defaults."""
    name = utils.get_fqdn()
    snap = Snap()
    src = snap.paths.snap / "etc" / "demo-setup/"
    dst = snap.paths.user_common / "etc" / "demo-setup"
    try:
        os.mkdir(dst)
    except FileExistsError:
        pass
    # NOTE: install to user writable location
    LOG.debug(f"Updating {dst} from {src}...")
    shutil.copytree(src, dst, dirs_exist_ok=True)

    data_location = snap.paths.user_data
    jhelper = JujuHelper(data_location)
    try:
        run_sync(jhelper.get_model(OPENSTACK_MODEL))
    except ModelNotFoundException:
        LOG.error(f"Expected model {OPENSTACK_MODEL} missing")
        raise click.ClickException(
            "Please run `openstack.sunbeam cluster bootstrap` first"
        )
    admin_credentials = _retrieve_admin_credentials(jhelper, OPENSTACK_MODEL)
    tfhelper = TerraformHelper(
        path=snap.paths.user_common / "etc" / "demo-setup",
        env=admin_credentials,
        plan="demo-setup",
        parallelism=1,
        backend="http",
        data_location=data_location,
    )
    answer_file = tfhelper.path / "config.auto.tfvars.json"
    plan = [
        UserQuestions(
            answer_file=answer_file,
            preseed_file=preseed,
            accept_defaults=accept_defaults,
        ),
        TerraformDemoInitStep(tfhelper),
        DemoSetup(
            tfhelper=tfhelper,
            answer_file=answer_file,
        ),
        UserOpenRCStep(
            auth_url=admin_credentials["OS_AUTH_URL"],
            auth_version=admin_credentials["OS_AUTH_VERSION"],
            openrc=openrc,
            clouds=clouds,
        ),
        SetHypervisorCharmConfigStep(jhelper, ext_network=answer_file),
        SetLocalHypervisorOptions(name, jhelper),
    ]
    run_plan(plan, console)
