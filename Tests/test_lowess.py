try :
    from numpy import array
except ImportError :
    from Bio import MissingExternalDependencyError
    raise MissingExternalDependencyError(\
        "Install NumPy if you want to use Bio.Statistics.lowess.")

from Bio.Statistics.lowess import lowess
import unittest

class test_lowess(unittest.TestCase):

    def test_Precomputed(self):
        x = array([0.0, 1.0, 2.0, 3.0, 5.0, 9.0, 11.0])
        y = x**2
        # Precalculated smooth output
        ys = array([-2.96219015, 1.72680044, 6.58686813,
                    11.62986671, 28.18598762, 86.85271581, 116.83893423 ])
        # Smooth output calculated by the lowess function
        output = lowess(x, y, f=2./3., iter = 3)
        for precomputed, calculated in zip(ys, output):
            self.assertAlmostEqual(precomputed, calculated, 4)

if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity = 2)
    unittest.main(testRunner=runner)
