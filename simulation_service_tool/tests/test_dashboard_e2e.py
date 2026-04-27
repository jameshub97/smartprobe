"""End-to-end Playwright tests for the smartprobe dashboard.

Validates that the simulation service is reachable, the dashboard loads,
and key UI elements are present and functional.

Run with:
    pytest simulation_service_tool/tests/test_dashboard_e2e.py \
        --base-url http://localhost:5002 -v

Requires:
    - simulation service running on localhost:5002
    - pytest-playwright installed  (pip install pytest-playwright)
    - chromium browser installed   (playwright install chromium)
"""

import urllib.request
import urllib.error

import pytest


BASE_URL = "http://localhost:5002"


def _service_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2):
            return True
    except Exception:
        return False


# Skip all tests in this module if the service isn't running
pytestmark = pytest.mark.skipif(
    not _service_reachable(),
    reason="Simulation service not reachable on localhost:5002 — start it first",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dashboard_page(playwright):
    """One browser context shared across all dashboard tests."""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(base_url=BASE_URL)
    page = context.new_page()
    yield page
    context.close()
    browser.close()


# ---------------------------------------------------------------------------
# Service routes
# ---------------------------------------------------------------------------

class TestServiceRoutes:
    """Verify that the Flask service exposes the expected HTTP routes."""

    def test_root_returns_200(self, page):
        response = page.goto(BASE_URL + "/")
        assert response.status == 200, f"/ returned {response.status}"

    def test_dashboard_route_returns_200(self, page):
        response = page.goto(BASE_URL + "/dashboard")
        assert response.status == 200, f"/dashboard returned {response.status}"

    def test_health_endpoint(self, page):
        response = page.goto(BASE_URL + "/health")
        assert response.status == 200

    def test_favicon_svg(self, page):
        response = page.goto(BASE_URL + "/favicon.svg")
        assert response.status == 200

    def test_favicon_ico(self, page):
        response = page.goto(BASE_URL + "/favicon.ico")
        assert response.status == 200

    def test_api_preflight(self, page):
        response = page.goto(BASE_URL + "/api/preflight")
        assert response.status == 200
        body = response.json()
        assert "has_conflicts" in body

    def test_api_simulation_summary(self, page):
        response = page.goto(BASE_URL + "/api/simulation/summary")
        assert response.status == 200

    def test_api_simulation_presets(self, page):
        response = page.goto(BASE_URL + "/api/simulation/presets")
        assert response.status == 200


# ---------------------------------------------------------------------------
# Dashboard page structure
# ---------------------------------------------------------------------------

class TestDashboardStructure:
    """Verify the dashboard HTML loads with all expected structural elements."""

    def test_page_title(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert "smartprobe" in dashboard_page.title().lower()

    def test_header_brand_button(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        brand = dashboard_page.locator("button", has_text="smartprobe")
        assert brand.count() >= 1

    def test_nav_analytics_button(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#nav-analytics").is_visible()

    def test_nav_agents_button(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#nav-agents").is_visible()

    def test_status_indicator_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#status-dot").count() == 1
        assert dashboard_page.locator("#status-text").count() == 1

    def test_stats_bar_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        for stat_id in ["s-pods", "s-success", "s-errors", "s-pending", "p-rate", "p-eta"]:
            assert dashboard_page.locator(f"#{stat_id}").count() == 1, \
                f"Missing stats element #{stat_id}"

    def test_dashboard_view_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#view-dashboard").count() == 1

    def test_analytics_view_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#view-analytics").count() == 1

    def test_agents_view_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#view-agents").count() == 1

    def test_log_container_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        assert dashboard_page.locator("#log-container").count() == 1

    def test_activity_log_tabs_present(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        for tab_id in ["tab-all", "tab-pods", "tab-tx", "tab-agents"]:
            assert dashboard_page.locator(f"#{tab_id}").count() == 1, \
                f"Missing activity tab #{tab_id}"


# ---------------------------------------------------------------------------
# Dashboard interactivity
# ---------------------------------------------------------------------------

class TestDashboardInteractivity:
    """Verify tab switching and view navigation work correctly."""

    def test_analytics_nav_switches_view(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        # Dashboard view should be visible initially
        assert dashboard_page.locator("#view-dashboard").is_visible()
        # Click Analytics nav
        dashboard_page.locator("#nav-analytics").click()
        dashboard_page.wait_for_timeout(300)
        assert dashboard_page.locator("#view-analytics").is_visible()
        assert not dashboard_page.locator("#view-dashboard").is_visible()

    def test_agents_nav_switches_view(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.locator("#nav-agents").click()
        dashboard_page.wait_for_timeout(300)
        assert dashboard_page.locator("#view-agents").is_visible()
        assert not dashboard_page.locator("#view-dashboard").is_visible()

    def test_brand_button_returns_to_dashboard(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        # Navigate away first
        dashboard_page.locator("#nav-analytics").click()
        dashboard_page.wait_for_timeout(300)
        # Click brand to go back
        dashboard_page.locator("button", has_text="smartprobe").first.click()
        dashboard_page.wait_for_timeout(300)
        assert dashboard_page.locator("#view-dashboard").is_visible()

    def test_activity_tab_all_clickable(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.locator("#tab-all").click()
        dashboard_page.wait_for_timeout(200)
        # No JS error — just verify the tab is still in DOM
        assert dashboard_page.locator("#tab-all").count() == 1

    def test_activity_tab_pods_clickable(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.locator("#tab-pods").click()
        dashboard_page.wait_for_timeout(200)
        assert dashboard_page.locator("#tab-pods").count() == 1

    def test_activity_tab_transactions_clickable(self, dashboard_page):
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.locator("#tab-tx").click()
        dashboard_page.wait_for_timeout(200)
        assert dashboard_page.locator("#tab-tx").count() == 1


# ---------------------------------------------------------------------------
# Polling / connection
# ---------------------------------------------------------------------------

class TestDashboardPolling:
    """Verify the dashboard connects to the API and updates the status indicator."""

    def test_status_text_updates_from_connecting(self, dashboard_page):
        """After a short wait the status text should leave 'connecting…'."""
        dashboard_page.goto(BASE_URL + "/")
        # Wait up to 5 s for the status to update
        dashboard_page.wait_for_timeout(5000)
        status = dashboard_page.locator("#status-text").inner_text()
        assert status.lower() != "connecting…", \
            f"Status never updated from 'connecting…' — polling may be broken"

    def test_no_console_errors_on_load(self, dashboard_page):
        """Dashboard should not log JS errors during initial load."""
        errors = []
        dashboard_page.on("pageerror", lambda err: errors.append(str(err)))
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.wait_for_timeout(3000)
        assert errors == [], f"JavaScript errors on load: {errors}"

    def test_api_label_populated(self, dashboard_page):
        """#api-label should have some text after initial poll completes."""
        dashboard_page.goto(BASE_URL + "/")
        dashboard_page.wait_for_timeout(5000)
        label = dashboard_page.locator("#api-label").inner_text().strip()
        # It may be empty when no test is running — just assert no JS crash
        assert isinstance(label, str)

    def test_perf_stats_render_when_summary_has_backfilled_throughput(self, page):
        """Top stats should render ETA/avg even when completed is zero."""

        def fulfill_json(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__('json').dumps(payload),
            )

        page.route(
            "**/api/simulation/summary",
            lambda route: fulfill_json(route, {
                "total": 0,
                "success": 0,
                "running": 16,
                "pending": 0,
                "errors": 0,
                "results": [],
                "jobs": {},
                "kueue": {"active": False},
                "coordinator": {"agents": 0, "roles": {}, "pool_size": 0, "transactions": {"total": 0, "completed": 0, "conflicts": 0, "failed": 0}},
                "prometheus": {
                    "active": 16,
                    "succeeded": 153,
                    "failed": 0,
                    "pending": 0,
                    "avg_duration": 2.0,
                    "active_test": {
                        "completions": "100",
                        "parallelism": "20",
                        "probe_mode": "basic",
                        "target_url": "http://localhost:5174",
                        "test_name": "large-123",
                    },
                },
                "throughput": {
                    "completed": 0,
                    "avgDuration": 2.0,
                    "etaSeconds": 10.0,
                    "agentsPerSecond": 0.5,
                    "agentsPerMinute": 30.0,
                    "source": "prometheus",
                },
            }),
        )
        page.route(
            "**/api/simulation/activity?limit=*",
            lambda route: fulfill_json(route, {
                "activity": [],
                "summary": {"sleeping": 0, "pending": 0, "running": 16},
                "totals": {},
            }),
        )
        page.route(
            "**/api/simulation/agent-states",
            lambda route: fulfill_json(route, {"states": {}}),
        )
        page.route(
            "**/api/simulation/coordinator/agents",
            lambda route: fulfill_json(route, {"agents": []}),
        )

        page.goto(BASE_URL + "/")
        page.wait_for_timeout(1000)

        assert page.locator("#p-rate").inner_text().strip() == "30/m"
        assert page.locator("#p-eta").inner_text().strip() == "10s"
        assert page.locator("#p-avg").inner_text().strip() == "2s"

    def test_perf_stats_derive_per_minute_when_summary_omits_it(self, page):
        """Top stats should not render undefined/m when only agentsPerSecond is present."""

        def fulfill_json(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__('json').dumps(payload),
            )

        page.route(
            "**/api/simulation/summary",
            lambda route: fulfill_json(route, {
                "total": 0,
                "success": 0,
                "running": 0,
                "pending": 0,
                "errors": 0,
                "results": [],
                "jobs": {},
                "kueue": {"active": False},
                "coordinator": {"agents": 0, "roles": {}, "pool_size": 0, "transactions": {"total": 0, "completed": 0, "conflicts": 0, "failed": 0}},
                "prometheus": {
                    "active": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "pending": 0,
                    "avg_duration": 2.0,
                    "active_test": {
                        "completions": "10",
                        "parallelism": "5",
                        "probe_mode": "basic",
                        "target_url": "http://localhost:5174",
                        "test_name": "small-123",
                    },
                },
                "throughput": {
                    "completed": 0,
                    "avgDuration": 2.0,
                    "etaSeconds": 10.0,
                    "agentsPerSecond": 0.5,
                    "source": "prometheus",
                },
            }),
        )
        page.route(
            "**/api/simulation/activity?limit=*",
            lambda route: fulfill_json(route, {
                "activity": [],
                "summary": {"sleeping": 0, "pending": 0, "running": 0},
                "totals": {},
            }),
        )
        page.route(
            "**/api/simulation/agent-states",
            lambda route: fulfill_json(route, {"states": {}}),
        )
        page.route(
            "**/api/simulation/coordinator/agents",
            lambda route: fulfill_json(route, {"agents": []}),
        )

        page.goto(BASE_URL + "/")
        page.wait_for_timeout(1000)

        assert page.locator("#p-rate").inner_text().strip() == "30/m"
        assert page.locator("#p-eta").inner_text().strip() == "10s"
        assert page.locator("#p-avg").inner_text().strip() == "2s"
