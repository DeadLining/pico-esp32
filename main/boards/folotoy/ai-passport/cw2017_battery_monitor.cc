#include "cw2017_battery_monitor.h"

#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#define TAG "Cw2017Battery"

// Register map (CW2017 datasheet / FoloToy AI Passport BSP).
#define CW_REG_VERSION   0x00  // version byte; answering means the chip is present
#define CW_REG_VCELL_H   0x02  // 14-bit voltage, V(uV) = raw * 312.5
#define CW_REG_SOC_H     0x04  // high byte = integer percent; 0x05 = 1/256 %
#define CW_REG_CONFIG    0x08  // 0xF0 sleep / 0x30 restart / 0x00 active
#define CW_REG_SOC_ALERT 0x0B  // bit7 = profile UPDATE_FLAG; bit6:0 = SOC alert threshold
#define CW_REG_PROFILE   0x10  // start of the 80-byte cell profile

#define CW_CONFIG_ACTIVE  0x00
#define CW_CONFIG_RESTART 0x30
#define CW_CONFIG_SLEEP   0xF0
#define CW_UPDATE_FLAG    0x80
#define CW_PROFILE_SIZE   80

// Cell profile for the Passport's 520 mAh Li-Poly cell. The CW2017 converts
// voltage to state-of-charge through this table, so a mismatch with the
// factory default is what makes the reported percentage drift.
static const uint8_t kCellProfile[CW_PROFILE_SIZE] = {
    0x64, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xAD, 0xC7, 0xC8, 0xCA, 0xBD, 0xB1, 0xC1, 0x94,
    0x88, 0xD1, 0xBD, 0x97, 0x88, 0x66, 0x56, 0x4A,
    0x3F, 0x33, 0x26, 0x5C, 0x37, 0xD1, 0x27, 0xD8,
    0xCC, 0xB7, 0xCF, 0xB3, 0xB2, 0xAE, 0xA6, 0x9E,
    0x99, 0x97, 0x9B, 0x86, 0x47, 0x1E, 0x17, 0x26,
    0x49, 0x96, 0xD9, 0xE1, 0xDD, 0xDC, 0xD4, 0x59,
    0x00, 0x00, 0x90, 0x02, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x5C,
};

static_assert(sizeof(kCellProfile) == CW_PROFILE_SIZE,
              "CW2017 cell profile must contain exactly 80 bytes");

Cw2017BatteryMonitor::Cw2017BatteryMonitor(i2c_master_bus_handle_t i2c_bus, uint8_t addr)
    : i2c_bus_(i2c_bus), i2c_device_(nullptr), device_address_(addr) {
    i2c_device_config_t i2c_device_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        // CW2017 is a slow 100 kHz part on the Passport shared bus.
        .scl_speed_hz = 100 * 1000,
        .scl_wait_us = 0,
        .flags = {
            .disable_ack_check = 0,
        },
    };
    if (i2c_master_bus_add_device(i2c_bus, &i2c_device_cfg, &i2c_device_) != ESP_OK) {
        ESP_LOGW(TAG, "Failed to register CW2017 device 0x%02X", addr);
        i2c_device_ = nullptr;
    }
}

Cw2017BatteryMonitor::~Cw2017BatteryMonitor() {
    if (i2c_device_) {
        i2c_master_bus_rm_device(i2c_device_);
        i2c_device_ = nullptr;
    }
}

int Cw2017BatteryMonitor::ReadReg(uint8_t reg, uint8_t* value) {
    if (!i2c_device_) {
        return -1;
    }
    if (i2c_master_transmit_receive(i2c_device_, &reg, 1, value, 1, 100) != ESP_OK) {
        return -1;
    }
    return 0;
}

int Cw2017BatteryMonitor::ReadReg16(uint8_t reg, uint16_t* value) {
    if (!i2c_device_) {
        return -1;
    }
    uint8_t buf[2] = {0, 0};
    // transmit_receive: write the register address, then read two bytes back.
    if (i2c_master_transmit_receive(i2c_device_, &reg, 1, buf, sizeof(buf), 100) != ESP_OK) {
        return -1;
    }
    *value = ((uint16_t)buf[0] << 8) | buf[1];
    return 0;
}

int Cw2017BatteryMonitor::WriteReg(uint8_t reg, uint8_t value) {
    if (!i2c_device_) {
        return -1;
    }
    uint8_t buf[2] = {reg, value};
    if (i2c_master_transmit(i2c_device_, buf, sizeof(buf), 100) != ESP_OK) {
        return -1;
    }
    return 0;
}

