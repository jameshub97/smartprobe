"""Reusable interactive prompt helpers for the CLI."""

import sys

import questionary

from simulation_service_tool.ui.styles import custom_style


def _prompt_image_override(config):
    default_repository = config.get('imageRepository', 'mcr.microsoft.com/playwright')
    default_tag = config.get('imageTag', 'v1.40.0-focal')

    repository = questionary.text(
        "Image repository:",
        default=default_repository,
        validate=lambda text: bool(text.strip()) or "Repository is required",
    ).ask()
    if repository is None:
        return False

    tag = questionary.text(
        "Image tag:",
        default=default_tag,
        validate=lambda text: bool(text.strip()) or "Tag is required",
    ).ask()
    if tag is None:
        return False

    config['imageRepository'] = repository.strip()
    config['imageTag'] = tag.strip()
    return True


def _prompt_text_value(prompt, default, validator_message):
    value = questionary.text(
        prompt,
        default=str(default),
        validate=lambda text: bool(text.strip()) or validator_message,
    ).ask()
    if value is None:
        return None
    return value.strip()


def _prompt_positive_int(prompt, default):
    value = questionary.text(
        prompt,
        default=str(default),
        validate=lambda text: text.isdigit() and int(text) > 0 or "Must be a positive number",
    ).ask()
    if value is None:
        return None
    return int(value)


def _prompt_non_negative_int(prompt, default):
    value = questionary.text(
        prompt,
        default=str(default),
        validate=lambda text: text.isdigit() or "Must be zero or a positive number",
    ).ask()
    if value is None:
        return None
    return int(value)


def _prompt_advanced_test_options(config):
    enable_advanced = questionary.confirm(
        "Configure advanced throughput options?",
        default=False,
        style=custom_style,
    ).ask()
    if not enable_advanced:
        return True

    workers = _prompt_positive_int("Workers per pod:", config.get('workers', 1))
    if workers is None:
        return False
    config['workers'] = workers

    replica_count = _prompt_positive_int("Replica count:", config.get('replicaCount', config.get('parallelism', 1)))
    if replica_count is None:
        return False
    config['replicaCount'] = replica_count

    shard_total = _prompt_positive_int("Shard total:", config.get('shardTotal', replica_count))
    if shard_total is None:
        return False
    config['shardTotal'] = shard_total

    resource_profile = questionary.select(
        "Resource profile:",
        choices=[
            questionary.Choice(title="Balanced defaults", value="balanced"),
            questionary.Choice(title="Throughput optimized (64Mi/50m requests)", value="throughput"),
            questionary.Choice(title="Manual resource tuning", value="manual"),
        ],
        style=custom_style,
    ).ask()
    if not resource_profile:
        return False

    if resource_profile == 'throughput':
        config.update({
            'requestMemory': '64Mi',
            'requestCpu': '50m',
            'limitMemory': '128Mi',
            'limitCpu': '200m',
        })
    elif resource_profile == 'manual':
        request_memory = _prompt_text_value("Resource request memory:", config.get('requestMemory', '64Mi'), "Memory request is required")
        if request_memory is None:
            return False
        request_cpu = _prompt_text_value("Resource request CPU:", config.get('requestCpu', '50m'), "CPU request is required")
        if request_cpu is None:
            return False
        limit_memory = _prompt_text_value("Resource limit memory:", config.get('limitMemory', '128Mi'), "Memory limit is required")
        if limit_memory is None:
            return False
        limit_cpu = _prompt_text_value("Resource limit CPU:", config.get('limitCpu', '200m'), "CPU limit is required")
        if limit_cpu is None:
            return False
        config.update({
            'requestMemory': request_memory,
            'requestCpu': request_cpu,
            'limitMemory': limit_memory,
            'limitCpu': limit_cpu,
        })

    backoff_limit = _prompt_non_negative_int("Backoff limit:", config.get('backoffLimit', 0))
    if backoff_limit is None:
        return False
    config['backoffLimit'] = backoff_limit

    ttl_seconds = _prompt_non_negative_int("TTL seconds after finished:", config.get('ttlSecondsAfterFinished', 0))
    if ttl_seconds is None:
        return False
    config['ttlSecondsAfterFinished'] = ttl_seconds

    wait_for_ready = questionary.confirm(
        "Wait for Helm resources to become ready before returning?",
        default=bool(config.get('wait', False)),
        style=custom_style,
    ).ask()
    if wait_for_ready is None:
        return False
    config['wait'] = wait_for_ready

    override_image = questionary.confirm(
        "Override container image?",
        default=bool(config.get('imageRepository') and config.get('imageTag')),
        style=custom_style,
    ).ask()
    if override_image is None:
        return False
    if override_image and not _prompt_image_override(config):
        return False

    override_command = questionary.confirm(
        "Override test command for throughput experiments?",
        default=bool(config.get('commandOverride')),
        style=custom_style,
    ).ask()
    if override_command is None:
        return False
    if override_command:
        command_override = _prompt_text_value(
            "Command override:",
            config.get('commandOverride', 'echo started && sleep 1 && echo done'),
            "Command override is required",
        )
        if command_override is None:
            return False
        config['commandOverride'] = command_override

    # Kueue workload queuing
    enable_kueue = questionary.confirm(
        "Enable Kueue workload queuing? (auto-detected if installed)",
        default=config.get('kueue', True),
        style=custom_style,
    ).ask()
    if enable_kueue is None:
        return False
    config['kueue'] = enable_kueue

    return True


def _prompt_go_back(title="Go back"):
    if not sys.stdin.isatty():
        return
    questionary.select(
        "Next step:",
        choices=[questionary.Choice(title=title, value="back")],
        style=custom_style,
    ).ask()