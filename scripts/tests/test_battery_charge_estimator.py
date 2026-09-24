"""Execute the production C++ voltage-trend estimator without ESP hardware."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class BatteryChargeEstimatorTest(unittest.TestCase):
    def test_trends_and_invalid_samples(self):
        source = r'''
#include <cassert>
#include "battery_charge_estimator.h"
int main() {
    BatteryChargeEstimator rise;
    for (int i=0;i<6;i++) assert(!rise.Update(i*5000,4000+i*3));
    assert(rise.Update(30000,4018)); // two confirmed rising windows
    for (int i=7;i<=14;i++) rise.Update(i*5000,4018-(i-6)*3);
    assert(!rise.IsCharging());
    BatteryChargeEstimator flat, noise, spike;
    for (int i=0;i<60;i++) {
        assert(!flat.Update(i*5000,4108));
        assert(!noise.Update(i*5000,4108+i%4));
        assert(!spike.Update(i*5000,4108+(i==8?80:0)));
    }
    BatteryChargeEstimator plateau;
    for(int i=0;i<=6;i++) plateau.Update(i*5000,4000+i*3);
    assert(plateau.IsCharging());
    for(int i=7;i<=25;i++) plateau.Update(i*5000,4018);
    assert(!plateau.IsCharging());
    BatteryChargeEstimator invalid, gap;
    for(int i=0;i<=6;i++) {invalid.Update(i*5000,4000+i*3);gap.Update(i*5000,4000+i*3);}
    assert(!invalid.Update(31000,-1));
    assert(!gap.Update(60000,4050));
    BatteryChargeEstimator frequent;
    for(int i=0;i<30;i++) assert(!frequent.Update(i*100,4000+i*5));
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            cpp=Path(tmp)/'test.cc'; exe=Path(tmp)/'test'
            cpp.write_text(source)
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-I',str(ROOT/'main/boards/folotoy/ai-passport'),str(cpp),'-o',str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
