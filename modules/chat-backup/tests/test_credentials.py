import unittest

from chat_backup.credentials import CredentialError, mask_email, parse_credentials


class CredentialTests(unittest.TestCase):
    def test_parses_and_normalizes_one_local_credential_line(self):
        credential = parse_credentials(" User@Example.COM | pass word | JBSW Y3DP EHPK 3PXP ")
        self.assertEqual(credential.email, "user@example.com")
        self.assertEqual(credential.password, "pass word")
        self.assertEqual(credential.totp_secret, "JBSWY3DPEHPK3PXP")

    def test_rejects_multiline_or_invalid_input(self):
        for value in ("a@example.com|password\nb@example.com|password", "invalid", "x|password"):
            with self.subTest(value=value), self.assertRaises(CredentialError):
                parse_credentials(value)

    def test_masks_email_for_status(self):
        self.assertEqual(mask_email("person@example.com"), "pe***@example.com")


if __name__ == "__main__":
    unittest.main()
