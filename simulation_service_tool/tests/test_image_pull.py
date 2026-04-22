"""Tests for the image pull debugger menu and helper functions."""

from unittest.mock import patch, MagicMock, call
import types
import pytest

from simulation_service_tool.menus.image_pull import (
    scan_image_pull_errors,
    check_local_registry,
    render_image_pull_diagnosis,
    _show_push_commands,
    _build_push_steps,
    _image_exists_locally,
    _detect_mirror_500,
    run_push_commands,
    delete_failing_pods,
    _show_containerd_patch,
    _show_pull_policy_patch,
    image_pull_menu,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _cmd(returncode=0, stdout="", stderr=""):
    r = types.SimpleNamespace()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


_GET_PODS_BACKOFF = (
    "playwright-agent-abc   0/1   ImagePullBackOff\n"
    "playwright-agent-xyz   0/1   ErrImagePull\n"
    "healthy-pod-123        1/1   Running\n"
)

_DESCRIBE_POD = """\
Name:         playwright-agent-abc
Namespace:    default
Controlled By: Job/playwright-agent-job
Events:
  Warning  Failed  5s  kubelet  Failed to pull image "host.docker.internal:5050/playwright:latest"
  Warning  Failed  3s  kubelet  Error response from daemon: manifest unknown
"""


# ── scan_image_pull_errors ────────────────────────────────────────────────────

class TestScanImagePullErrors:
    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_returns_failing_pods(self, mock_cmd):
        # First call: get pods (custom-columns) — succeed
        # Subsequent calls: describe each failing pod
        mock_cmd.side_effect = [
            _cmd(0, _GET_PODS_BACKOFF),          # get pods
            _cmd(0, _DESCRIBE_POD),              # describe playwright-agent-abc
            _cmd(0, _DESCRIBE_POD),              # describe playwright-agent-xyz
        ]
        pods = scan_image_pull_errors()
        assert len(pods) == 2
        names = [p["name"] for p in pods]
        assert "playwright-agent-abc" in names
        assert "playwright-agent-xyz" in names

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_no_failing_pods_returns_empty(self, mock_cmd):
        mock_cmd.return_value = _cmd(0, "healthy-pod-123   1/1   Running\n")
        result = scan_image_pull_errors()
        assert result == []

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_command_error_returns_empty(self, mock_cmd):
        # First call (custom-columns) fails, fallback also fails
        mock_cmd.return_value = _cmd(1, "")
        result = scan_image_pull_errors()
        assert result == []

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_pod_entry_has_expected_keys(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd(0, "playwright-agent-abc   0/1   ErrImagePull\n"),
            _cmd(0, _DESCRIBE_POD),
        ]
        pods = scan_image_pull_errors()
        assert len(pods) == 1
        pod = pods[0]
        assert pod["name"] == "playwright-agent-abc"
        assert pod["status"] in ("ErrImagePull", "ImagePullBackOff")
        assert "namespace" in pod
        assert "image" in pod
        assert "message" in pod

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_image_extracted_from_describe(self, mock_cmd):
        mock_cmd.side_effect = [
            _cmd(0, "playwright-agent-abc   0/1   ErrImagePull\n"),
            _cmd(0, _DESCRIBE_POD),
        ]
        pods = scan_image_pull_errors()
        assert pods[0]["image"] == "host.docker.internal:5050/playwright:latest"

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_message_trimmed_to_three_lines(self, mock_cmd):
        many_events = "\n".join(
            [f"  Warning  Failed  {i}s  kubelet  Failed to pull image \"img\"" for i in range(10)]
        )
        mock_cmd.side_effect = [
            _cmd(0, "playwright-agent-abc   0/1   ErrImagePull\n"),
            _cmd(0, many_events),
        ]
        pods = scan_image_pull_errors()
        assert pods[0]["message"].count("\n") <= 2  # at most 3 lines joined by \n

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_custom_namespace_forwarded(self, mock_cmd):
        mock_cmd.return_value = _cmd(0, "")
        scan_image_pull_errors(namespace="staging")
        first_call_kwargs = mock_cmd.call_args_list[0][1]
        assert first_call_kwargs.get("namespace") == "staging"

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_parallel_describe_all_pods_returned(self, mock_cmd):
        """15 failing pods are all returned even though describes run in parallel."""
        pod_lines = "".join(
            f"large-{i:03d}-agent   0/1   ErrImagePull\n" for i in range(15)
        )
        mock_cmd.side_effect = [_cmd(0, pod_lines)] + [_cmd(0, _DESCRIBE_POD)] * 15
        pods = scan_image_pull_errors()
        assert len(pods) == 15

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_max_describe_cap(self, mock_cmd):
        """Pods beyond max_describe are included as stubs rather than described."""
        pod_lines = "".join(
            f"agent-{i:03d}   0/1   ErrImagePull\n" for i in range(10)
        )
        # Only 3 describes should be made when max_describe=3
        mock_cmd.side_effect = [_cmd(0, pod_lines)] + [_cmd(0, _DESCRIBE_POD)] * 3
        pods = scan_image_pull_errors(max_describe=3)
        assert len(pods) == 10  # all 10 returned
        # First 3 have real data; rest are stubs
        stubs = [p for p in pods if "too many pods" in p["message"]]
        assert len(stubs) == 7


# ── delete_failing_pods ───────────────────────────────────────────────────────

class TestDeleteFailingPods:
    def _pods(self, *names):
        return [{"name": n, "namespace": "default", "status": "ErrImagePull",
                 "image": "img:v1", "message": ""} for n in names]

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_deletes_each_pod(self, mock_cmd):
        mock_cmd.return_value = _cmd(0, "pod deleted")
        results = delete_failing_pods(self._pods("agent-1", "agent-2", "agent-3"))
        assert len(results) == 3
        assert all(r["returncode"] == 0 for r in results)
        # Each call should be a delete verb
        for c in mock_cmd.call_args_list:
            args = c[0][0]
            assert args[1] == "delete"
            assert args[2] == "pod"

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_partial_failure_reported(self, mock_cmd):
        mock_cmd.side_effect = [_cmd(0), _cmd(1, stderr="not found")]
        results = delete_failing_pods(self._pods("agent-1", "agent-2"))
        assert results[0]["returncode"] == 0
        assert results[1]["returncode"] == 1

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_uses_correct_namespace(self, mock_cmd):
        mock_cmd.return_value = _cmd(0)
        pods = [{"name": "p", "namespace": "staging", "status": "ErrImagePull",
                 "image": None, "message": ""}]
        delete_failing_pods(pods)
        assert mock_cmd.call_args[1]["namespace"] == "staging"


# ── check_local_registry ──────────────────────────────────────────────────────

class TestCheckLocalRegistry:
    def test_reachable_returns_true(self):
        with patch("simulation_service_tool.menus.image_pull.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=None)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            result = check_local_registry("localhost", 5050)
        assert result["reachable"] is True
        assert result["error"] is None

    def test_unreachable_returns_error(self):
        with patch("simulation_service_tool.menus.image_pull.socket.create_connection",
                   side_effect=OSError("Connection refused")):
            result = check_local_registry("localhost", 5050)
        assert result["reachable"] is False
        assert "Connection refused" in result["error"]

    def test_result_contains_host_and_port(self):
        with patch("simulation_service_tool.menus.image_pull.socket.create_connection",
                   side_effect=OSError("timeout")):
            result = check_local_registry("host.docker.internal", 5050)
        assert result["host"] == "host.docker.internal"
        assert result["port"] == 5050


# ── render_image_pull_diagnosis ───────────────────────────────────────────────

class TestRenderImagePullDiagnosis:
    def test_no_errors_prints_clean_message(self, capsys):
        render_image_pull_diagnosis([])
        stdout = capsys.readouterr().out
        assert "No image pull errors detected" in stdout

    def test_failing_pods_appear_in_output(self, capsys):
        pods = [{"name": "agent-abc", "status": "ErrImagePull",
                 "image": "host.docker.internal:5050/img:v1",
                 "message": "Failed to pull image", "namespace": "default"}]
        render_image_pull_diagnosis(pods)
        stdout = capsys.readouterr().out
        assert "agent-abc" in stdout
        assert "ErrImagePull" in stdout

    def test_registry_result_shown_when_provided(self, capsys):
        pods = [{"name": "p", "status": "ErrImagePull", "image": None,
                 "message": "", "namespace": "default"}]
        render_image_pull_diagnosis(pods, registry_result={"reachable": False,
                                                            "host": "localhost",
                                                            "port": 5050,
                                                            "error": "refused"})
        out = capsys.readouterr().out
        assert "unreachable" in out.lower() or "refused" in out.lower()

    def test_registry_reachable_shown(self, capsys):
        pods = [{"name": "p", "status": "ErrImagePull", "image": None,
                 "message": "", "namespace": "default"}]
        render_image_pull_diagnosis(pods, registry_result={"reachable": True,
                                                            "host": "localhost",
                                                            "port": 5050,
                                                            "error": None})
        out = capsys.readouterr().out
        assert "reachable" in out.lower()

    def test_long_image_name_does_not_crash(self, capsys):
        long_img = "host.docker.internal:5050/" + "a" * 200 + ":latest"
        pods = [{"name": "p", "status": "ErrImagePull", "image": long_img,
                 "message": "", "namespace": "default"}]
        render_image_pull_diagnosis(pods)  # must not raise


# ── _show_push_commands ───────────────────────────────────────────────────────

class TestShowPushCommands:
    def test_prints_docker_commands_for_each_image(self, capsys):
        pods = [
            {"name": "a", "image": "playwright:v1", "namespace": "default", "status": "ErrImagePull", "message": ""},
            {"name": "b", "image": "playwright:v1", "namespace": "default", "status": "ErrImagePull", "message": ""},
            {"name": "c", "image": "worker:v2", "namespace": "default", "status": "ErrImagePull", "message": ""},
        ]
        _show_push_commands(pods)
        out = capsys.readouterr().out
        assert "docker push" in out
        # Deduplicated — playwright:v1 should appear once, worker:v2 once
        assert out.count("playwright:v1") >= 1
        assert out.count("worker:v2") >= 1

    def test_no_images_prints_fallback(self, capsys):
        _show_push_commands([{"name": "p", "image": None, "namespace": "default",
                               "status": "ErrImagePull", "message": ""}])
        out = capsys.readouterr().out
        assert "kubectl describe" in out or "Could not determine" in out


# ── _build_push_steps ─────────────────────────────────────────────────────────

class TestBuildPushSteps:
    def _pods(self, *images):
        return [{"name": f"p{i}", "image": img, "namespace": "default",
                 "status": "ErrImagePull", "message": ""}
                for i, img in enumerate(images)]

    def test_three_steps_per_image(self):
        steps = _build_push_steps(self._pods("playwright:v1"))
        assert len(steps) == 3
        descs = [d for d, _ in steps]
        assert any("pull" in d for d in descs)
        assert any("tag"  in d for d in descs)
        assert any("push" in d for d in descs)

    def test_deduplicates_images(self):
        steps = _build_push_steps(self._pods("img:v1", "img:v1", "img:v2"))
        assert len(steps) == 6  # 2 unique images × 3 steps

    def test_strips_registry_prefix_for_pull(self):
        pods = self._pods("host.docker.internal:5050/playwright:v1")
        steps = _build_push_steps(pods)
        pull_argv = next(argv for desc, argv in steps if argv[1] == "pull")
        # The bare image (without registry prefix) should be pulled
        assert pull_argv[2] == "playwright:v1"

    def test_push_target_includes_registry(self):
        steps = _build_push_steps(self._pods("playwright:v1"))
        push_argv = next(argv for desc, argv in steps if argv[1] == "push")
        assert push_argv[2].startswith("host.docker.internal:5050/")

    def test_no_images_returns_empty(self):
        assert _build_push_steps([{"name": "p", "image": None, "namespace": "default",
                                    "status": "ErrImagePull", "message": ""}]) == []

    @patch("simulation_service_tool.menus.image_pull._image_exists_locally", return_value=True)
    def test_skips_pull_when_image_exists_locally(self, _mock_local):
        """Locally-built images must skip the docker pull step."""
        steps = _build_push_steps(self._pods("playwright-agent:latest"))
        verbs = [argv[1] for _, argv in steps]
        assert "pull" not in verbs
        assert "tag" in verbs
        assert "push" in verbs
        assert len(steps) == 2  # only tag + push

    @patch("simulation_service_tool.menus.image_pull._image_exists_locally", return_value=False)
    def test_includes_pull_when_image_not_local(self, _mock_local):
        """Remote images that are not in the local daemon still need a pull step."""
        steps = _build_push_steps(self._pods("playwright-agent:latest"))
        verbs = [argv[1] for _, argv in steps]
        assert verbs == ["pull", "tag", "push"]
        assert len(steps) == 3


# ── run_push_commands ─────────────────────────────────────────────────────────

class TestRunPushCommands:
    def _pods(self, image="playwright:v1"):
        return [{"name": "p", "image": image, "namespace": "default",
                 "status": "ErrImagePull", "message": ""}]

    @patch("simulation_service_tool.menus.image_pull.subprocess.run")
    def test_all_success_returns_results(self, mock_run, capsys):
        mock_run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        results = run_push_commands(self._pods())
        assert len(results) == 3
        assert all(r["returncode"] == 0 for r in results)

    @patch("simulation_service_tool.menus.image_pull.subprocess.run")
    def test_stops_on_first_failure(self, mock_run):
        # pull fails, tag and push should not run
        mock_run.return_value = types.SimpleNamespace(returncode=1, stdout="", stderr="pull error")
        results = run_push_commands(self._pods())
        assert len(results) == 1
        assert results[0]["returncode"] == 1

    @patch("simulation_service_tool.menus.image_pull.subprocess.run",
           side_effect=FileNotFoundError)
    def test_docker_not_found(self, mock_run):
        results = run_push_commands(self._pods())
        assert results[0]["returncode"] == 127
        assert "not found" in results[0]["stderr"]

    @patch("simulation_service_tool.menus.image_pull.subprocess.run")
    def test_timeout_stops_batch(self, mock_run):
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(cmd=["docker"], timeout=120)
        results = run_push_commands(self._pods())
        assert results[0]["returncode"] == -1
        assert "timed out" in results[0]["stderr"]

    @patch("simulation_service_tool.menus.image_pull.subprocess.run")
    def test_no_shell_used(self, mock_run):
        """Ensure subprocess.run is never called with shell=True."""
        mock_run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        run_push_commands(self._pods())
        for c in mock_run.call_args_list:
            assert not c.kwargs.get("shell", False)


# ── _detect_mirror_500 ───────────────────────────────────────────────────────

class TestDetectMirror500:
    def _pod(self, message):
        return [{"name": "p", "status": "ErrImagePull", "image": "img:v1",
                 "message": message, "namespace": "default"}]

    def test_returns_true_when_registry_mirror_500(self):
        msg = (
            'failed to pull image: unexpected status from HEAD request to '
            'http://registry-mirror:1273/v2/playwright-agent/manifests/latest'
            '?ns=host.docker.internal%3A5050: 500 Internal Server Error'
        )
        assert _detect_mirror_500(self._pod(msg)) is True

    def test_returns_false_when_no_registry_mirror(self):
        assert _detect_mirror_500(self._pod("Error response from daemon: manifest unknown")) is False

    def test_returns_false_when_mirror_but_not_500(self):
        assert _detect_mirror_500(self._pod("registry-mirror:1273 returned 200")) is False

    def test_returns_false_for_empty_message(self):
        assert _detect_mirror_500(self._pod("")) is False


# ── _show_containerd_patch ────────────────────────────────────────────────────

class TestShowContainerdPatch:
    def test_contains_hosts_toml_content(self, capsys):
        _show_containerd_patch()
        out = capsys.readouterr().out
        assert "hosts.toml" in out
        assert "containerd" in out.lower()
        assert "host.docker.internal:5050" in out
        assert "http://" in out


# ── _show_pull_policy_patch ───────────────────────────────────────────────────

class TestShowPullPolicyPatch:
    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_prints_patch_command_for_owner(self, mock_cmd, capsys):
        describe_output = (
            "Name: playwright-agent-abc\n"
            "Controlled By: Job/playwright-agent-job\n"
        )
        mock_cmd.return_value = _cmd(0, describe_output)
        pods = [{"name": "playwright-agent-abc", "namespace": "default",
                 "status": "ErrImagePull", "image": "img:v1", "message": ""}]
        _show_pull_policy_patch(pods)
        out = capsys.readouterr().out
        assert "kubectl patch" in out
        assert "IfNotPresent" in out

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_no_owner_prints_manual_note(self, mock_cmd, capsys):
        mock_cmd.return_value = _cmd(0, "Name: some-pod\n")  # no Controlled By line
        pods = [{"name": "some-pod", "namespace": "default",
                 "status": "ErrImagePull", "image": None, "message": ""}]
        _show_pull_policy_patch(pods)
        out = capsys.readouterr().out
        assert "manually" in out.lower() or "patch" in out.lower()

    @patch("simulation_service_tool.menus.image_pull.run_cli_command")
    def test_deduplicates_owner(self, mock_cmd, capsys):
        describe_output = "Controlled By: Deployment/my-deploy\n"
        mock_cmd.return_value = _cmd(0, describe_output)
        pods = [
            {"name": "pod-1", "namespace": "default", "status": "ErrImagePull", "image": None, "message": ""},
            {"name": "pod-2", "namespace": "default", "status": "ErrImagePull", "image": None, "message": ""},
        ]
        _show_pull_policy_patch(pods)
        out = capsys.readouterr().out
        # my-deploy should appear only once  (deduplicated)
        assert out.count("my-deploy") == 1


# ── image_pull_menu (integration) ─────────────────────────────────────────────

class TestImagePullMenu:
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_back_exits_immediately(self, mock_scan, mock_q):
        mock_scan.return_value = []
        mock_q.select.return_value.ask.return_value = "back"
        image_pull_menu()  # should return without hanging
        assert mock_scan.call_count == 1

    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_none_choice_exits(self, mock_scan, mock_q):
        mock_scan.return_value = []
        mock_q.select.return_value.ask.return_value = None
        image_pull_menu()
        assert mock_scan.call_count == 1

    @patch("simulation_service_tool.menus.image_pull._show_push_commands")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_show_push_then_back(self, mock_scan, mock_q, mock_cont, mock_push):
        failing = [{"name": "p", "status": "ErrImagePull", "image": "img:v1",
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        # First iter: pick show_push, second iter: pick back
        mock_q.select.return_value.ask.side_effect = ["show_push", "back"]
        image_pull_menu()
        assert mock_push.call_count == 1
        assert mock_cont.call_count == 1

    @patch("simulation_service_tool.menus.image_pull.check_local_registry")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_check_registry_then_back(self, mock_scan, mock_q, mock_cont, mock_reg):
        failing = [{"name": "p", "status": "ErrImagePull", "image": None,
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        mock_reg.return_value = {"reachable": True, "host": "localhost", "port": 5050, "error": None}
        mock_q.select.return_value.ask.side_effect = ["check_registry", "back"]
        image_pull_menu()
        mock_reg.assert_called_once()

    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_rescan_reruns_scan(self, mock_scan, mock_q, mock_cont):
        mock_scan.return_value = []
        mock_q.select.return_value.ask.side_effect = ["rescan", "back"]
        image_pull_menu()
        # scan called once at start + once for rescan
        assert mock_scan.call_count == 2

    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_keyboard_interrupt_exits_cleanly(self, mock_scan, mock_q):
        mock_scan.return_value = []
        mock_q.select.return_value.ask.side_effect = KeyboardInterrupt
        image_pull_menu()  # must not propagate

    @patch("simulation_service_tool.menus.image_pull.run_push_commands")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_run_push_confirmed_executes_batch(self, mock_scan, mock_q, mock_cont, mock_run_push):
        failing = [{"name": "p", "status": "ErrImagePull", "image": "playwright:v1",
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        mock_run_push.return_value = [
            {"cmd": "docker pull playwright:v1", "returncode": 0, "stderr": ""},
            {"cmd": "docker tag  playwright:v1 host.docker.internal:5050/playwright:v1", "returncode": 0, "stderr": ""},
            {"cmd": "docker push host.docker.internal:5050/playwright:v1", "returncode": 0, "stderr": ""},
        ]
        # select: run_push then back; confirm: True
        mock_q.select.return_value.ask.side_effect = ["run_push", "back"]
        mock_q.confirm.return_value.ask.return_value = True
        image_pull_menu()
        mock_run_push.assert_called_once()

    @patch("simulation_service_tool.menus.image_pull.run_push_commands")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_run_push_declined_skips_batch(self, mock_scan, mock_q, mock_cont, mock_run_push):
        failing = [{"name": "p", "status": "ErrImagePull", "image": "playwright:v1",
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        mock_q.select.return_value.ask.side_effect = ["run_push", "back"]
        mock_q.confirm.return_value.ask.return_value = False
        image_pull_menu()
        mock_run_push.assert_not_called()

    @patch("simulation_service_tool.menus.image_pull.delete_failing_pods")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_delete_pods_confirmed(self, mock_scan, mock_q, mock_cont, mock_del):
        failing = [{"name": "p", "status": "ErrImagePull", "image": "img:v1",
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        mock_q.select.return_value.ask.side_effect = ["delete_pods", "back"]
        mock_q.confirm.return_value.ask.return_value = True
        mock_del.return_value = [{"name": "p", "returncode": 0, "stderr": ""}]
        image_pull_menu()
        mock_del.assert_called_once_with(failing)
        # rescan triggered after delete
        assert mock_scan.call_count == 2

    @patch("simulation_service_tool.menus.image_pull.delete_failing_pods")
    @patch("simulation_service_tool.menus.image_pull._prompt_continue")
    @patch("simulation_service_tool.menus.image_pull.questionary")
    @patch("simulation_service_tool.menus.image_pull.scan_image_pull_errors")
    def test_delete_pods_declined_skips(self, mock_scan, mock_q, mock_cont, mock_del):
        failing = [{"name": "p", "status": "ErrImagePull", "image": "img:v1",
                    "message": "", "namespace": "default"}]
        mock_scan.return_value = failing
        mock_q.select.return_value.ask.side_effect = ["delete_pods", "back"]
        mock_q.confirm.return_value.ask.return_value = False
        image_pull_menu()
        mock_del.assert_not_called()
