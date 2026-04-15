"""StatefulSet-to-Job guidance flows used by diagnostics."""

import json
from pathlib import Path
import re

import questionary

from simulation_service_tool.cli.prompts import _prompt_go_back
from simulation_service_tool.services.command_runner import run_cli_command
from simulation_service_tool.ui.styles import custom_style


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HELM_FIXES_DIR = PROJECT_ROOT / 'helm-fixes'


def _run_command(args, timeout=None):
    return run_cli_command(args, timeout=timeout)


def _safe_name(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', (value or 'unknown').strip())


def _write_text_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (content or '').rstrip()
    path.write_text(f"{text}\n" if text else '', encoding='utf-8')
    return path


def _kubectl_get_json(resource_type, resource_name):
    result = _run_command(["kubectl", "get", resource_type, resource_name, "-o", "json"])
    if result.returncode != 0 or not result.stdout.strip():
        return None, result.stderr.strip() or f"{resource_type}/{resource_name} not found"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid kubectl JSON for {resource_type}/{resource_name}: {exc}"


def _parse_release_value(values_text, key, default):
    match = re.search(rf'^{re.escape(key)}:\s*(.+)$', values_text, re.MULTILINE)
    if not match:
        return default
    value = match.group(1).strip().strip('"').strip("'")
    return value or default


def _parse_release_image(values_text):
    repository_match = re.search(r'^image:\s*$[\s\S]*?^\s{2}repository:\s*(.+)$', values_text, re.MULTILINE)
    tag_match = re.search(r'^image:\s*$[\s\S]*?^\s{2}tag:\s*(.+)$', values_text, re.MULTILINE)
    repository = repository_match.group(1).strip().strip('"').strip("'") if repository_match else 'mcr.microsoft.com/playwright'
    tag = tag_match.group(1).strip().strip('"').strip("'") if tag_match else 'v1.40.0-focal'
    return repository, tag


def _build_job_yaml(release_name):
    values_result = _run_command(['helm', 'get', 'values', release_name, '-o', 'yaml'])
    values_text = values_result.stdout if values_result.returncode == 0 else ''

    completions = _parse_release_value(values_text, 'completions', '100')
    parallelism = _parse_release_value(values_text, 'parallelism', '20')
    backoff_limit = _parse_release_value(values_text, 'backoffLimit', '2')
    shard_total = _parse_release_value(values_text, 'shardTotal', '10')
    workers_per_pod = _parse_release_value(values_text, 'workersPerPod', '1')
    persona = _parse_release_value(values_text, 'persona', 'default')
    backend_api = _parse_release_value(values_text, 'backendApi', 'http://backend:5001/api/simulation/results')
    ttl_seconds = _parse_release_value(values_text, 'ttlSecondsAfterFinished', '60')
    repository, tag = _parse_release_image(values_text)

    return (
        'apiVersion: batch/v1\n'
        'kind: Job\n'
        'metadata:\n'
        f'  name: {release_name}-job\n'
        '  labels:\n'
        '    app: playwright-agent\n'
        f'    release: {release_name}\n'
        'spec:\n'
        '  completionMode: Indexed\n'
        f'  completions: {completions}\n'
        f'  parallelism: {parallelism}\n'
        f'  backoffLimit: {backoff_limit}\n'
        f'  ttlSecondsAfterFinished: {ttl_seconds}\n'
        '  template:\n'
        '    metadata:\n'
        '      labels:\n'
        '        app: playwright-agent\n'
        f'        release: {release_name}\n'
        '    spec:\n'
        '      restartPolicy: Never\n'
        '      containers:\n'
        '      - name: playwright-agent\n'
        f'        image: "{repository}:{tag}"\n'
        '        command: ["sh", "-c"]\n'
        '        args:\n'
        '          - |\n'
        '            SHARD_NUM=$((JOB_COMPLETION_INDEX + 1))\n'
        f'            npx playwright test --workers={workers_per_pod} --shard=$SHARD_NUM/{shard_total}\n'
        '        env:\n'
        '        - name: AGENT_PERSONA\n'
        f'          value: "{persona}"\n'
        '        - name: BACKEND_API\n'
        f'          value: "{backend_api}"\n'
    )


def _save_job_yaml_to_file(release_name, yaml_text):
    output_path = HELM_FIXES_DIR / f'{_safe_name(release_name)}-job.yaml'
    _write_text_file(output_path, yaml_text)
    return output_path


def _apply_job_yaml(release_name, yaml_text):
    output_path = _save_job_yaml_to_file(release_name, yaml_text)
    apply_result = _run_command(['kubectl', 'apply', '-f', str(output_path)])
    return output_path, apply_result


def _cleanup_legacy_statefulset_resources(release_name):
    job_name = f'{release_name}-job'
    job_resource, _ = _kubectl_get_json('job', job_name)
    if not job_resource:
        return {
            'error': f"Job '{job_name}' was not found. Apply or create the Job before cleaning up the old StatefulSet resources.",
        }

    confirm = questionary.confirm(
        f"Mark this as resolved by deleting the old StatefulSet resources for release '{release_name}'?",
        default=False,
        style=custom_style,
    ).ask()
    if not confirm:
        return {'cancelled': True}

    actions = []
    for kind, name in [
        ('statefulset', 'playwright-agent'),
        ('pod', 'playwright-agent-0'),
        ('pdb', 'playwright-agent-pdb'),
    ]:
        result = _run_command(['kubectl', 'delete', kind, name, '--ignore-not-found'])
        actions.append({
            'kind': kind,
            'name': name,
            'output': (result.stdout or result.stderr or '').strip(),
            'success': result.returncode == 0,
        })

    return {
        'job_name': job_name,
        'actions': actions,
    }


def _show_resource_type_comparison():
    details = (
        'Resource Type Comparison\n\n'
        'StatefulSet      | Job\n'
        'Purpose: long-running service | one-time batch workload\n'
        'Restart: restarted on exit    | completes and stays finished\n'
        'Cleanup: manual               | TTL auto-cleanup supported\n'
        'Best for: stateful systems    | tests, batch jobs\n\n'
        'Job is the better fit for one-shot Playwright agents.'
    )
    _print_text_block('Resource type comparison', details, max_lines=40)


def _show_job_yaml_guidance(release_name):
    yaml_text = _build_job_yaml(release_name)
    details = (
        'Recommended direction: use a Kubernetes Job for test agents.\n\n'
        'Why this is better:\n'
        'StatefulSet      | Job\n'
        'Long-running     | One-time batch\n'
        'Restart on exit  | Complete on exit\n'
        'Manual cleanup   | TTL auto-cleanup\n\n'
        'Example shape:\n'
        '```yaml\n'
        f'{yaml_text}'
        '```\n\n'
        'This matches one-shot test execution better than a StatefulSet.\n\n'
        'Recommended next step: apply the Job, then clean up the old StatefulSet resources.'
    )
    _print_text_block('Job YAML guidance', details, max_lines=120, prompt_after=False)

    action = questionary.select(
        'Next step:',
        choices=[
            questionary.Choice(title='Apply Job now and clean up old StatefulSet (recommended)', value='apply_and_resolve'),
            questionary.Choice(title='Apply this Job now (kubectl apply)', value='apply_job_yaml'),
            questionary.Choice(title='Save this Job YAML to file', value='save_job_yaml'),
            questionary.Choice(title='Mark as resolved (clean up old StatefulSet)', value='mark_resolved'),
            questionary.Choice(title='Learn more about Jobs vs StatefulSets', value='learn_more'),
            questionary.Choice(title='Back to diagnostic menu', value='back'),
        ],
        style=custom_style,
    ).ask()

    if action == 'save_job_yaml':
        output_path = _save_job_yaml_to_file(release_name, yaml_text)
        _print_text_block('Job YAML saved', f'Saved Job YAML to {output_path}', max_lines=20)
        return
    if action == 'apply_and_resolve':
        output_path, apply_result = _apply_job_yaml(release_name, yaml_text)
        if apply_result.returncode != 0:
            output = apply_result.stdout or apply_result.stderr or f'Applied from {output_path}'
            _print_text_block('Job YAML apply result', output, max_lines=40)
            return
        cleanup_result = _cleanup_legacy_statefulset_resources(release_name)
        if cleanup_result.get('cancelled'):
            _print_text_block('Job YAML apply result', f'Applied from {output_path}\n\nLegacy StatefulSet cleanup was cancelled.', max_lines=40)
            return
        if cleanup_result.get('error'):
            _print_text_block('Cleanup blocked', cleanup_result['error'], max_lines=20)
            return
        lines = [
            f'Applied from {output_path}',
            '',
            f"Verified Job: {cleanup_result['job_name']}",
            '',
            'Cleanup actions:',
        ]
        for item in cleanup_result.get('actions', []):
            status = 'OK' if item.get('success') else 'WARN'
            message = item.get('output') or 'no output'
            lines.append(f"[{status}] {item['kind']}/{item['name']}: {message}")
        _print_text_block('Job apply and legacy cleanup', '\n'.join(lines), max_lines=60)
        return
    if action == 'apply_job_yaml':
        output_path, apply_result = _apply_job_yaml(release_name, yaml_text)
        output = apply_result.stdout or apply_result.stderr or f'Applied from {output_path}'
        _print_text_block('Job YAML apply result', output, max_lines=40)
        return
    if action == 'mark_resolved':
        cleanup_result = _cleanup_legacy_statefulset_resources(release_name)
        if cleanup_result.get('cancelled'):
            return
        if cleanup_result.get('error'):
            _print_text_block('Cleanup blocked', cleanup_result['error'], max_lines=20)
            return
        lines = [f"Verified Job: {cleanup_result['job_name']}", '', 'Cleanup actions:']
        for item in cleanup_result.get('actions', []):
            status = 'OK' if item.get('success') else 'WARN'
            message = item.get('output') or 'no output'
            lines.append(f"[{status}] {item['kind']}/{item['name']}: {message}")
        _print_text_block('Legacy StatefulSet cleanup', '\n'.join(lines), max_lines=40)
        return
    if action == 'learn_more':
        _show_resource_type_comparison()
        return


def _show_statefulset_keepalive_guidance():
    details = (
        'Workaround if you must keep the StatefulSet: prevent the container from exiting immediately after the test.\n\n'
        'Example shape:\n'
        '```yaml\n'
        'spec:\n'
        '  template:\n'
        '    spec:\n'
        '      restartPolicy: Always\n'
        '      containers:\n'
        '      - command: ["sh", "-c"]\n'
        '        args:\n'
        '          - |\n'
        '            SHARD_NUM=$(($(echo $HOSTNAME | awk -F\'-\' \'{print $NF}\') + 1))\n'
        '            npx playwright test --shard=$SHARD_NUM/10\n'
        '            sleep infinity\n'
        '```\n\n'
        'This avoids restart loops, but it is still a workaround. A Job is the cleaner resource type for one-time tests.'
    )
    _print_text_block('StatefulSet keep-alive workaround', details, max_lines=80)


def _print_text_block(title, text, max_lines=120, prompt_after=True):
    print(f"\n[DIAG] {title}\n")
    lines = [line.rstrip() for line in (text or '').splitlines()]
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
        print(f"[INFO] Showing the last {max_lines} lines.\n")
    if lines:
        for line in lines:
            print(line)
    else:
        print("[WARN] No output available.")
    print()
    if prompt_after:
        _prompt_go_back("Return to diagnostic actions")