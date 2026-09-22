package com.aheaddemo.checkout;

import com.aheaddemo.checkout.Dtos.CustomerRow;
import com.aheaddemo.checkout.Dtos.OrderDetail;
import com.aheaddemo.checkout.Dtos.OrderEvent;
import com.aheaddemo.checkout.Dtos.OrderRequest;
import com.aheaddemo.checkout.Dtos.OrderResponse;
import com.aheaddemo.checkout.Dtos.PricingResponse;
import com.aheaddemo.checkout.Dtos.RevenueRow;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.server.ResponseStatusException;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;

/**
 * Checkout order flow. This is where the distributed trace fans out: the POST
 * handler below calls into PostgreSQL directly (customer lookup, order insert)
 * and also makes an outbound HTTP call to the pricing-python tier. The
 * OpenTelemetry Java agent auto-instruments both the JDBC calls and the
 * RestTemplate call, so no manual span or header code is required here -
 * the W3C traceparent header is injected onto the RestTemplate request
 * transparently by the agent.
 */
@RestController
@RequestMapping("/api")
public class OrderController {

    private final OrderRepository repository;
    private final RestTemplate restTemplate;
    private final FaultConfig faultConfig;
    private final String pricingBaseUrl;

    public OrderController(OrderRepository repository,
                            RestTemplate restTemplate,
                            FaultConfig faultConfig,
                            @Value("${pricing.base-url}") String pricingBaseUrl) {
        this.repository = repository;
        this.restTemplate = restTemplate;
        this.faultConfig = faultConfig;
        this.pricingBaseUrl = pricingBaseUrl;
    }

    @PostMapping("/orders")
    @ResponseStatus(HttpStatus.CREATED)
    public OrderResponse createOrder(@RequestBody OrderRequest request) {
        applyFault();

        CustomerRow customer = repository.findCustomer(request.customerId())
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "customer not found"));

        // Do NOT add tracing headers manually here - the OTel Java agent instruments
        // RestTemplate and injects the traceparent header on this call automatically,
        // which is what lets Dynatrace stitch this into one distributed trace with
        // the pricing-python tier.
        String url = pricingBaseUrl + "/api/pricing/" + request.sku()
                + "?qty=" + request.qty() + "&tier=" + customer.tier();

        PricingResponse pricing;
        try {
            pricing = restTemplate.getForObject(url, PricingResponse.class);
        } catch (HttpClientErrorException.Conflict e) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "out of stock");
        } catch (HttpClientErrorException.NotFound e) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "product not found");
        }

        if (pricing == null) {
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "empty pricing response");
        }

        BigDecimal unitPrice = BigDecimal.valueOf(pricing.unitPrice()).setScale(2, RoundingMode.HALF_UP);
        BigDecimal totalPrice = BigDecimal.valueOf(pricing.totalPrice()).setScale(2, RoundingMode.HALF_UP);

        long orderId = repository.insertOrder(
                customer.id(), request.sku(), request.qty(), unitPrice, totalPrice, "CONFIRMED");
        repository.insertOrderEvent(orderId, "ORDER_CREATED", "order placed for customer " + customer.id());

        return repository.findOrderById(orderId)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "order vanished after insert"));
    }

    @GetMapping("/orders/{id}")
    public OrderDetail getOrder(@PathVariable long id) {
        OrderResponse order = repository.findOrderById(id)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "order not found"));
        List<OrderEvent> events = repository.findEventsByOrderId(id);
        return new OrderDetail(order, events);
    }

    @GetMapping("/orders/recent")
    public List<OrderResponse> recentOrders(@RequestParam(defaultValue = "20") int limit) {
        return repository.findRecentOrders(limit);
    }

    /** Deliberately heavier query, so Dynatrace shows a slower DB span for this endpoint. */
    @GetMapping("/reports/revenue")
    public List<RevenueRow> revenueReport() {
        return repository.revenueByCategory();
    }

    private void applyFault() {
        if (!faultConfig.shouldFire()) {
            return;
        }
        switch (faultConfig.getMode()) {
            case "slow" -> sleep(faultConfig.getSlowMs());
            case "error" -> throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "injected checkout failure");
            case "dbslow" -> repository.pgSleep(faultConfig.getSlowMs() / 1000.0);
            default -> { /* none: no-op */ }
        }
    }

    private void sleep(int ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
