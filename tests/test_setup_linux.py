import json
import unittest
from unittest.mock import patch

from deploy.setup_wizard import (
    SetupError, apache_config, collect_profiles, pam_config,
    validate_management_ip, validate_proxy_ip,
)


class SetupWizardTests(unittest.TestCase):
    def test_management_ip_must_be_on_private_lights_out_network(self):
        self.assertEqual(validate_management_ip("10.20.30.40").compressed, "10.20.30.40")
        self.assertEqual(validate_management_ip("fd12:3456::1").compressed, "fd12:3456::1")
        for address in ("8.8.8.8", "127.0.0.1", "fe80::1", "not-an-ip"):
            with self.subTest(address=address), self.assertRaises(SetupError):
                validate_management_ip(address)

    def test_proxy_address_and_generated_virtual_hosts_are_restricted(self):
        self.assertEqual(validate_proxy_ip("192.0.2.10"), "192.0.2.10")
        with self.assertRaises(SetupError):
            validate_proxy_ip("127.0.0.1")
        config = apache_config("192.0.2.10", "/etc/ssl/noc/fullchain.pem", "/etc/ssl/noc/key.pem")
        self.assertIn("AuthPAMService noc-management", config)
        self.assertIn("AuthFormProvider PAM", config)
        self.assertIn("AuthFormLoginRequiredLocation /login", config)
        self.assertIn("SetHandler form-login-handler", config)
        self.assertIn("SetHandler form-logout-handler", config)
        self.assertIn("SessionCookieRemove On", config)
        self.assertIn("SessionCryptoPassphraseFile /etc/apache2/noc-session-key", config)
        self.assertIn("__Host-noc-management path=/;HttpOnly;Secure;SameSite=Strict", config)
        self.assertGreaterEqual(config.count("Require ip 192.0.2.10"), 4)
        self.assertIn("Require ip 192.0.2.10", config)
        self.assertIn("ProxyPass / http://127.0.0.1:8081/", config)
        self.assertIn("RequestHeader unset Authorization", config)
        self.assertNotIn("AuthType Basic", config)
        self.assertIn("pam_succeed_if.so user ingroup noc-admin", pam_config())

    @patch("deploy.setup_wizard.confirm", return_value=False)
    @patch("deploy.setup_wizard.getpass.getpass", return_value="readonly-password")
    @patch("deploy.setup_wizard.prompt", side_effect=["server-bmc", "1", "reader", ""])
    def test_profile_wizard_stores_redfish_password_in_secret_json(self, prompt, getpass, confirm):
        profile_document = collect_profiles()
        self.assertEqual(
            json.loads(profile_document),
            {"profiles": {"server-bmc": {
                "protocol": "redfish",
                "username": "reader",
                "password": "readonly-password",
            }}},
        )

    @patch("deploy.setup_wizard.getpass.getpass", return_value="community\nwith-newline")
    @patch("deploy.setup_wizard.prompt", side_effect=["switch-snmp", "2"])
    def test_profile_wizard_rejects_unusable_snmp_community(self, prompt, getpass):
        with self.assertRaises(SetupError):
            collect_profiles()


if __name__ == "__main__":
    unittest.main()
