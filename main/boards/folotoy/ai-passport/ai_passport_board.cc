#include "wifi_board.h"
#include "display/lcd_display.h"
#include "codecs/es8311_audio_codec.h"
#include "application.h"
#include "button.h"
#include "config.h"
#include "assets/lang_config.h"
#include "cw2017_battery_monitor.h"
#include "battery_charge_estimator.h"

#include <esp_log.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_timer.h>
#include <button_adc.h>
#include <esp_adc/adc_oneshot.h>
#include <driver/i2c_master.h>
#include <driver/spi_common.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <mutex>

#define TAG "AiPassport"

// The screen turns off after this much accumulated true-idle time
// (kDeviceStateIdle with the audio channel closed). Listening, speaking,
// notifying and connecting never accumulate, and no countdown exists at all
// once the screen is off.
#define IDLE_MS_BEFORE_SCREEN_OFF (60 * 1000)

// Screen timer poll period in milliseconds. This is also the worst-case latency
// for lighting the screen back up when a notification arrives while blanked.
#define SCREEN_TIMER_PERIOD_MS 200

// Physical keys share one ADC pin through a resistor ladder (see config.h).
enum {
    kAdcButtonUp = 0,
    kAdcButtonDown,
    kAdcButtonOk,
    kAdcButtonNum,
};

class AiPassportBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t codec_i2c_bus_;
    Button* adc_button_[kAdcButtonNum];
    adc_oneshot_unit_handle_t adc_handle_ = nullptr;
    LcdDisplay* display_;
    Cw2017BatteryMonitor* battery_;
    BatteryChargeEstimator charge_estimator_;
    std::mutex battery_mutex_;

    // Idle-timer screen blanking. The flag is shared between the esp_timer task
    // (countdown), the button task (callbacks) and the main task (applying the
    // backlight change), so it must be atomic.
    esp_timer_handle_t screen_timer_ = nullptr;
    std::atomic<bool> screen_off_{false};
    std::atomic<int> idle_ms_{0};

    void InitializeCodecI2c() {
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &codec_i2c_bus_));

        // CW2017 fuel gauge is optional; a missing chip just disables battery UI.
        battery_ = new Cw2017BatteryMonitor(codec_i2c_bus_, BATTERY_CW2017_ADDR);
        battery_->Initialize();
    }

    void ChangeVolume(int delta) {
        auto codec = GetAudioCodec();
        auto volume = codec->output_volume() + delta;
        if (volume > 100) {
            volume = 100;
        }
        if (volume < 0) {
            volume = 0;
        }
        codec->SetOutputVolume(volume);
        GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
    }

    void ToggleChat() {
        auto& app = Application::GetInstance();
        if (app.GetDeviceState() == kDeviceStateStarting) {
            EnterWifiConfigMode();
            return;
        }
        app.ToggleChatState();
    }

    // --- Screen blanking -----------------------------------------------------
    // These three helpers must run on the main task: the backlight drives LEDC
    // and the display is touched from LVGL. Request* are the thread-safe entries
    // used by the button callbacks and the timer.

    void ApplyScreenOff() {
        // Re-check on the main task: the idle countdown may have fired at the
        // same moment the user pressed a button, in which case the device is no
        // longer idle and must stay lit.
        if (!Application::GetInstance().CanEnterSleepMode()) {
            return;
        }
        if (screen_off_.exchange(true)) {
            return;
        }
        ESP_LOGI(TAG, "Screen off (idle)");
        GetBacklight()->SetBrightness(0);
    }

    void ApplyScreenOn() {
        if (!screen_off_.exchange(false)) {
            return;
        }
        idle_ms_ = 0;
        ESP_LOGI(TAG, "Screen on");
        GetBacklight()->RestoreBrightness();
    }

    void RequestScreenOff() {
        Application::GetInstance().Schedule([this]() { ApplyScreenOff(); });
    }

    void RequestScreenOn() {
        Application::GetInstance().Schedule([this]() { ApplyScreenOn(); });
    }

    void ResetIdleTimer() {
        idle_ms_ = 0;
    }

    // Runs in the esp_timer task every SCREEN_TIMER_PERIOD_MS.
    void OnScreenTimerTick() {
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();

        if (screen_off_.load()) {
            // No countdown exists while blanked. Instead, watch for the assistant
            // starting to talk or a notification arriving, and light the screen
            // back up so the user is not left staring at a dark display while it
            // is speaking.
            if (state == kDeviceStateSpeaking || state == kDeviceStateNotifying) {
                RequestScreenOn();
            }
            return;
        }

        // Only true idle (kDeviceStateIdle + closed audio channel + idle audio
        // service) accumulates. Listening / speaking / notifying / connecting
        // reset the counter instead of advancing it.
        if (!app.CanEnterSleepMode()) {
            idle_ms_ = 0;
            return;
        }

        idle_ms_ += SCREEN_TIMER_PERIOD_MS;
        if (idle_ms_ >= IDLE_MS_BEFORE_SCREEN_OFF) {
            idle_ms_ = 0;
            RequestScreenOff();
        }
    }

    void InitializeScreenTimer() {
        esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                static_cast<AiPassportBoard*>(arg)->OnScreenTimerTick();
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "screen_timer",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &screen_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(screen_timer_, SCREEN_TIMER_PERIOD_MS * 1000));
    }

    void InitializeButtons() {
        for (int i = 0; i < kAdcButtonNum; i++) {
            adc_button_[i] = nullptr;
        }

        // One ADC1 unit shared by all three ladder keys. AdcButton reuses the
        // handle when adc_config.adc_handle is non-null, so the same physical
        // pin can decode several keys without "adc1 is already in use".
        adc_oneshot_unit_init_cfg_t init_cfg = {
            .unit_id = ADC_UNIT_1,
        };
        ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_cfg, &adc_handle_));

        button_adc_config_t adc_cfg = {};
        adc_cfg.adc_handle = &adc_handle_;
        adc_cfg.unit_id = ADC_UNIT_1;
        adc_cfg.adc_channel = ADC_CHANNEL_0;  // GPIO0

        adc_cfg.button_index = kAdcButtonUp;      // UP:   ~0 mV
        adc_cfg.min = BSP_ADC_BUTTON_UP_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_UP_MAX;
        adc_button_[kAdcButtonUp] = new AdcButton(adc_cfg);

        adc_cfg.button_index = kAdcButtonDown;    // DOWN: ~300 mV
        adc_cfg.min = BSP_ADC_BUTTON_DOWN_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_DOWN_MAX;
        adc_button_[kAdcButtonDown] = new AdcButton(adc_cfg);

        adc_cfg.button_index = kAdcButtonOk;      // OK:   ~595 mV
        adc_cfg.min = BSP_ADC_BUTTON_OK_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_OK_MAX;
        adc_button_[kAdcButtonOk] = new AdcButton(adc_cfg);

        // Button callbacks run on the button task; schedule all UI/audio
        // work onto the main task so LVGL and codec access stay on one thread.
        // While the screen is blanked, any button is ignored except OK (which
        // only wakes the screen and deliberately does NOT start a conversation).
        // While it is lit, every button resets the idle countdown.

        auto up = adc_button_[kAdcButtonUp];
        up->OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    return;
                }
                ResetIdleTimer();
                ChangeVolume(10);
            });
        });
        up->OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    return;
                }
                ResetIdleTimer();
                GetAudioCodec()->SetOutputVolume(100);
                GetDisplay()->ShowNotification(Lang::Strings::MAX_VOLUME);
            });
        });

        auto down = adc_button_[kAdcButtonDown];
        down->OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    return;
                }
                ResetIdleTimer();
                ChangeVolume(-10);
            });
        });
        down->OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    return;
                }
                ResetIdleTimer();
                GetAudioCodec()->SetOutputVolume(0);
                GetDisplay()->ShowNotification(Lang::Strings::MUTED);
            });
        });

        auto ok = adc_button_[kAdcButtonOk];
        ok->OnClick([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    // Wake only; leave the conversation state untouched.
                    ApplyScreenOn();
                    return;
                }
                ResetIdleTimer();
                ToggleChat();
            });
        });
        ok->OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                if (screen_off_.load()) {
                    // Already blanked; long press is a no-op so that waking has a
                    // single, predictable path (OK single click).
                    return;
                }
                ApplyScreenOff();
            });
        });
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = DISPLAY_SPI_MOSI_PIN;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = DISPLAY_SPI_SCK_PIN;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeDisplay() {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = DISPLAY_SPI_CS_PIN;
        io_config.dc_gpio_num = DISPLAY_DC_PIN;
        io_config.spi_mode = 0;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &panel_io));

        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = DISPLAY_RST_PIN;  // -1 -> software reset
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));

        esp_lcd_panel_reset(panel);
        esp_lcd_panel_init(panel);

        // Panel-specific power/porch/gamma sequence for the ST7789P3 module
        // used on the Passport (copied from the original badge firmware).
        static const struct {
            uint8_t command;
            uint8_t data[16];
            uint8_t data_length;
            uint16_t delay_ms;
        } kSt7789P3InitCommands[] = {
            {0xB2, {0x05, 0x05, 0x00, 0x33, 0x33}, 5, 0},
            {0xB7, {0x35}, 1, 0},
            {0xBB, {0x21}, 1, 0},
            {0xC0, {0x2C}, 1, 0},
            {0xC2, {0x01}, 1, 0},
            {0xC3, {0x0B}, 1, 0},
            {0xC4, {0x20}, 1, 0},
            {0xC6, {0x0F}, 1, 0},
            {0xD0, {0xA7, 0xA1}, 2, 0},
            {0xD0, {0xA4, 0xA1}, 2, 0},
            {0xD6, {0xA1}, 1, 0},
            {0xE0, {0xD0, 0x04, 0x08, 0x0A, 0x09, 0x05, 0x2D, 0x43,
                    0x49, 0x09, 0x16, 0x15, 0x26, 0x2B}, 14, 0},
            {0xE1, {0xD0, 0x03, 0x09, 0x0A, 0x0A, 0x06, 0x2E, 0x44,
                    0x40, 0x3A, 0x15, 0x15, 0x26, 0x2A}, 14, 10},
        };
        for (const auto& cmd : kSt7789P3InitCommands) {
            esp_lcd_panel_io_tx_param(panel_io, cmd.command, cmd.data, cmd.data_length);
            if (cmd.delay_ms > 0) {
                vTaskDelay(pdMS_TO_TICKS(cmd.delay_ms));
            }
        }

        esp_lcd_panel_invert_color(panel, DISPLAY_INVERT_COLOR);
        esp_lcd_panel_set_gap(panel, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
        esp_lcd_panel_disp_on_off(panel, true);

        display_ = new SpiLcdDisplay(panel_io, panel,
                                     DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                     DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y,
                                     DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
        display_->SetBatteryChargingColor(0x22C55E);
        display_->ShowBatteryPercentage();
    }

public:
    AiPassportBoard() : display_(nullptr), battery_(nullptr) {
        InitializeCodecI2c();
        InitializeSpi();
        InitializeDisplay();
        InitializeButtons();
        GetBacklight()->RestoreBrightness();
        InitializeScreenTimer();
    }

    virtual AudioCodec* GetAudioCodec() override {
        static Es8311AudioCodec audio_codec(
            codec_i2c_bus_,
            I2C_NUM_0,
            AUDIO_INPUT_SAMPLE_RATE,
            AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK,
            AUDIO_I2S_GPIO_BCLK,
            AUDIO_I2S_GPIO_WS,
            AUDIO_I2S_GPIO_DOUT,
            AUDIO_I2S_GPIO_DIN,
            AUDIO_CODEC_PA_PIN,
            AUDIO_CODEC_ES8311_ADDR);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Backlight* GetBacklight() override {
        static PwmBacklight backlight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        return &backlight;
    }

    virtual bool GetBatteryLevel(int& level, bool& charging, bool& discharging) override {
        std::lock_guard<std::mutex> lock(battery_mutex_);
        if (!battery_ || !battery_->IsPresent()) {
            return false;
        }
        int soc = battery_->GetBatteryLevel();
        if (soc < 0) {
            charge_estimator_.Update(esp_timer_get_time() / 1000, -1);
            return false;
        }
        level = soc;
        const auto now_ms = esp_timer_get_time() / 1000;
        if (charge_estimator_.Due(now_ms)) {
            const bool previous = charge_estimator_.IsCharging();
            charge_estimator_.Update(now_ms, battery_->GetBatteryVoltageMv());
            if (previous != charge_estimator_.IsCharging()) {
                ESP_LOGI(TAG, "Estimated charging: %s (voltage trend, not hardware status)",
                         charge_estimator_.IsCharging() ? "yes" : "no");
            }
        }
        charging = charge_estimator_.IsCharging();
        discharging = !charging;
        return true;
    }
};

DECLARE_BOARD(AiPassportBoard);
