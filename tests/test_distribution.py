import unittest
from importlib.resources import files

from healthassure_messaging import REQUEST_SCHEMA_VERSION


class DistributionTests(unittest.TestCase):
    def test_typed_package_marker_is_available(self) -> None:
        self.assertTrue(files("healthassure_messaging").joinpath("py.typed").is_file())

    def test_request_schema_version_remains_one(self) -> None:
        self.assertEqual(REQUEST_SCHEMA_VERSION, 1)
