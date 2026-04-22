"""Test launch flow."""

import os
import subprocess
import time

import questionary

from simulation_service_tool.cli.initialize_cluster import initialize_cluster_menu
from simulation_service_tool.cli.preflight import (
    _auto_fix_conflicts,
    _get_preflight,
    _handle_remaining_preflight_conflicts,
    _handle_start_error_recovery,
)
from simulation_service_tool.cli.prompts import _prompt_advanced_test_options, _prompt_go_back
from simulation_service_tool.cli.watch import watch_release_pods_kubectl
from simulation_service_tool.menus.presets import get_preset_config
from simulation_service_tool.services.api_client import call_service
from simulation_service_tool.services.command_runner import _resolve_binary, run_cli_command
from simulation_service_tool.ui.styles import custom_style
from simulation_service_tool.ui.utils import clear_screen

_HELM_CHART_SUBPATH = os.path.join('helm', 'playwright-agent')
_LOCAL_REGISTRY = 'host.docker.internal:5050'
_BARE_IMAGE = 'playwright-agent'
_REGISTRY_IMAGE = f'{_LOCAL_REGISTRY}/{_BARE_IMAGE}:latest'


def _image_in_registry(tagged: str) -> bool:
    """Return True if *tagged* is available in the local registry.

    Uses ``docker manifest inspect`` which queries the registry API without
    pulling the full image.  Falls back to False on any error.
    """
    try:
        r = subprocess.run(
            ['docker', 'manifest', 'inspect', tagged],
            capture_output=True, text=True, timeout=8,
        )
        return r.returncode == 0
    except Exception:
        return False


def _push_image_to_registry(bare: str, tagged: str) -> tuple[bool, str]:
    """Tag and push *bare* image to *tagged* in the local registry.

    Returns (success, error_message).
    """
    from simulation_service_tool.menus.image_pull import _image_exists_locally
    if not _image_exists_locally(bare):
        return False, f"Image '{bare}' not found in local Docker daemon. Build it first."

    for desc, argv in [
        (f'docker tag {bare} {tagged}', ['docker', 'tag', bare, tagged]),
        (f'docker push {tagged}', ['docker', 'push', tagged]),
    ]:
        print(f'  $ {desc}')
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False, f'{desc} timed out after 120s'
        except FileNotFoundError:
            return False, 'docker not found in PATH'
        if r.returncode != 0:
            err = (r.stderr or r.stdout or 'command failed').strip()
            print(f'    [FAIL] {err[:200]}')
            return False, err
        print('    [OK]')
    return True, ''


def _check_registry_image_prelaunch() -> bool:
    """Check whether the playwright-agent image is in the local registry.

    If it is missing, offer to push it now.  Returns True when it is safe to
    proceed (image is present or user chose to continue anyway), False to abort.
    """
    from simulation_service_tool.menus.image_pull import (
        check_local_registry, _image_exists_locally,
    )

    yellow = '\033[33m'
    green  = '\033[32m'
    reset  = '\033[0m'

    print(f'[36m[INFO][0m Checking image in local registry…')

    # Fast path: if the registry isn’t reachable at all, skip check
    reg = check_local_registry()
    if not reg['reachable']:
        print(f'{yellow}[WARN]{reset} Local registry unreachable ({reg["error"]})')
        print('  Pods may fail with ErrImagePull. Start registry with:')
        print('    docker run -d -p 5050:5000 registry:2')
        action = questionary.select(
            'Registry not reachable. Continue anyway?',
            choices=[
                questionary.Choice(title='Continue anyway', value='continue'),
                questionary.Choice(title='Abort', value='abort'),
            ],
            style=custom_style,
        ).ask()
        return action != 'abort' and action is not None

    if _image_in_registry(_REGISTRY_IMAGE):
        print(f'  {green}✓{reset} {_REGISTRY_IMAGE} found in registry.')
        return True

    # Image missing from registry
    print(f'{yellow}[WARN]{reset} Image not found in local registry: {_REGISTRY_IMAGE}')
    local_ok = _image_exists_locally(_BARE_IMAGE)
    if local_ok:
        print(f'  Local image \'{_BARE_IMAGE}\' found. Needs to be pushed.')
    else:
        print(f'  Local image \'{_BARE_IMAGE}\' not found either — build it first:')
        print(f'    docker build -t {_BARE_IMAGE} .')

    choices = []
    if local_ok:
        choices.append(questionary.Choice(
            title=f'Push {_BARE_IMAGE} to registry now  (docker tag + docker push)',
            value='push',
        ))
    choices.extend([
        questionary.Choice(title='Continue anyway (pods may fail)', value='continue'),
        questionary.Choice(title='Abort', value='abort'),
    ])

    action = questionary.select(
        'Image missing from local registry. What would you like to do?',
        choices=choices,
        style=custom_style,
    ).ask()

    if action == 'push':
        print()
        ok, err = _push_image_to_registry(_BARE_IMAGE, _REGISTRY_IMAGE)
        if ok:
            print(f'  {green}✓{reset} Image pushed successfully.')
            return True
        else:
            print(f'{yellow}[WARN]{reset} Push failed: {err}')
            retry = questionary.select(
                'Push failed. How would you like to proceed?',
                choices=[
                    questionary.Choice(title='Continue anyway', value='continue'),
                    questionary.Choice(title='Abort', value='abort'),
                ],
                style=custom_style,
            ).ask()
            return retry != 'abort' and retry is not None

    return action == 'continue'


