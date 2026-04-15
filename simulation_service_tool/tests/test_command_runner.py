import pytest

from simulation_service_tool.services.command_runner import build_cli_command, is_valid_k8s_name, run_cli_command


def test_build_kubectl_command_injects_namespace():
    assert build_cli_command(['kubectl', 'get', 'pods'], namespace='qa') == ['kubectl', '-n', 'qa', 'get', 'pods']


def test_build_kubectl_command_rejects_invalid_name():
    with pytest.raises(ValueError):
        build_cli_command(['kubectl', 'delete', 'pod', 'Bad_Name'], namespace='qa')


def test_is_valid_k8s_name_accepts_dns_style_names():
    assert is_valid_k8s_name('playwright-agent-0') is True
    assert is_valid_k8s_name('tes02389123') is True


def test_run_cli_command_rejects_invalid_helm_release_without_shelling_out():
    result = run_cli_command(['helm', 'status', 'Bad_Name'], namespace='qa')
    assert result.returncode == 2
    assert 'Invalid Helm release name' in result.stderr