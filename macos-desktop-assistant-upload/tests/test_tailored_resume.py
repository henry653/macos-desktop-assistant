from __future__ import annotations

import unittest

from tailored_resume import tailor


class TailoredResumeTests(unittest.TestCase):
    def test_only_user_authored_text_is_selected(self) -> None:
        profile = {
            "name": "Example Student", "contact": "student@example.test",
            "experience": [{"title": "Intern", "details": "Summer", "bullets": [
                "Built a Python data pipeline.", "Created a poster."]}],
            "projects": [{"title": "Website", "bullets": ["Designed a page."]},
                         {"title": "ETL", "bullets": ["Built a Python data pipeline."]}],
            "skills": ["Python", "Illustration"],
        }
        result = tailor(profile, "Python data engineering internship")
        self.assertEqual(result["sections"][1]["entries"][0]["title"], "ETL")
        self.assertEqual(result["skills"][0], "Python")
        original_bullets = {bullet for section in (profile["experience"], profile["projects"])
                            for entry in section for bullet in entry["bullets"]}
        output_bullets = {bullet for section in result["sections"]
                          for entry in section["entries"] for bullet in entry["bullets"]}
        self.assertLessEqual(output_bullets, original_bullets)

    def test_rejects_missing_profile_facts(self) -> None:
        with self.assertRaises(ValueError):
            tailor({"name": "Student"}, "Engineering internship")
