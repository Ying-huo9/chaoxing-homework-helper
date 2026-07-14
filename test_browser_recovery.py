from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from backend import Api


class AlivePage:
    url = "https://i.chaoxing.com"

    def run_cdp(self, command: str):
        if command != "Browser.getVersion":
            raise AssertionError(command)
        return {"product": "Chrome/test"}


class DeadPage:
    @property
    def url(self):
        raise RuntimeError("page disconnected")


class TrackingWindow:
    def __init__(self) -> None:
        self.events: list[str] = []

    def show(self) -> None:
        self.events.append("show")

    def hide(self) -> None:
        self.events.append("hide")


class TrackingSetter:
    def __init__(self, window: TrackingWindow) -> None:
        self.window = window


class TrackingPage(AlivePage):
    def __init__(self, url: str = "https://i.chaoxing.com") -> None:
        self.url = url
        self.window = TrackingWindow()
        self.set = TrackingSetter(self.window)


def wait_for_job(api: Api, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.get_job(job_id)["job"]
        if job.get("state") != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {job_id}")


class BrowserRecoveryTests(unittest.TestCase):
    def test_login_replaces_a_disconnected_page(self) -> None:
        api = Api()
        api.page = DeadPage()
        api.jobs["login-test"] = {"id": "login-test", "state": "running"}
        replacement = AlivePage()

        with (
            patch("chaoxing_auto.cleanup_chrome_tmp_processes", return_value=0),
            patch("chaoxing_auto.make_page", return_value=replacement) as make_page,
            patch("chaoxing_auto.login", return_value={"ok": True}),
            patch("chaoxing_scraper.discover_courses", return_value=[{"name": "课程 A"}]),
        ):
            api._run_login_job("login-test", "user", "password")

        self.assertIs(api.page, replacement)
        self.assertEqual(make_page.call_count, 1)
        self.assertEqual(api.get_job("login-test")["job"]["state"], "done")

    def test_successful_login_hides_browser_in_background_mode(self) -> None:
        api = Api()
        api.jobs["login-test"] = {"id": "login-test", "state": "running"}
        page = TrackingPage()

        with (
            patch("chaoxing_auto.make_page", return_value=page),
            patch("chaoxing_auto.login", return_value={"ok": True}),
            patch("chaoxing_scraper.discover_courses", return_value=[{"name": "课程 A"}]),
        ):
            api._run_login_job("login-test", "user", "password", "edge:test", False)

        self.assertEqual(page.window.events, ["hide"])
        self.assertTrue(api.browser_window_hidden)
        self.assertEqual(api.active_browser_choice, "edge:test")

    def test_verification_reshows_a_hidden_browser(self) -> None:
        api = Api()
        page = TrackingPage("https://passport2.chaoxing.com/login")
        api.page = page
        api.active_browser_choice = "chrome:test"
        api.browser_window_hidden = True
        api.jobs["login-test"] = {"id": "login-test", "state": "running"}

        with patch("chaoxing_auto.login", return_value={"ok": True, "verification_required": True}):
            api._run_login_job("login-test", "user", "password", "chrome:test", False)

        self.assertIn("show", page.window.events)
        self.assertFalse(api.browser_window_hidden)
        self.assertTrue(api.browser_verification_required)
        self.assertEqual(api.get_job("login-test")["job"]["state"], "verification_required")

    def test_visibility_switch_updates_a_live_browser(self) -> None:
        api = Api()
        page = TrackingPage()
        api.page = page
        api.browser_window_hidden = True

        result = api.online_set_browser_visibility(True)

        self.assertTrue(result["visible"])
        self.assertEqual(page.window.events, ["show"])
        self.assertFalse(api.browser_window_hidden)

    def test_new_browser_choice_recreates_the_controlled_browser(self) -> None:
        api = Api()
        api.page = TrackingPage()
        api.active_browser_choice = "chrome:test"
        replacement = TrackingPage()
        api.jobs["login-test"] = {"id": "login-test", "state": "running"}

        with (
            patch("chaoxing_auto.cleanup_chrome_tmp_processes", return_value=1),
            patch("chaoxing_auto.make_page", return_value=replacement) as make_page,
            patch("chaoxing_auto.login", return_value={"ok": True}),
            patch("chaoxing_scraper.discover_courses", return_value=[]),
        ):
            api._run_login_job("login-test", "user", "password", "edge:test", True)

        make_page.assert_called_once_with(show_browser=True, browser_choice="edge:test")
        self.assertIs(api.page, replacement)
        self.assertEqual(api.active_browser_choice, "edge:test")

    def test_expired_login_reshows_browser_during_course_read(self) -> None:
        api = Api()
        page = TrackingPage("https://passport2.chaoxing.com/login")
        api.page = page
        api.browser_window_hidden = True

        result = api.online_load_content({"name": "课程 A", "course_id": "1"})
        job = wait_for_job(api, result["job_id"])

        self.assertEqual(job["state"], "verification_required")
        self.assertTrue(job["browser_attention"])
        self.assertEqual(page.window.events, ["show"])

    def test_two_courses_can_be_loaded_sequentially(self) -> None:
        api = Api()
        api.page = AlivePage()

        def load(_page, course, progress_callback=None):
            if progress_callback:
                progress_callback(1, 1, "读取完成")
            return [{"title": f"{course['name']}作业"}]

        with patch("chaoxing_auto.get_task_list", side_effect=load):
            first = api.online_load_content({"name": "课程 A", "course_id": "1"})
            first_job = wait_for_job(api, first["job_id"])
            second = api.online_load_content({"name": "课程 B", "course_id": "2"})
            second_job = wait_for_job(api, second["job_id"])

        self.assertEqual(first_job["state"], "done")
        self.assertEqual(second_job["state"], "done")
        self.assertEqual(second_job["tasks"][0]["title"], "课程 B作业")

    def test_rapid_second_course_click_does_not_queue_a_hidden_job(self) -> None:
        api = Api()
        api.page = AlivePage()
        entered = threading.Event()
        release = threading.Event()

        def slow_load(_page, _course, progress_callback=None):
            entered.set()
            self.assertTrue(release.wait(2))
            return []

        with patch("chaoxing_auto.get_task_list", side_effect=slow_load):
            first = api.online_load_content({"name": "课程 A", "course_id": "1"})
            self.assertTrue(entered.wait(1))
            second = api.online_load_content({"name": "课程 B", "course_id": "2"})
            self.assertFalse(second["ok"])
            self.assertTrue(second["busy"])
            self.assertEqual(second["job_id"], first["job_id"])
            release.set()
            wait_for_job(api, first["job_id"])

    def test_disconnection_during_course_read_resets_login_state(self) -> None:
        api = Api()
        api.page = AlivePage()

        class PageDisconnectedError(RuntimeError):
            pass

        with (
            patch("chaoxing_auto.get_task_list", side_effect=PageDisconnectedError("page disconnected")),
            patch("chaoxing_auto.cleanup_chrome_tmp_processes", return_value=0),
        ):
            result = api.online_load_content({"name": "课程 A", "course_id": "1"})
            job = wait_for_job(api, result["job_id"])

        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["browser_closed"])
        self.assertIsNone(api.page)


if __name__ == "__main__":
    unittest.main()
