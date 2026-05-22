"""Smoke tests for the VedaVine response parser and image downscaler.

Run with: python -m unittest test_app.py

Uses a temporary config (via VEDAVINE_CONFIG env var) so the user's real
config.yaml is never touched. Does not require a live Ollama instance.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent

# Set up an isolated config BEFORE importing app — load_config() runs at import.
_tmp_dir = tempfile.mkdtemp(prefix="vedavine-test-")
_tmp_cfg = Path(_tmp_dir) / "config.yaml"
_tmp_cfg.write_text(
    'vineyard:\n'
    '  name: "Test Vineyard"\n'
    '  location: "Test Region"\n'
    '  varietal: "Test Varietal"\n'
    '  notes: "test"\n'
    'flask:\n'
    '  secret_key: "test-secret-not-the-placeholder"\n'
    '  debug: false\n'
    '  port: 5000\n'
    'ollama:\n'
    '  host: "http://localhost:11434"\n'
    '  model: "gemma4:e2b"\n'
    '  timeout: 5\n'
)
os.environ["VEDAVINE_CONFIG"] = str(_tmp_cfg)

sys.path.insert(0, str(_THIS_DIR))
import app  # noqa: E402


class ParseResponseTests(unittest.TestCase):
    def test_clean_json(self):
        raw = (
            '{"severity": "Healthy", "summary": "Strong canopy.",'
            ' "observations": ["Vivid green leaves"],'
            ' "recommendations": ["Maintain current irrigation"]}'
        )
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["severity"], "Healthy")
        self.assertEqual(out["summary"], "Strong canopy.")
        self.assertEqual(out["observations"], ["Vivid green leaves"])
        self.assertEqual(out["recommendations"], ["Maintain current irrigation"])

    def test_markdown_fence_stripped(self):
        raw = (
            "```json\n"
            '{"severity": "Monitor", "summary": "Some yellowing.",'
            ' "observations": [], "recommendations": []}\n'
            "```"
        )
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["severity"], "Monitor")
        self.assertIn("yellowing", out["summary"])

    def test_trailing_commentary_extracted(self):
        raw = (
            'Sure, here is the result:\n'
            '{"severity": "Attention Needed", "summary": "Mildew suspected.",'
            ' "observations": ["white residue on undersides"],'
            ' "recommendations": ["spray sulfur"]}\n'
            'Hope this helps!'
        )
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["severity"], "Attention Needed")
        self.assertEqual(out["recommendations"], ["spray sulfur"])

    def test_invalid_severity_falls_back_to_monitor(self):
        raw = (
            '{"severity": "Catastrophic", "summary": "x",'
            ' "observations": [], "recommendations": []}'
        )
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["severity"], "Monitor")

    def test_unparseable_falls_back(self):
        raw = "the leaves look fine I think"
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["severity"], "Monitor")
        self.assertIn("leaves", out["summary"])
        self.assertEqual(out["observations"], [])

    def test_empty_input_does_not_crash(self):
        out = app.parse_ollama_response("")
        self.assertEqual(out["severity"], "Monitor")
        self.assertTrue(out["summary"])

    def test_observations_coerced_to_strings(self):
        raw = '{"severity": "Healthy", "summary": "ok", "observations": [1, 2], "recommendations": []}'
        out = app.parse_ollama_response(raw)
        self.assertEqual(out["observations"], ["1", "2"])


class SystemPromptTests(unittest.TestCase):
    def test_no_reference_omits_section(self):
        p = app.build_system_prompt()
        self.assertIn("Test Vineyard", p)
        self.assertNotIn("Reference material", p)
        # the JSON spec must remain the last instruction
        self.assertTrue(p.rstrip().endswith("}"))

    def test_reference_injected_before_json_spec(self):
        p = app.build_system_prompt("- (foo.pdf p.1) example corpus passage")
        self.assertIn("Reference material", p)
        self.assertIn("example corpus passage", p)
        self.assertLess(p.index("Reference material"), p.index("Respond ONLY"))

    def test_weather_injected_before_reference_and_json(self):
        p = app.build_system_prompt(
            reference="- (foo.pdf p.1) corpus text",
            weather="Today: Rain likely, 68F, humidity ~90%.",
        )
        self.assertIn("Current local weather", p)
        self.assertIn("humidity ~90%", p)
        # weather first, then corpus reference, then the JSON instruction
        self.assertLess(p.index("Current local weather"), p.index("Reference material"))
        self.assertLess(p.index("Reference material"), p.index("Respond ONLY"))

    def test_weather_disabled_returns_empty(self):
        # Test config has no weather section -> WEATHER_ENABLED False -> no call.
        self.assertEqual(app.get_weather_context(), "")

    def test_vineyard_override_repoints_prompt(self):
        p = app.build_system_prompt(
            vineyard={
                "name": "your vineyard",
                "location": "Sta. Rita Hills, CA",
                "varietal": "Pinot Noir",
                "notes": "",
            }
        )
        self.assertIn("Sta. Rita Hills", p)
        self.assertIn("Pinot Noir", p)
        self.assertNotIn("Test Varietal", p)  # config defaults not used when overridden

    def test_retrieve_context_fails_soft(self):
        # No rag.db / no Ollama on the test host — must degrade to no grounding,
        # never raise, so /analyze keeps working.
        app._rag_cache["reference"] = None  # ensure we exercise the lookup path
        ref, sources = app.retrieve_context()
        self.assertEqual(ref, "")
        self.assertEqual(sources, [])

    def test_profile_query_includes_vineyard(self):
        q = app._profile_query()
        self.assertIn("Test Varietal", q)
        self.assertIn("Test Region", q)

    def test_dynamic_retrieval_fails_soft(self):
        # The concern-driven (dynamic) branch must also degrade gracefully when
        # the embedder/index is unavailable.
        original = app.RAG_DYNAMIC
        app.RAG_DYNAMIC = True
        try:
            ref, sources = app.retrieve_context("why are the leaves yellowing?")
        finally:
            app.RAG_DYNAMIC = original
        self.assertEqual(ref, "")
        self.assertEqual(sources, [])


class ImagePreprocessTests(unittest.TestCase):
    def test_downscale_jpeg(self):
        from PIL import Image
        import io as _io

        img = Image.new("RGB", (4000, 3000), color=(120, 60, 30))
        buf = _io.BytesIO()
        img.save(buf, format="JPEG")
        out = app.preprocess_image(buf.getvalue())
        re = Image.open(_io.BytesIO(out))
        self.assertLessEqual(max(re.size), app.MAX_IMAGE_EDGE)


if __name__ == "__main__":
    unittest.main()
