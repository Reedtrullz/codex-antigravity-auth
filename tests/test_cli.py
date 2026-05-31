import unittest
import urllib.request
from unittest.mock import patch, MagicMock
from codex_antigravity_auth.cli import run_doctor

class TestCliDoctor(unittest.TestCase):
    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_displays_accurate_information(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": []}
        
        # Mock successful network check
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        with patch("builtins.print") as mock_print:
            run_doctor()
            
            # Extract printed strings
            printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
            printed_text = "\n".join(printed_args)
            
            self.assertIn("Configured", printed_text)
            self.assertIn("Token Storage Encryption", printed_text)

if __name__ == "__main__":
    unittest.main()
