"""Run DOM/fetch/timer unit tests through JavaScriptCore; no browser or server."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
JSC = str(SYSTEM_JSC) if SYSTEM_JSC.is_file() else shutil.which("jsc")


class FrontendInteractions(unittest.TestCase):
    @unittest.skipUnless(JSC, "JavaScriptCore command-line runtime is unavailable")
    def test_real_app_interactions(self):
        result = subprocess.run(
            [JSC, "web_control/tests/frontend_interactions.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        # jsc quit() can exit successfully after a caught asynchronous error.
        self.assertIn("Frontend interaction tests passed:", output, output)
        self.assertNotIn("FAIL:", output, output)


if __name__ == "__main__":
    unittest.main()
