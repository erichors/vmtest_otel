package com.aheaddemo.checkout;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Lightweight liveness endpoint for the load generator's convenience, distinct
 * from the richer /actuator/health exposed by Spring Boot Actuator.
 */
@RestController
public class HealthController {

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "UP", "service", "checkout-java");
    }
}