// CONFIG's low nibble is reserved; sleep/restart/active follow the datasheet
// sequencing, which requires a settle delay between the two writes.
int Cw2017BatteryMonitor::EnterSleep() {
    if (WriteReg(CW_REG_CONFIG, CW_CONFIG_RESTART) != 0) {
        return -1;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    if (WriteReg(CW_REG_CONFIG, CW_CONFIG_SLEEP) != 0) {
        return -1;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
    return 0;
}

int Cw2017BatteryMonitor::EnterActive() {
    if (WriteReg(CW_REG_CONFIG, CW_CONFIG_RESTART) != 0) {
        return -1;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    if (WriteReg(CW_REG_CONFIG, CW_CONFIG_ACTIVE) != 0) {
        return -1;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
    return 0;
}

// Checks UPDATE_FLAG first and then compares the full 80 bytes, so a stale
// flag alone never makes us trust the wrong cell parameters.
int Cw2017BatteryMonitor::CheckProfile(bool* matches) {
    *matches = false;

    uint8_t value = 0;
    if (ReadReg(CW_REG_SOC_ALERT, &value) != 0) {
        return -1;
    }
    if ((value & CW_UPDATE_FLAG) == 0) {
        return 0;
    }

    for (size_t i = 0; i < CW_PROFILE_SIZE; i++) {
        if (ReadReg((uint8_t)(CW_REG_PROFILE + i), &value) != 0) {
            return -1;
        }
        if (value != kCellProfile[i]) {
            return 0;
        }
    }

    *matches = true;
    return 0;
}

// The profile must be written while the gauge is asleep. After writing we read
// it back, raise UPDATE_FLAG so the chip latches the new table, and restart the
// conversion.
int Cw2017BatteryMonitor::WriteProfile() {
    if (EnterSleep() != 0) {
        return -1;
    }

    for (size_t i = 0; i < CW_PROFILE_SIZE; i++) {
        if (WriteReg((uint8_t)(CW_REG_PROFILE + i), kCellProfile[i]) != 0) {
            ESP_LOGE(TAG, "Failed to write cell profile at index %u", (unsigned)i);
            return -1;
        }
    }

    uint8_t value = 0;
    for (size_t i = 0; i < CW_PROFILE_SIZE; i++) {
        if (ReadReg((uint8_t)(CW_REG_PROFILE + i), &value) != 0) {
            return -1;
        }
        if (value != kCellProfile[i]) {
            ESP_LOGE(TAG, "Cell profile verify failed at index %u: wrote 0x%02X read 0x%02X",
                     (unsigned)i, kCellProfile[i], value);
            return -1;
        }
    }

    if (ReadReg(CW_REG_SOC_ALERT, &value) != 0) {
        return -1;
    }
    if (WriteReg(CW_REG_SOC_ALERT, (uint8_t)(value | CW_UPDATE_FLAG)) != 0) {
        return -1;
    }

    return EnterActive();
}

// Right after a restart the gauge may briefly report >100; wait for the first
// usable sample instead of surfacing a bogus percentage.
int Cw2017BatteryMonitor::WaitSocReady() {
    for (int retry = 0; retry < 50; retry++) {
        vTaskDelay(pdMS_TO_TICKS(100));
        uint8_t soc = 0;
        if (ReadReg(CW_REG_SOC_H, &soc) == 0 && soc <= 100) {
            return 0;
        }
    }
    return -1;
}

bool Cw2017BatteryMonitor::Initialize() {
    if (present_) {
        return true;
    }
    if (!i2c_device_) {
        return false;
    }

    uint8_t version = 0;
    if (ReadReg(CW_REG_VERSION, &version) != 0) {
        ESP_LOGW(TAG, "CW2017 did not answer at 0x%02X - battery disabled", device_address_);
        return false;
    }
    ESP_LOGI(TAG, "CW2017 found (version 0x%02X)", version);

    bool profile_matches = false;
    if (CheckProfile(&profile_matches) != 0) {
        ESP_LOGE(TAG, "Failed to read the CW2017 cell profile - battery disabled");
        return false;
    }

    if (profile_matches) {
        uint8_t config = 0;
        if (ReadReg(CW_REG_CONFIG, &config) != 0) {
            ESP_LOGE(TAG, "Failed to read CW2017 CONFIG - battery disabled");
            return false;
        }
        if (config != CW_CONFIG_ACTIVE && EnterActive() != 0) {
            ESP_LOGE(TAG, "Failed to put CW2017 back into active mode - battery disabled");
            return false;
        }
        ESP_LOGI(TAG, "CW2017 cell profile already installed");
    } else {
        ESP_LOGI(TAG, "Installing CW2017 cell profile for the 520 mAh cell");
        if (WriteProfile() != 0) {
            ESP_LOGE(TAG, "Failed to install the CW2017 cell profile - battery disabled");
            return false;
        }
    }

    if (WaitSocReady() != 0) {
        ESP_LOGE(TAG, "Timed out waiting for the first CW2017 SOC sample - battery disabled");
        return false;
    }

    present_ = true;

    // Surface the first real reading in the boot log: the gauge is otherwise
    // only visible as a status-bar icon, which makes field diagnosis awkward.
    int soc = GetBatteryLevel();
    int mv = GetBatteryVoltageMv();
    if (soc >= 0) {
        ESP_LOGI(TAG, "CW2017 ready: %d%% (%d mV) using the installed cell profile", soc, mv);
    } else {
        ESP_LOGW(TAG, "CW2017 ready but the first reading was unavailable");
    }
    return true;
}

int Cw2017BatteryMonitor::GetBatteryLevel() {
    uint16_t soc;
    if (!present_ || ReadReg16(CW_REG_SOC_H, &soc) != 0) {
        return -1;
    }
    int percent = soc >> 8;  // high byte is the integer percentage
    if (percent > 100) {
        // Chip not ready yet may report 0xFF.
        return -1;
    }
    return percent;
}

int Cw2017BatteryMonitor::GetBatteryVoltageMv() {
    uint16_t raw;
    if (!present_ || ReadReg16(CW_REG_VCELL_H, &raw) != 0) {
        return -1;
    }
    raw &= 0x3FFF;  // 14-bit voltage
    return (int)(((uint32_t)raw * 3125) / 10000);  // raw * 312.5 uV -> mV
}