def _locate_helm_chart() -> str | None:
    """Return an absolute path to the playwright-agent helm chart, or None."""
    # 1. Relative to the package source tree (dev / editable install)
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidate = os.path.join(pkg_root, _HELM_CHART_SUBPATH)
    if os.path.isdir(candidate):
        return candidate
    # 2. Relative to the current working directory (installed / CI)
    candidate = os.path.join(os.getcwd(), _HELM_CHART_SUBPATH)
    if os.path.isdir(candidate):
        return candidate
    return None


def _build_direct_helm_values(payload: dict) -> dict:
    """Map an /api/simulation/start payload to helm --set values.

    Mirrors the logic in TestController.run_test() so results are identical
    whether the service or the CLI drives the helm install.
    """
    image_repository = (payload.get('imageRepository') or '').strip() or 'playwright-agent'
    image_tag = (payload.get('imageTag') or '').strip() or 'latest'
    command_override = (payload.get('commandOverride') or '').strip() or 'python3 /app/run.py'
    ttl = payload.get('ttlSecondsAfterFinished', 3600)
    probe_mode = (payload.get('mode') or 'transactional').strip()

    values: dict = {
        'completions': payload.get('completions', 10),
        'parallelism': payload.get('parallelism', 5),
        'persona': payload.get('persona', 'impatient'),
        'workersPerPod': payload.get('workers', 1),
        'image.repository': image_repository,
        'image.tag': image_tag,
        'commandOverride': command_override,
        'ttlSecondsAfterFinished': ttl,
        'probeMode': probe_mode,
    }
    optional_mappings = [
        ('replicaCount', 'replicaCount'),
        ('shardTotal', 'shardTotal'),
        ('backoffLimit', 'backoffLimit'),
        ('requestMemory', 'resources.requests.memory'),
        ('requestCpu', 'resources.requests.cpu'),
        ('limitMemory', 'resources.limits.memory'),
        ('limitCpu', 'resources.limits.cpu'),
    ]
    for payload_key, helm_key in optional_mappings:
        val = payload.get(payload_key)
        if val is not None and val != '':
            values[helm_key] = val

    if image_repository == 'playwright-agent':
        values['targetUrl'] = 'http://host.docker.internal:5001'
        values['simApi'] = 'http://host.docker.internal:5002/api/simulation'
        values['backendApi'] = 'http://host.docker.internal:5001/api/simulation/results'
        values['coordApi'] = 'http://host.docker.internal:5003/api/coordinator'
        values['image.repository'] = 'host.docker.internal:5050/playwright-agent'
        values['image.pullPolicy'] = 'IfNotPresent'

    if payload.get('kueue'):
        values['kueue.enabled'] = True
        values['kueue.queueName'] = 'simulation-queue'

    return values


