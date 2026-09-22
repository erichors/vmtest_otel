package com.aheaddemo.checkout;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestTemplate;

/**
 * Checkout tier entry point.
 *
 * Distributed tracing here comes purely from the OpenTelemetry Java agent, attached
 * at process start via -javaagent (Dynatrace OneAgent on this host is infrastructure-only,
 * so it does not add its own deep-code spans - there is exactly one tracer in play).
 * The agent auto-instruments Spring MVC, JdbcTemplate/PostgreSQL JDBC calls, and
 * RestTemplate, and it injects the W3C traceparent header on outbound RestTemplate
 * calls automatically, which is how the trace continues into the pricing-python tier.
 */
@SpringBootApplication
public class CheckoutApplication {

    public static void main(String[] args) {
        SpringApplication.run(CheckoutApplication.class, args);
    }

    @Bean
    public RestTemplate restTemplate() {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(2000);
        factory.setReadTimeout(10000);
        return new RestTemplate(factory);
    }
}
