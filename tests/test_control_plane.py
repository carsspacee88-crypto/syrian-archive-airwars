from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from pwdlib import PasswordHash
except ModuleNotFoundError:
    VPS_DEPENDENCIES_AVAILABLE = False
else:
    VPS_DEPENDENCIES_AVAILABLE = True


if VPS_DEPENDENCIES_AVAILABLE:
    TEST_DB = Path("/tmp/syrian-archive-control-plane-test.sqlite3")
    TEST_DB.unlink(missing_ok=True)
    os.environ["ARCHIVE_DATABASE_URL"] = f"sqlite:///{TEST_DB}"
    os.environ["ARCHIVE_ADMIN_USERNAME"] = "admin"
    os.environ["ARCHIVE_ADMIN_PASSWORD_HASH"] = PasswordHash.recommended().hash(
        "test-password"
    )
    os.environ["ARCHIVE_SESSION_SECRET"] = (
        "a-test-session-secret-with-at-least-thirty-two-characters"
    )
    os.environ["ARCHIVE_SECURE_COOKIES"] = "false"

    from control_plane.config import get_settings
    from control_plane.db import reset_database_caches

    get_settings.cache_clear()
    reset_database_caches()
    main_module = importlib.import_module("control_plane.main")
    from control_plane.tasks import JobProgressSink


