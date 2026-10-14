import os
import unittest
from unittest import mock

from nobetci.config import Config


class ConfigTest(unittest.TestCase):
    def test_kubernetes_service_link_does_not_break_port(self):
        env = {"NOBETCI_PORT": "tcp://10.96.250.114:80", "NOBETCI_INTERVAL": "30", "NOBETCI_SERVICE_HOST": "10.96.250.114"}
        with mock.patch.dict(os.environ, env):
            c = Config.from_env()
        self.assertEqual(c.port, 8080)
        self.assertEqual(c.interval, 30)


if __name__ == "__main__":
    unittest.main()
