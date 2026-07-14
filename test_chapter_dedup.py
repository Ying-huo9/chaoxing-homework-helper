from __future__ import annotations

import json
import unittest

from chaoxing_auto import _extract_chapter_exercises, _merge_chapter_exercises


class ChapterDedupTests(unittest.TestCase):
    def test_same_work_with_different_job_ids_is_one_exercise(self) -> None:
        exercises: list[dict] = []
        index: dict[str, int] = {}
        entry = {
            "title": "1.5 作业",
            "work_id": "work-1",
            "job_id": "entry-job",
            "knowledge_id": "chapter-1",
            "url": "https://example.test/api/work",
            "completed_detail": False,
        }
        reviewed = {
            "title": "1.5 作业",
            "work_id": "work-1",
            "job_id": "reviewed-job",
            "knowledge_id": "chapter-1",
            "url": "https://example.test/selectWorkQuestionYiPiYue",
            "completed_detail": True,
        }

        self.assertEqual(_merge_chapter_exercises(exercises, index, [entry]), 1)
        self.assertEqual(_merge_chapter_exercises(exercises, index, [reviewed]), 0)
        self.assertEqual(len(exercises), 1)
        self.assertTrue(exercises[0]["completed_detail"])
        self.assertIn("selectWorkQuestionYiPiYue", exercises[0]["url"])

    def test_same_chapter_title_merges_different_pre_redirect_work_ids(self) -> None:
        exercises: list[dict] = []
        index: dict[str, int] = {}
        entry = {
            "title": "1.6 导论章节测验",
            "chapter_title": "1.6 导论章节测验",
            "work_id": "temporary-work-id",
            "knowledge_id": "temporary-chapter-id",
            "completed_detail": False,
        }
        reviewed = {
            "title": "1.6 导论章节测验",
            "chapter_title": "1.6 导论章节测验",
            "work_id": "44987036",
            "knowledge_id": "1013232642",
            "completed_detail": True,
        }

        _merge_chapter_exercises(exercises, index, [entry, reviewed])

        self.assertEqual(len(exercises), 1)
        self.assertEqual(exercises[0]["work_id"], "44987036")
        self.assertTrue(exercises[0]["completed_detail"])

    def test_attachment_and_reviewed_frame_keep_exact_chapter_title(self) -> None:
        data = {
            "defaults": {"knowledgeid": "chapter-1"},
            "attachments": [
                {
                    "type": "workid",
                    "jobid": "entry-job",
                    "property": {"workid": "work-1", "title": "章节测验"},
                }
            ],
        }
        reviewed_url = (
            "https://mooc1.chaoxing.com/mooc-ans/work/selectWorkQuestionYiPiYue"
            "?workId=work-1&jobid=reviewed-job&knowledgeid=chapter-1&workAnswerId=answer-1"
        )
        html = (
            f"<script>mArg = {json.dumps(data)};</script>"
            f'<iframe src="{reviewed_url}"></iframe>'
        )
        chapter = {
            "title": "1.5 作业",
            "chapter_id": "chapter-1",
            "knowledge_id": "chapter-1",
        }

        exercises = _extract_chapter_exercises(
            html,
            "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
            {"course_id": "course-1", "clazz_id": "class-1", "cpi": "cpi-1"},
            chapter,
            0,
        )

        self.assertEqual(len(exercises), 1)
        self.assertEqual(exercises[0]["title"], "1.5 作业")
        self.assertEqual(exercises[0]["status"], "已批阅")
        self.assertTrue(exercises[0]["completed_detail"])


if __name__ == "__main__":
    unittest.main()
