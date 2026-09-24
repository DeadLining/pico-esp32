#pragma once

#include <algorithm>
#include <array>
#include <cstdint>

// Heuristic only: a voltage rise can also be caused by reduced load. A flat
// voltage does not establish charge termination. Never use this for protection.
// Caller serializes access. No allocations, timers, or gauge writes.
class BatteryChargeEstimator {
public:
    bool IsCharging() const { return charging_; }
    bool Due(int64_t now_ms) const { return last_ms_ < 0 || now_ms - last_ms_ >= 5000; }

    bool Update(int64_t now_ms, int mv) {
        if (mv < 2500 || mv > 4500) {
            Reset();
            return false;
        }
        if (last_ms_ >= 0 && (now_ms < last_ms_ || now_ms - last_ms_ > 10000)) {
            Reset();
        }
        if (!Due(now_ms)) return charging_;
        last_ms_ = now_ms;
        for (unsigned i = 1; i < samples_.size(); ++i) samples_[i - 1] = samples_[i];
        samples_.back() = mv;
        if (count_ < samples_.size()) ++count_;
        if (count_ < samples_.size()) return false;

        const int delta = Median(samples_[3], samples_[4], samples_[5]) -
                          Median(samples_[0], samples_[1], samples_[2]);
        if (delta >= 6) {
            if (rising_windows_ < 2) ++rising_windows_;
            if (rising_windows_ >= 2) charging_ = true;
            last_rise_ms_ = now_ms;
        } else {
            rising_windows_ = 0;
            if (delta <= -4 || now_ms - last_rise_ms_ >= 45000) charging_ = false;
        }
        return charging_;
    }

private:
    static int Median(int a, int b, int c) {
        return std::max(std::min(a, b), std::min(std::max(a, b), c));
    }
    void Reset() {
        count_ = 0;
        rising_windows_ = 0;
        last_ms_ = -1;
        last_rise_ms_ = 0;
        charging_ = false;
        samples_.fill(0);
    }
    std::array<int, 6> samples_{};
    unsigned count_ = 0;
    unsigned rising_windows_ = 0;
    int64_t last_ms_ = -1;
    int64_t last_rise_ms_ = 0;
    bool charging_ = false;
};
