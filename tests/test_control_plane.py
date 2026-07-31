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


if __name__ == "__main__":
    unittest.main()
