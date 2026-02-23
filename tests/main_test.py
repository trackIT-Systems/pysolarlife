import unittest
import sys
sys.path.append("..")

class TestImport(unittest.TestCase):
    def test_import(self):
        import solarlife
        self.assertTrue(hasattr(solarlife, "MpptClient"))
        self.assertTrue(hasattr(solarlife, "Result"))
        self.assertTrue(hasattr(solarlife, "ResultContainer"))
        self.assertTrue(hasattr(solarlife, "Variable"))
        self.assertTrue(hasattr(solarlife, "VariableContainer"))
        self.assertTrue(hasattr(solarlife, "variables"))

if __name__ == "__main__":
    unittest.main()
