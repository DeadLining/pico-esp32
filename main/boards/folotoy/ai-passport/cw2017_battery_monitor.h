#ifndef CW2017_BATTERY_MONITOR_H
#define CW2017_BATTERY_MONITOR_H

#include <driver/i2c_master.h>

// CW2017 fuel-gauge helper for boards that mount the chip on the shared
// codec I2C bus (e.g. FoloToy AI Passport, address 0x63).
//
// The chip is optional: a missing device must not crash the board, so reads
// return -1 / false instead of asserting.
//
// The CW2017 derives state-of-charge from an on-chip battery model. Its
// factory default is a generic Li-Poly curve that does not match the
// Passport's 520 mAh cell, which skews the reported percentage - most
// visible across the middle and low SOC range, and it also shifts when the
// low-battery warning fires. Initialize() therefore installs the vendor
// profile, mirroring the FoloToy BSP reference driver.
//
// Battery "charging" is not reported by CW2017 and the Passport has no
// charge-detect GPIO, so the charge state is always unknown; callers decide
// how to surface that.
class Cw2017BatteryMonitor {
public:
    Cw2017BatteryMonitor(i2c_master_bus_handle_t i2c_bus, uint8_t addr = 0x63);
    ~Cw2017BatteryMonitor();

    // Probes the chip, installs the vendor cell profile when it is missing or
    // stale, and leaves the gauge in active mode. Blocks while the first SOC
    // sample settles (typically ~100 ms, bounded at ~5 s). Returns false when
    // the chip is absent or unusable, in which case the board reports "no
    // battery" and keeps working.
    bool Initialize();

    // Battery state of charge in percent (0-100), or -1 when unavailable.
    int GetBatteryLevel();

    // Battery voltage in mV, or -1 when unavailable.
    int GetBatteryVoltageMv();

    bool IsPresent() const { return present_; }

private:
    i2c_master_bus_handle_t i2c_bus_;
    i2c_master_dev_handle_t i2c_device_;
    uint8_t device_address_;
    bool present_ = false;

    int ReadReg(uint8_t reg, uint8_t* value);
    int ReadReg16(uint8_t reg, uint16_t* value);
    int WriteReg(uint8_t reg, uint8_t value);
    int EnterSleep();
    int EnterActive();
    int CheckProfile(bool* matches);
    int WriteProfile();
    int WaitSocReady();
};

#endif  // CW2017_BATTERY_MONITOR_H
