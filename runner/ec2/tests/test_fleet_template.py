"""A fleet member is built as a single box is (`fleet_template.py`): the fleet's
launch template carries runner.cfn.yaml's own cloud-init, with its name read from
the instance at boot and box.env saying it is a member — and nothing else changed."""
from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import fleet_template  # noqa: E402


@pytest.fixture
def rendered():
    return fleet_template.render((HERE / "fleet.cfn.yaml").read_text(), (HERE / "runner.cfn.yaml").read_text())


def test_the_members_cloud_init_is_the_boxs_own(rendered):
    box = fleet_template.runner_user_data((HERE / "runner.cfn.yaml").read_text())
    for line in box:
        if line.strip() and line.strip() != fleet_template.NAME_LINE:
            assert line.strip() in rendered, line


def test_a_member_is_named_by_its_instance_and_knows_its_group(rendered):
    assert "RUNNER_NAME=${FleetName}-$(" in rendered and "meta-data/instance-id" in rendered
    for line in fleet_template.FLEET_LINES:
        assert line in rendered
    assert "${RunnerName}" not in rendered and fleet_template.PLACEHOLDER not in rendered


def test_the_rendered_user_data_fits_ec2s_cap(rendered):
    start = rendered.index("#cloud-config")
    body = rendered[start:rendered.index("- GroupName:", start)]
    # Indentation is YAML's, not the user data's; strip it before measuring.
    assert len("\n".join(line.strip() for line in body.splitlines()).encode()) < 16 * 1024


def test_a_box_template_without_its_name_line_is_refused():
    with pytest.raises(SystemExit):
        fleet_template.member_user_data(["#cloud-config", "RUNNER_WORKSPACE=x"])


def test_the_group_launches_only_in_the_zones_it_is_given(rendered):
    assert "AvailabilityZones: !If [HasZones, !Ref Zones, !GetAZs '']" in rendered
    assert "HasZones:" in rendered


def test_the_warm_pool_holds_exactly_its_size(rendered):
    # Uncapped, AWS prepares MaxSize - desired: a new, empty group's pool would launch
    # instances the stack waits on before the fleet even exists.
    assert "MaxGroupPreparedCapacity: !Ref WarmPoolSize" in rendered
