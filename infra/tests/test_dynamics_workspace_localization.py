"""English form projections must never conceal missing stored Arabic labels."""

import copy
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock

from infra.tests.test_dynamics_workspace_setup import native_fixture, setup, uid


class LocalizedCaseFormTests(unittest.TestCase):
    def setUp(self):
        native = native_fixture()
        source = copy.deepcopy(native["case_form"])
        tree = ET.fromstring(source["formxml"])
        labels = ET.SubElement(tree.find(".//section[@name='native']"), "labels")
        ET.SubElement(labels, "label", description="Native section", languagecode="1033")
        source["formxml"] = ET.tostring(tree, encoding="unicode")
        source["formidunique"] = uid("source-form-version")
        payload = setup.case_form(source, [source], native["case_attributes"], uid("localized-case"), "Sample")
        self.expected = payload["formxml"]
        self.published = {
            "formid": payload["formid"], "formidunique": uid("localized-version"), "componentstate": 0,
            "isdefault": False, "_ancestorformid_value": source["formid"], "@odata.etag": 'W/"20"',
            "formxml": setup.case_form_english_projection(self.expected),
        }
        self.full = {**self.published, "formxml": self.expected}
        self.source, self.source_full = source, copy.deepcopy(source)
        self.api = MagicMock()
        self.api.get.side_effect = lambda path: copy.deepcopy({
            setup.record_path("systemforms", self.published["formid"]) + "/Microsoft.Dynamics.CRM.RetrieveUnpublished": self.full,
            setup.record_path("systemforms", source["formid"]): self.source,
            setup.record_path("systemforms", source["formid"]) + "/Microsoft.Dynamics.CRM.RetrieveUnpublished": self.source_full,
        }[path])

    def verify(self):
        return setup.localized_case_form_readback(self.api, self.published, self.expected)

    def test_native_english_projection_requires_real_stored_arabic_labels(self):
        result = self.verify()
        self.assertEqual(result["mode"], "published-english-projection-with-stored-translations")
        self.assertEqual(len(result["verified_case_labels"]), 9)
        self.assertFalse(result["browser_rendering_verified"])
        self.api.request.assert_not_called()

    def test_renderer_preserves_the_approved_shared_bilingual_heading(self):
        section = ET.fromstring(self.expected).find(".//section[@name='case_location']")
        self.assertIsNone(section.get("labelid"))
        self.assertEqual([label.attrib for label in section.findall("labels/label")],
                         [{"description": setup.CASE_SECTION_HEADING, "languagecode": "1033"}])

    def test_missing_shared_heading_is_not_normalized_away(self):
        tree = ET.fromstring(self.full["formxml"])
        group = tree.find(".//section[@name='case_location']/labels")
        group.remove(group.find("label[@languagecode='1033']"))
        self.full["formxml"] = ET.tostring(tree, encoding="unicode")
        with self.assertRaisesRegex(setup.GateError, "stored form translation"):
            self.verify()

    def test_missing_field_translation_is_not_hidden_by_english_readback(self):
        tree = ET.fromstring(self.full["formxml"])
        group = tree.find(".//section[@name='case_location']//cell/labels")
        group.remove(group.find("label[@languagecode='1025']"))
        self.full["formxml"] = ET.tostring(tree, encoding="unicode")
        with self.assertRaisesRegex(setup.GateError, "stored form translation"):
            self.verify()

    def test_unapproved_section_labelid_stays_blocked(self):
        tree = ET.fromstring(self.expected)
        tree.find(".//section[@name='case_location']").set(
            "labelid", setup.case_section_label_id(self.published["formid"]))
        self.expected = ET.tostring(tree, encoding="unicode")
        self.published["formxml"] = setup.case_form_english_projection(self.expected)
        with self.assertRaisesRegex(setup.GateError, "approved shared bilingual heading"):
            self.verify()

    def test_unrelated_xml_change_is_never_normalization(self):
        tree = ET.fromstring(self.published["formxml"])
        tree.find(".//control[@id='moacc_district']").set("disabled", "true")
        self.published["formxml"] = ET.tostring(tree, encoding="unicode")
        with self.assertRaisesRegex(setup.GateError, "beyond the four field-label"):
            self.verify()

    def test_distinct_unpublished_revision_is_not_accepted(self):
        for field, value in (("componentstate", 1), ("@odata.etag", 'W/"21"'), ("formidunique", uid("foreign-version"))):
            with self.subTest(field=field):
                old = self.full[field]
                self.full[field] = value
                with self.assertRaisesRegex(setup.GateError, "distinct unpublished"):
                    self.verify()
                self.full[field] = old

    def test_only_source_inherited_extra_translations_are_allowed(self):
        for target in (self.full, self.source_full):
            tree = ET.fromstring(target["formxml"])
            group = tree.find(".//section[@name='native']/labels")
            ET.SubElement(group, "label", description="القسم الأصلي", languagecode="1025")
            target["formxml"] = ET.tostring(tree, encoding="unicode")
        self.assertEqual(self.verify()["inherited_extra_labels"], 1)
        self.source_full["formxml"] = self.source_full["formxml"].replace("القسم الأصلي", "نص آخر")
        with self.assertRaisesRegex(setup.GateError, "non-source translation"):
            self.verify()

    def test_unapproved_separate_section_translation_is_not_inherited(self):
        tree = ET.fromstring(self.full["formxml"])
        group = tree.find(".//section[@name='case_location']/labels")
        ET.SubElement(group, "label", description="Unapproved separate section translation", languagecode="1025")
        self.full["formxml"] = ET.tostring(tree, encoding="unicode")
        with self.assertRaisesRegex(setup.GateError, "non-source translation"):
            self.verify()
        self.api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
