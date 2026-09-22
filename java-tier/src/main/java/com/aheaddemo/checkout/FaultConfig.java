package com.aheaddemo.checkout;

import org.springframework.stereotype.Component;

import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Mutable, thread-safe fault-injection state for chaos-engineering demos.
 *
 * Toggled at runtime via {@link AdminController} so a live Dynatrace session can
 * show slow spans, thrown errors, or slow DB spans on demand, without a redeploy.
 * Mirrors the identical none|slow|error|dbslow contract exposed by the Python tier.
 */
@Component
public class FaultConfig {

    public static final Set<String> VALID_MODES = Set.of("none", "slow", "error", "dbslow");

    private final AtomicReference<String> mode = new AtomicReference<>("none");
    private volatile double rate = 0.0;
    private volatile int slowMs = 1500;

    public String getMode() {
        return mode.get();
    }

    public double getRate() {
        return rate;
    }

    public int getSlowMs() {
        return slowMs;
    }

    public void update(String newMode, Double newRate, Integer newSlowMs) {
        if (newMode != null) {
            mode.set(newMode);
        }
        if (newRate != null) {
            rate = newRate;
        }
        if (newSlowMs != null) {
            slowMs = newSlowMs;
        }
    }

    /** True when a fault mode is active and this call's dice roll says "fire". */
    public boolean shouldFire() {
        return !"none".equals(mode.get()) && ThreadLocalRandom.current().nextDouble() < rate;
    }
}