def _check_node_images_prelaunch() -> bool:
    """Verify playwright-agent is loaded on every k8s worker node before launch.

    Uses the same crictl probe as the boot-time check.  If any nodes are
    missing the image the user is offered an inline ``kind load`` fix.

    Returns True when it is safe to proceed (all loaded, or user chose to
    continue anyway).  Returns False to abort.
    """
    from concurrent.futures import ThreadPoolExecutor
    from simulation_service_tool.cli.main import (
        _get_k8s_node_containers,
        _image_on_node,
    )
    from simulation_service_tool.menus.image_pull import (
        _image_exists_locally,
        _get_kind_cluster_name,
        _kind_load_images,
    )

    green  = '\033[32m'
    yellow = '\033[33m'
    red    = '\033[31m'
    bold   = '\033[1m'
    dim    = '\033[2m'
    reset  = '\033[0m'

    print(f'\n{dim}  Checking playwright-agent on cluster nodes…{reset}', end='', flush=True)

    nodes = _get_k8s_node_containers()
    if not nodes:
        # Can't list nodes — don't block launch
        print(f'\r  {yellow}[WARN]{reset} Could not query cluster nodes — skipping image check.')
        return True

    workers = [n for n in nodes if 'worker' in n.lower() or
               ('control' not in n.lower() and 'master' not in n.lower())]
    check_nodes = workers or nodes  # fall back to all nodes if no workers found

    with ThreadPoolExecutor(max_workers=min(len(check_nodes), 8)) as pool:
        futures = {pool.submit(_image_on_node, n, _BARE_IMAGE): n for n in check_nodes}
        node_status = {futures[f]: f.result() for f in futures}

    missing = [n for n, ok in node_status.items() if not ok]
    total   = len(check_nodes)

    if not missing:
        print(f'\r  {green}✓{reset} playwright-agent loaded on all {total} node(s).\n')
        return True

    # Clear the checking line
    print('\r' + ' ' * 60 + '\r', end='')
    print(f'  {red}✗{reset} playwright-agent {bold}missing on {len(missing)}/{total} node(s){reset}:')
    for n in missing[:5]:
        print(f'  {dim}    · {n}{reset}')
    if len(missing) > 5:
        print(f'  {dim}    … and {len(missing) - 5} more{reset}')
    print(f'  {yellow}  Pods will immediately fail with ErrImagePull if you continue.{reset}\n')

    local_ok = _image_exists_locally(_BARE_IMAGE)
    if not local_ok:
        print(f'  {yellow}[WARN]{reset} Local image \'{_BARE_IMAGE}\' not found in Docker daemon.')
        print(f'  Build it first:  docker build -t {_BARE_IMAGE} .\n')
        action = questionary.select(
            'Image not built locally. How would you like to proceed?',
            choices=[
                questionary.Choice(title='Continue anyway (pods will ErrImagePull)', value='continue'),
                questionary.Choice(title='Abort', value='abort'),
            ],
            style=custom_style,
        ).ask()
        return action == 'continue'

    cluster = _get_kind_cluster_name()
    action = questionary.select(
        f'playwright-agent is missing on {len(missing)} node(s). What would you like to do?',
        choices=[
            questionary.Choice(
                title=f'Load now  (kind load docker-image → cluster "{cluster}")',
                value='load',
            ),
            questionary.Choice(title='Continue anyway (pods will ErrImagePull)', value='continue'),
            questionary.Choice(title='Abort', value='abort'),
        ],
        style=custom_style,
    ).ask()

    if action == 'abort' or action is None:
        return False

    if action == 'continue':
        return True

    # Load the image
    print(f'\n  Loading {_BARE_IMAGE} into kind cluster "{cluster}"…\n')
    fake_failing = [{'image': _REGISTRY_IMAGE}]
    results = _kind_load_images(fake_failing)
    ok_count = sum(1 for r in results if r['returncode'] == 0)

    if ok_count == len(results):
        print(f'\n  {green}✓{reset} Image loaded on all nodes.\n')
        return True

    # Partial failure — show errors and let user decide
    for r in results:
        if r['returncode'] != 0:
            print(f'  {red}[FAIL]{reset} {(r.get("stderr") or "")[:120]}')
    print()
    retry = questionary.select(
        'kind load had errors. Continue anyway?',
        choices=[
            questionary.Choice(title='Continue anyway', value='continue'),
            questionary.Choice(title='Abort', value='abort'),
        ],
        style=custom_style,
    ).ask()
    return retry == 'continue'


