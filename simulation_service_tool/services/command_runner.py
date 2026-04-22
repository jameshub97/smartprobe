"""Safe subprocess helpers for kubectl and helm commands used by the CLI."""

import os
import re
import shutil
import subprocess

# Directories added to PATH when locating binaries.  These are prepended to
# whatever PATH the current process inherited so that tools installed via
# Homebrew (macOS) or in /usr/local/bin are always found even when the parent
# process was launched without those paths (e.g. from a GUI app or a service
# supervisor that strips the user's shell PATH).
_EXTRA_BIN_DIRS = [
    '/opt/homebrew/bin',   # Apple-silicon Homebrew
    '/usr/local/bin',      # Intel Homebrew / common Linux
    '/usr/bin',
    '/bin',
]


def _augmented_env() -> dict:
    """Return os.environ with _EXTRA_BIN_DIRS prepended to PATH."""
    env = os.environ.copy()
    current = env.get('PATH', '')
    extra = ':'.join(d for d in _EXTRA_BIN_DIRS if d not in current.split(':'))
    env['PATH'] = f"{extra}:{current}" if extra else current
    return env


def _resolve_binary(name: str) -> str:
    """Return the full path for *name*, searching _EXTRA_BIN_DIRS if needed."""
    found = shutil.which(name, path=_augmented_env()['PATH'])
    return found if found else name


DEFAULT_NAMESPACE = os.environ.get('SIMULATION_K8S_NAMESPACE', 'default')
COMMAND_TIMEOUTS = {
    'kubectl': 4,
    'helm': 6,
}

ALLOWED_KUBECTL_VERBS = {'apply', 'delete', 'describe', 'get', 'logs'}
ALLOWED_KUBECTL_RESOURCES = {
    'clusterqueue',
    'clusterqueues',
    'configmap',
    'configmaps',
    'crd',
    'crds',
    'customresourcedefinition',
    'customresourcedefinitions',
    'deployment',
    'deployments',
    'events',
    'job',
    'jobs',
    'localqueue',
    'localqueues',
    'node',
    'nodes',
    'pdb',
    'pod',
    'pods',
    'pvc',
    'pvcs',
    'replicaset',
    'replicasets',
    'secret',
    'secrets',
    'service',
    'services',
    'statefulset',
    'statefulsets',
    'workload',
    'workloads',
}
ALLOWED_HELM_COMMANDS = {'get', 'list', 'status', 'uninstall'}
ALLOWED_HELM_GET_ACTIONS = {'manifest', 'values'}
NAME_PATTERN = re.compile(r'^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$')


def is_valid_k8s_name(name: str) -> bool:
    return bool(name and len(name) <= 253 and NAME_PATTERN.match(name))


def format_command(args) -> str:
    return ' '.join(args)


def _completed_error(args, message, returncode=2):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout='', stderr=message)


def _strip_namespace_flags(args):
    stripped = []
    namespace = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in {'-n', '--namespace'} and index + 1 < len(args):
            namespace = args[index + 1]
            index += 2
            continue
        stripped.append(token)
        index += 1
    return stripped, namespace


def _validate_resource_spec(resource_spec: str):
    resources = [part.strip() for part in (resource_spec or '').split(',') if part.strip()]
    if not resources:
        raise ValueError('kubectl resource type is required')
    invalid = [resource for resource in resources if resource not in ALLOWED_KUBECTL_RESOURCES]
    if invalid:
        raise ValueError(f"Disallowed kubectl resource type: {', '.join(invalid)}")


def _prepare_kubectl_args(args, namespace):
    tokens, explicit_namespace = _strip_namespace_flags(list(args[1:]))
    if not tokens:
        raise ValueError('kubectl verb is required')

    verb = tokens[0]
    if verb not in ALLOWED_KUBECTL_VERBS:
        raise ValueError(f'Disallowed kubectl verb: {verb}')

    if verb == 'logs':
        if len(tokens) < 2 or tokens[1].startswith('-'):
            raise ValueError('kubectl logs requires a pod name')
        if not is_valid_k8s_name(tokens[1]):
            raise ValueError(f'Invalid Kubernetes resource name: {tokens[1]}')
    elif verb != 'apply':
        if len(tokens) < 2:
            raise ValueError(f'kubectl {verb} requires a resource type')
        _validate_resource_spec(tokens[1])
        if len(tokens) >= 3 and not tokens[2].startswith('-'):
            if not is_valid_k8s_name(tokens[2]):
                raise ValueError(f'Invalid Kubernetes resource name: {tokens[2]}')

    effective_namespace = explicit_namespace or namespace
    if effective_namespace:
        return ['kubectl', '-n', effective_namespace, *tokens]
    return ['kubectl', *tokens]


def _prepare_helm_args(args, namespace):
    tokens, explicit_namespace = _strip_namespace_flags(list(args[1:]))
    if not tokens:
        raise ValueError('helm subcommand is required')

    subcommand = tokens[0]
    if subcommand not in ALLOWED_HELM_COMMANDS:
        raise ValueError(f'Disallowed helm subcommand: {subcommand}')

    if subcommand == 'get':
        if len(tokens) < 3:
            raise ValueError('helm get requires an action and release name')
        if tokens[1] not in ALLOWED_HELM_GET_ACTIONS:
            raise ValueError(f'Disallowed helm get action: {tokens[1]}')
        if not is_valid_k8s_name(tokens[2]):
            raise ValueError(f'Invalid Helm release name: {tokens[2]}')
    elif subcommand == 'status':
        if len(tokens) < 2:
            raise ValueError('helm status requires a release name')
        if not is_valid_k8s_name(tokens[1]):
            raise ValueError(f'Invalid Helm release name: {tokens[1]}')
    elif subcommand == 'uninstall':
        if len(tokens) < 2:
            raise ValueError('helm uninstall requires a release name')
        if not is_valid_k8s_name(tokens[1]):
            raise ValueError(f'Invalid Helm release name: {tokens[1]}')

    effective_namespace = explicit_namespace or namespace
    if effective_namespace:
        return ['helm', *tokens, '-n', effective_namespace]
    return ['helm', *tokens]


def build_cli_command(args, namespace=DEFAULT_NAMESPACE):
    if not args:
        raise ValueError('Command arguments are required')

    binary = args[0]
    if binary == 'kubectl':
        return _prepare_kubectl_args(args, namespace)
    if binary == 'helm':
        return _prepare_helm_args(args, namespace)
    raise ValueError(f'Unsupported CLI command: {binary}')


def run_cli_command(args, namespace=DEFAULT_NAMESPACE, timeout=None):
    try:
        prepared = build_cli_command(args, namespace=namespace)
    except ValueError as exc:
        return _completed_error(list(args), str(exc))

    command_timeout = timeout if timeout is not None else COMMAND_TIMEOUTS.get(prepared[0])
    # Resolve the binary to its full path so the subprocess succeeds even when
    # the parent process was started without Homebrew's bin dir on PATH.
    prepared = [_resolve_binary(prepared[0]), *prepared[1:]]
    try:
        return subprocess.run(prepared, capture_output=True, text=True, shell=False, timeout=command_timeout)
    except FileNotFoundError as exc:
        return _completed_error(prepared, str(exc), returncode=127)
    except subprocess.TimeoutExpired:
        return _completed_error(prepared, f"Command timed out after {command_timeout}s: {format_command(prepared)}", returncode=124)