import sys
import logging
import unittest
from unittest.mock import patch, MagicMock

try:
    import PySide6

    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is required for desktop main UI tests")
class DesktopMainTests(unittest.TestCase):
    def test_main_execution_and_exception_hook(self):
        from obs_tool.desktop import main

        fake_dir = "/tmp/fake_diagnostics_dir"
        mock_window_instance = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.exec.return_value = 0

        original_excepthook = sys.excepthook

        try:
            with (
                patch(
                    "obs_tool.diagnostics.install", return_value=fake_dir
                ) as mock_install,
                patch("obs_tool.desktop.QApplication") as mock_qapp_cls,
                patch(
                    "obs_tool.desktop.Window", return_value=mock_window_instance
                ) as mock_window_cls,
                patch("obs_tool.desktop.QFont") as mock_qfont,
                patch("logging.info") as mock_log_info,
                patch("logging.error") as mock_log_error,
            ):

                mock_qapp_cls.instance.return_value = mock_app_instance

                res = main()

                mock_install.assert_called_once()
                mock_window_cls.assert_called_once()
                mock_window_instance.show.assert_called_once()
                mock_app_instance.exec.assert_called_once()
                self.assertEqual(res, 0)

                # Test custom excepthook installation and behavior
                self.assertNotEqual(sys.excepthook, original_excepthook)

                # Trigger excepthook
                test_err = RuntimeError("Desktop main UI exception test")
                sys.excepthook(RuntimeError, test_err, None)

                mock_log_error.assert_called_once()
                mock_window_instance.notify.assert_called_once_with(
                    "操作未完成",
                    f"Desktop main UI exception test\n详细记录已保存到：{fake_dir}",
                )
        finally:
            sys.excepthook = original_excepthook


if __name__ == "__main__":
    unittest.main()