@unittest.skipUnless(
    VPS_DEPENDENCIES_AVAILABLE,
    "VPS dependencies are installed from requirements-vps.txt",
)
class ControlPlaneTests(unittest.TestCase):
    @staticmethod
    def login(client: TestClient) -> None:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    @staticmethod
    def csrf(client: TestClient) -> str:
        response = client.get("/admin/jobs/new")
        assert response.status_code == 200
        marker = 'name="csrf" value="'
        return response.text.split(marker, 1)[1].split('"', 1)[0]

    def test_login_and_create_range_without_running_collection(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch(
                "control_plane.main.enqueue_job", return_value="task-test"
            ) as enqueue,
        ):
            self.login(client)
            response = client.post(
                "/admin/jobs",
                data={
                    "csrf": self.csrf(client),
                    "first_sequence": "151",
                    "last_sequence": "300",
                    "collect_incidents": "on",
                    "collect_sources": "on",
                    "write_report": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith("/admin/jobs/"))
            enqueue.assert_called_once()

    def test_v4_pages_render_mobile_and_live_monitor_controls(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch("control_plane.main.enqueue_job", return_value="task-ui"),
        ):
            self.login(client)
            new_page = client.get("/admin/jobs/new")
            self.assertEqual(new_page.status_code, 200)
            self.assertIn('name="viewport"', new_page.text)
            self.assertIn("ملف الأداء", new_page.text)
            created = client.post(
                "/admin/jobs",
                data={
                    "csrf": self.csrf(client),
                    "first_sequence": "501",
                    "last_sequence": "502",
                    "collect_incidents": "on",
                    "collect_sources": "on",
                },
                follow_redirects=False,
            )
            job_page = client.get(created.headers["location"])
            self.assertEqual(job_page.status_code, 200)
            self.assertIn("المحرك 4.0.0", job_page.text)
            self.assertIn('id="live-state"', job_page.text)
            self.assertIn('id="host-breakdown"', job_page.text)
            self.assertIn('id="coverage-rate"', job_page.text)
            self.assertIn('id="network-p50"', job_page.text)

    def test_reversed_range_is_rejected(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch("control_plane.main.enqueue_job") as enqueue,
        ):
            self.login(client)
            response = client.post(
                "/admin/jobs",
                data={
                    "csrf": self.csrf(client),
                    "first_sequence": "300",
                    "last_sequence": "151",
                    "collect_incidents": "on",
                },
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("النطاق يجب", response.text)
            enqueue.assert_not_called()

    def test_queued_job_can_pause_and_resume_without_losing_state(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch(
                "control_plane.main.enqueue_job", return_value="task-control"
            ) as enqueue,
        ):
            self.login(client)
            token = self.csrf(client)
            created = client.post(
                "/admin/jobs",
                data={
                    "csrf": token,
                    "first_sequence": "301",
                    "last_sequence": "302",
                    "collect_incidents": "on",
                },
                follow_redirects=False,
            )
            location = created.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            paused = client.post(
                f"/admin/jobs/{job_id}/action",
                data={"csrf": token, "action": "pause"},
                follow_redirects=False,
            )
            self.assertEqual(paused.status_code, 303)
            self.assertEqual(
                client.get(f"/admin/api/jobs/{job_id}").json()["status"], "paused"
            )
            resumed = client.post(
                f"/admin/jobs/{job_id}/action",
                data={"csrf": token, "action": "resume"},
                follow_redirects=False,
            )
            self.assertEqual(resumed.status_code, 303)
            self.assertEqual(
                client.get(f"/admin/api/jobs/{job_id}").json()["status"], "queued"
            )
            self.assertEqual(enqueue.call_count, 2)

    def test_queue_outage_is_recorded_instead_of_starting_collection(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch(
                "control_plane.main.enqueue_job",
                side_effect=ConnectionError("redis unavailable"),
            ),
        ):
            self.login(client)
            created = client.post(
                "/admin/jobs",
                data={
                    "csrf": self.csrf(client),
                    "first_sequence": "303",
                    "last_sequence": "304",
                    "collect_sources": "on",
                },
                follow_redirects=False,
            )
            job_id = created.headers["location"].rsplit("/", 1)[-1]
            snapshot = client.get(f"/admin/api/jobs/{job_id}").json()
            self.assertEqual(snapshot["status"], "failed")
            self.assertEqual(snapshot["stage"], "queue_error")
            self.assertIn("ConnectionError", snapshot["last_error"])

    def test_live_progress_sink_updates_each_small_batch(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch("control_plane.main.enqueue_job", return_value="task-live"),
        ):
            self.login(client)
            created = client.post(
                "/admin/jobs",
                data={
                    "csrf": self.csrf(client),
                    "first_sequence": "401",
                    "last_sequence": "402",
                    "collect_sources": "on",
                },
                follow_redirects=False,
            )
            job_id = created.headers["location"].rsplit("/", 1)[-1]
            sink = JobProgressSink(job_id, interval=0.01, batch_size=2)
            sink({"kind": "catalog", "source_total": 2, "incident_total": 2})
            sink({
                "kind": "source", "identity": "source-live-a", "status": "successful",
                "duration_seconds": 0.3, "attempts": 1, "error": None,
                "detail": {"host": "example.org", "quality_score": 100},
            })
            sink({
                "kind": "source", "identity": "source-live-b", "status": "internal_error",
                "duration_seconds": 1.0, "attempts": 1, "error": "timed_out",
                "detail": {"host": "example.net", "quality_score": 0},
            })
            snapshot = client.get(f"/admin/api/jobs/{job_id}").json()
            self.assertEqual(snapshot["sources"], {
                "completed": 2,
                "failed": 1,
                "resolved": 1,
                "policy_complete": 0,
                "deferred": 0,
                "operational_errors": 1,
                "total": 2,
            })
            self.assertEqual(len(snapshot["items"]), 2)
            self.assertIn("performance", snapshot)

    def test_general_archive_project_and_analysis_workflow_are_available(self) -> None:
        with (
            TestClient(main_module.create_app(get_settings())) as client,
            patch("control_plane.main.enqueue_general_run", return_value="general-task") as enqueue,
        ):
            self.login(client)
            token = self.csrf(client)
            created = client.post(
                "/admin/archive/projects",
                data={
                    "csrf": token,
                    "name": "Library fixture",
                    "target_url": "https://library.example/catalogue",
                    "connector": "synthetic_library",
                    "allowed_domains": "library.example",
                    "scope_json": '{"records":[]}',
                    "collection_limits_json": '{"timeout_seconds":5}',
                    "rate_policy_json": '{"per_host_delay_seconds":0.1}',
                    "release_name": "library-text",
                    "text_only": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(created.status_code, 303)
            project_page = client.get(created.headers["location"])
            self.assertEqual(project_page.status_code, 200)
            self.assertIn("تحليل الموقع", project_page.text)
            self.assertIn("تشغيل عينة", project_page.text)
            self.assertIn("تشغيل كامل", project_page.text)
            project_id = created.headers["location"].rsplit("/", 1)[-1]
            run = client.post(
                f"/admin/archive/projects/{project_id}/runs",
                data={"csrf": token, "mode": "analysis", "pilot_limit": "10", "max_workers": "1"},
                follow_redirects=False,
            )
            self.assertEqual(run.status_code, 303)
            self.assertTrue(run.headers["location"].startswith("/admin/archive/runs/"))
            enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
