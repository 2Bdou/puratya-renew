#!/usr/bin/env python3
import base64
import json
import time
import unittest

from auth import extract_mws_token, jwt_seconds_left, discord_token
import os


class JwtTests(unittest.TestCase):
    def test_seconds_left(self):
        exp = int(time.time()) + 3600
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        token = "eyJhbGciOiJub25lIn0.{}.x".format(payload)
        left = jwt_seconds_left(token)
        self.assertIsNotNone(left)
        self.assertTrue(3500 < left < 3700)

    def test_bad_token(self):
        self.assertIsNone(jwt_seconds_left(""))
        self.assertIsNone(jwt_seconds_left("not-a-jwt"))


class CookieTests(unittest.TestCase):
    def test_extract(self):
        header = (
            "__Host-mrtcloud_oauth_state=abc; Path=/; HttpOnly, "
            "__Host-mrtcloud_token=eyJhbGciOiJIUzI1NiJ9.aaa.bbb; Path=/; Secure"
        )
        self.assertEqual(
            extract_mws_token(header),
            "eyJhbGciOiJIUzI1NiJ9.aaa.bbb",
        )

    def test_missing(self):
        self.assertEqual(extract_mws_token("foo=bar"), "")


class DiscordTokenEnvTests(unittest.TestCase):
    def test_comma_form(self):
        os.environ["DISCORD_TOKEN"] = "备注,the-real-token"
        self.assertEqual(discord_token(), "the-real-token")
        del os.environ["DISCORD_TOKEN"]


if __name__ == "__main__":
    unittest.main()