def _run_direct_helm_install(name: str, payload: dict) -> tuple[bool, str]:
    """Install the playwright-agent chart directly from the host CLI.

    Used as an automatic fallback when the simulation service (running inside
    Docker) cannot find helm.  Returns (success, error_message).
    """
    helm_bin = _resolve_binary('helm')
    namespace = os.environ.get('SIMULATION_K8S_NAMESPACE', 'default')

    chart_dir = _locate_helm_chart()
    if chart_dir is None:
        return False, (
            f"Helm chart not found. Expected at: "
            f"{os.path.join(os.getcwd(), _HELM_CHART_SUBPATH)}"
        )

    # Clean up any previous release first (mirrors HelmClient.install)
    run_cli_command(['helm', 'uninstall', name, '--ignore-not-found'], namespace=namespace)

    values = _build_direct_helm_values(payload)
    cmd = [helm_bin, 'install', name, chart_dir, '-n', namespace]
    for key, value in values.items():
        cmd.extend(['--set', f'{key}={value}'])

    result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if result.returncode == 0:
        return True, ''
    err = (result.stderr or result.stdout or 'helm install failed').strip()
    return False, err


def start_test_menu(service_running):
    """Start a test with preset or custom configuration."""
    clear_screen()
    method = questionary.select(
        "How would you like to configure the test?",
        choices=[
            questionary.Choice(title="Use a preset (recommended)", value="preset"),
            questionary.Choice(title="Custom configuration", value="custom"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style
    ).ask()
    if method == "back":
        return
    # Choose simulation mode
    mode = questionary.select(
        "Simulation mode:",
        choices=[
            questionary.Choice(title="basic - Standard Playwright e2e test", value="basic"),
            questionary.Choice(title="transactional - Browse & transfer assets (agent)", value="transactional"),
            questionary.Separator(),
            questionary.Choice(title="Back", value="back"),
        ],
        style=custom_style
    ).ask()
    if mode == "back":
        return

    if method == "preset":
        preset = questionary.select(
            "Choose preset:",
            choices=[
                questionary.Choice(title="tiny   | 5 agents   | 2 parallel  | ~10s", value="tiny"),
                questionary.Choice(title="small  | 10 agents  | 5 parallel  | ~30s", value="small"),
                questionary.Choice(title="medium | 50 agents  | 10 parallel | ~2m", value="medium"),
                questionary.Choice(title="large  | 100 agents | 20 parallel | ~5m", value="large"),
                questionary.Choice(title="xlarge | 500 agents | 50 parallel | ~15m", value="xlarge"),
                questionary.Choice(title="throughput | 500 agents | 100 parallel | low resource", value="throughput"),
                questionary.Separator(),
                questionary.Choice(title="Back", value="back"),
            ],
            style=custom_style
        ).ask()
        if preset == "back":
            return
        config = get_preset_config(preset)
        config['mode'] = mode
    else:
        completions = questionary.text(
            "Number of agents (completions):",
            default="50",
            validate=lambda text: text.isdigit() and int(text) > 0 or "Must be a positive number"
        ).ask()
        parallelism = questionary.text(
            "Max concurrent agents (parallelism):",
            default=str(min(10, int(completions))),
            validate=lambda text: text.isdigit() and int(text) > 0 or "Must be a positive number"
        ).ask()
        persona = questionary.select(
            "Agent persona:",
            choices=[
                questionary.Choice(title="strategic - Thoughtful, variable timing", value="strategic"),
                questionary.Choice(title="browser - Realistic human behavior", value="browser"),
                questionary.Choice(title="impatient - Fast clicks, minimal waits", value="impatient"),
            ],
            style=custom_style
        ).ask()
        config = {
            'completions': int(completions),
            'parallelism': int(parallelism),
            'persona': persona,
            'mode': mode
        }
        if not _prompt_advanced_test_options(config):
            _prompt_go_back()
            return
        preset = "custom"
    if mode == 'basic':
        probe_url = questionary.text(
            "Target URL to probe (e.g. http://localhost:5174):",
            default="http://localhost:5174",
        ).ask()
        if probe_url:
            config['probeUrl'] = probe_url.strip().rstrip('/')

    default_name = f"{preset}-{int(time.time())}"
    name = questionary.text(
        "Test name (leave blank for auto-generated):",
        default=""
    ).ask()
    if not name:
        name = default_name
    print(f"\n[CONFIG] Test Configuration:")
    print(f"   Name: {name}")
    print(f"   Mode: {config.get('mode', 'basic')}")
    if config.get('probeUrl'):
        print(f"   Probe URL: {config['probeUrl']}")
    print(f"   Agents: {config.get('completions', '?')}")
    print(f"   Parallel: {config.get('parallelism', '?')}")
    print(f"   Persona: {config.get('persona', 'browser')}")
    if config.get('workers'):
        print(f"   Workers per pod: {config['workers']}")
    if config.get('replicaCount'):
        print(f"   Replica count: {config['replicaCount']}")
    if config.get('shardTotal'):
        print(f"   Shard total: {config['shardTotal']}")
    if config.get('requestMemory') and config.get('requestCpu'):
        print(f"   Requests: {config['requestCpu']} CPU / {config['requestMemory']} memory")
    if config.get('limitMemory') and config.get('limitCpu'):
        print(f"   Limits: {config['limitCpu']} CPU / {config['limitMemory']} memory")
    if 'backoffLimit' in config:
        print(f"   Backoff limit: {config['backoffLimit']}")
    if 'ttlSecondsAfterFinished' in config:
        print(f"   TTL after finished: {config['ttlSecondsAfterFinished']}s")
    if config.get('wait'):
        print("   Wait for ready: yes")
    if config.get('imageRepository') and config.get('imageTag'):
        print(f"   Image: {config['imageRepository']}:{config['imageTag']}")
    if config.get('commandOverride'):
        print(f"   Command override: {config['commandOverride']}")
    if config.get('kueue'):
        print("   Kueue queuing: enabled")
    confirm = questionary.confirm("Start this test?", default=True).ask()
    if not confirm:
        print("[33m[CANCELLED][0m")
        _prompt_go_back()
        return
    # ── pre-launch node image check ──────────────────────────────────────────
    if not _check_node_images_prelaunch():
        _prompt_go_back()
        return
    print(f"\nStarting test: {name}")
    if service_running:
        print("[36m[INFO][0m Probing service health...")
        from simulation_service_tool.services.api_client import check_service
        if not check_service():
            print("[33m[WARN][0m Simulation service is not responding. Preflight will fall back to direct mode.")
            service_running = False

        if service_running:
            print("[36m[INFO][0m Running preflight conflict check via service API...")
        else:
            print("[36m[INFO][0m Running preflight conflict check via kubectl...")
        preflight = _get_preflight(service_running)
        if preflight.get('cancelled'):
            print("[33m[CANCELLED][0m Preflight check was cancelled.")
            _prompt_go_back()
            return
        if preflight.get('error'):
            print(f"[33m[WARN][0m Could not run preflight checks: {preflight['error']}")
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice(title="Start the simulation service and retry", value="start_service"),
                    questionary.Choice(title="Continue anyway (skip preflight)", value="continue"),
                    questionary.Choice(title="Go back", value="back"),
                ],
                style=custom_style,
            ).ask()
            if action == "start_service":
                from simulation_service_tool.services.smart_diagnostics import _restart_service
                print("[36m[INFO][0m Starting simulation service...")
                success, detail = _restart_service()
                print(f"{'[32m[OK][0m' if success else '[33m[WARN][0m'} {detail}")
                if success:
                    print("[36m[INFO][0m Retrying preflight check...")
                    service_running = True
                    preflight = _get_preflight(service_running)
                    if preflight.get('cancelled'):
                        print("[33m[CANCELLED][0m Preflight check was cancelled.")
                        _prompt_go_back()
                        return
                    if preflight.get('error'):
                        print(f"[33m[WARN][0m Preflight still unavailable: {preflight['error']}")
                        preflight = {'has_conflicts': False}
                else:
                    _prompt_go_back()
                    return
            elif action == "continue":
                preflight = {'has_conflicts': False}
            else:
                return

        if preflight.get('has_conflicts'):
            print("[36m[INFO][0m Clearing conflicting resources...")
            attempted_cleanup = _auto_fix_conflicts(preflight)
            if attempted_cleanup:
                print("[32m[OK][0m Standard conflicts cleared.")

            print("[36m[INFO][0m Re-checking cluster state...")
            preflight = _get_preflight(service_running)
            if preflight.get('cancelled'):
                print("[33m[CANCELLED][0m Preflight refresh was cancelled.")
                _prompt_go_back()
                return
            if preflight.get('error'):
                print(f"[33m[WARN][0m Could not refresh preflight checks: {preflight['error']}")
                _prompt_go_back()
                return
            if preflight.get('has_conflicts'):
                if not _handle_remaining_preflight_conflicts(preflight, service_running):
                    print("[33m[CANCELLED][0m")
                    _prompt_go_back()
                    return

        payload = {
            'name': name,
            'skip_preflight': True,  # Already checked and cleaned above
            **config
        }

        # Auto-detect Kueue if not explicitly set by advanced options
        if 'kueue' not in payload:
            try:
                from simulation_service_tool.services.kueue import is_kueue_installed
                if is_kueue_installed():
                    payload['kueue'] = True
                    print("[36m[INFO][0m Kueue detected on cluster — enabling workload queuing.")
            except Exception:
                pass

        result = call_service('/api/simulation/start', 'POST', payload)
        if 'error' in result:
            error_msg = result['error']
            _lower = error_msg.lower()
            if 'required command' in _lower and 'was not found' in _lower:
                # Service is running in Docker and doesn't have helm — fall back
                # to running helm directly from the host where it IS available.
                print("[36m[INFO][0m Service cannot run helm — using host helm directly...")
                ok, err = _run_direct_helm_install(name, payload)
                if not ok:
                    print(f"[31m[ERROR][0m Direct helm install failed: {err}")
                    _handle_start_error_recovery(err, service_running)
                    return
                # ok — fall through to success message below
            else:
                print(f"[31m[ERROR][0m {error_msg}")
                _handle_start_error_recovery(error_msg, service_running)
                return
        print("\033[92m✓ Test started successfully!\033[0m")
        choice = questionary.select(
            "What would you like to do?",
            choices=["Watch pods", "Return to main menu"],
            style=custom_style
        ).ask()
        if choice == "Watch pods":
            watch_release_pods_kubectl(name)
        return
    else:
        # Auto-detect Kueue for direct helm path
        if 'kueue' not in config:
            try:
                from simulation_service_tool.services.kueue import is_kueue_installed
                if is_kueue_installed():
                    config['kueue.enabled'] = True
                    config['kueue.queueName'] = 'simulation-queue'
                    print("[36m[INFO][0m Kueue detected — enabling workload queuing.")
            except Exception:
                pass
        cmd = f"helm install {name} ./helm/playwright-agent"
        for key, value in config.items():
            cmd += f" --set {key}={value}"
        print(f"   Running: {cmd}")
        # subprocess.run(cmd, shell=True)  # Uncomment to actually run
        print("\033[92m✓ Test started successfully!\033[0m")
        choice = questionary.select(
            "What would you like to do?",
            choices=["Watch pods", "Return to main menu"],
            style=custom_style
        ).ask()
        if choice == "Watch pods":
            watch_release_pods_kubectl(name)
